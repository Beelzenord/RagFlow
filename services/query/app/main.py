"""Query service. Path is intentionally minimal — every expensive operation
already happened during ingestion."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from rag_shared.db import session_scope
from rag_shared.embeddings import EmbeddingClient
from rag_shared.llm import LLMClient, Turn
from rag_shared.security import require_service_key
from rag_shared.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("query")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the embedding + LLM clients once per process and reuse their
    connection pools across all requests. Saves a TLS handshake (or two) per
    /query call."""
    app.state.embedder = EmbeddingClient()
    app.state.llm = LLMClient()
    log.info("query service ready (embedder + llm clients initialized)")
    try:
        yield
    finally:
        await app.state.embedder.aclose()
        await app.state.llm.aclose()


app = FastAPI(title="RAG Query Service", version="0.1.0", lifespan=lifespan)

SYSTEM_PROMPT = (
    "You are a warm, helpful assistant that answers questions from the user's documents. "
    "Use a friendly, conversational tone - clear and human, never robotic - but stay focused "
    "and avoid filler. Answer ONLY using the provided sources; never invent facts or rely on "
    "outside knowledge. Do NOT include bracketed citation markers like [1], [2] in your "
    "answer; the UI shows downloadable source documents separately. If the answer is not in "
    "the sources, say so honestly and kindly, and suggest what the user could try next "
    "(rephrasing the question, or sharing a document that covers it)."
)

SYSTEM_PROMPT_VOICE = (
    "You are a warm, helpful voice assistant that answers from the user's documents. Speak "
    "naturally and kindly, the way you would help a colleague - clear and easy to follow "
    "aloud, but never invent facts. Answer ONLY using the provided sources. Do NOT include "
    "bracketed citation markers like [1], [2] in your spoken text; the UI shows sources "
    "separately. Keep answers concise. If the answer is not in the sources, say so honestly "
    "in a sentence or two and gently suggest what the user could try next."
)

NO_HITS_ANSWER = (
    "I looked through the indexed documents but couldn't find anything relevant to that "
    "question. Try rephrasing it, or upload a document that covers this topic and I'll take "
    "another look."
)

REWRITE_PROMPT = (
    "Rewrite the user's latest message as a single standalone search question. Resolve "
    "pronouns and implicit references using the conversation. Keep the user's language and "
    "wording where possible. If the message is already self-contained, repeat it unchanged. "
    "Output ONLY the question - no preamble, quotes, or explanation."
)

# A follow-up like "and the delivery time?" embeds to nothing useful, so it is
# condensed against the transcript before retrieval. Bounded low: this is a
# one-line rewrite, not an answer.
REWRITE_MAX_TOKENS = 120
# Guardrail for a runaway rewrite (model explaining itself instead of answering).
REWRITE_MAX_CHARS = 400


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    document_id: str | None = None
    collection: str | None = None
    user_id: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    voice: bool | None = False
    lang: str | None = None
    # Prior turns, oldest first, excluding the current question.
    history: list[ChatTurn] = Field(default_factory=list, max_length=12)


# Human-readable names so we can ask the model to answer in that language.
LANG_NAMES: dict[str, str] = {
    "en": "English",
    "sv": "Swedish",
}


def _system_prompt_for(req: QueryRequest) -> str:
    base = SYSTEM_PROMPT_VOICE if req.voice else SYSTEM_PROMPT
    if req.history:
        base += (
            " Earlier turns of this conversation are included for context: use them to "
            "understand what the user is referring to, but ground every fact in the sources "
            "provided with the current question."
        )
    name = LANG_NAMES.get((req.lang or "").lower())
    if name:
        return f"{base} Answer in {name}."
    return base


def _history_turns(req: QueryRequest) -> list[Turn]:
    return [(t.role, t.content) for t in req.history]


async def _rewrite_question(req: QueryRequest, llm: LLMClient) -> str:
    """Condense a follow-up into a standalone question for retrieval.

    Falls back to the original question on any failure or implausible output -
    a bad rewrite would silently poison retrieval, so the original always wins
    when in doubt.
    """
    if not req.history:
        return req.question
    try:
        raw = await llm.complete(
            REWRITE_PROMPT,
            req.question,
            max_tokens=REWRITE_MAX_TOKENS,
            history=_history_turns(req),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("query rewrite failed, using original question: %s", exc)
        return req.question

    out = (raw or "").strip()
    if not out:
        return req.question
    candidate = out.splitlines()[0].strip().strip('"').strip("'").strip()
    if not candidate or len(candidate) > REWRITE_MAX_CHARS:
        return req.question
    return candidate


class Citation(BaseModel):
    n: int
    document_id: str
    filename: str
    page_number: int | None
    heading: str | None
    # Vector cosine similarity, reported for every hit even when the chunk was
    # surfaced by the lexical retriever.
    score: float
    # Which retriever(s) found this chunk: "dense", "lexical", or "both".
    retrieval: str = "dense"


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    # Set only when a follow-up was condensed into a standalone search question.
    search_query: str | None = None


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _build_filter(req: QueryRequest) -> tuple[str, dict[str, Any]]:
    # 'degraded' documents lost layout on one or more pages but kept their
    # content, so they stay searchable - excluding them would hide the very
    # pages the recovery was performed to save.
    clauses = ["d.status IN ('completed','degraded')"]
    params: dict[str, Any] = {}
    if req.document_id:
        clauses.append("d.id = :document_id")
        params["document_id"] = req.document_id
    if req.collection:
        clauses.append("d.collection = :collection")
        params["collection"] = req.collection
    if req.user_id:
        clauses.append("d.user_id = :user_id")
        params["user_id"] = req.user_id
    return " AND ".join(clauses), params


# Counting how many chunks contain a term is O(matches), so a word like "the"
# would cost a full index walk on every query. Stop counting past the point
# where the answer stops mattering - anything at the cap is far too common to
# be of interest to this retriever.
_DF_CAP = 5000
# On a small corpus a percentage cutoff would demand near-uniqueness, so never
# require a term to be rarer than this many chunks.
_MIN_RARE_CHUNKS = 3
# Length at which a purely alphabetic term is allowed through. Short words are
# grammar; identifiers and the distinctive nouns worth a literal search are
# longer than this or contain a digit.
_MIN_LEXEME_LEN = 6

# The lexical retriever exists to catch what vector search misses: invoice
# numbers, meter IDs, org numbers, fuse ratings, place and product names. Which
# query terms it searches for matters more than how it ranks them.
#
# Rarity alone is not enough to pick them out. Postgres text search has no
# inverse document frequency, so unweighted it lets a chunk repeating "number"
# and "document" outrank the one chunk holding the identifier the user typed.
# But weighting by corpus frequency has its own failure: in a small or
# multilingual corpus, English function words are genuinely rare. Measured
# here, "how" appeared in 3 chunks and "many" in 5, while "vacation" appeared
# in 32 - so frequency by itself keeps the grammar and discards the content.
#
# Shape settles it. A term earns a literal search by containing a digit or by
# being long, and by then also being rare. Everything else is left to the
# vector side, which is good at exactly the words this drops. A question made
# only of common short words yields no lexical hits at all, which is correct
# rather than a gap - it also stops weak matches from bypassing the vector
# score floor applied further down.
#
# Surviving weights are squared before summing so that one unique token beats
# several merely uncommon ones outright instead of narrowly edging them out.
_LEXICAL_TERMS = """
    SELECT lexeme, ln(1 + total::float / df) AS idf
    FROM (
        SELECT u.lexeme,
               (SELECT count(*) FROM (
                   SELECT 1 FROM document_chunks c2
                   WHERE c2.content_tsv @@ to_tsquery('simple', quote_literal(u.lexeme))
                   LIMIT GREATEST(
                       :df_cap,
                       ((SELECT count(*) FROM document_chunks) * :max_df)::int + 1
                   )
               ) capped) AS df,
               (SELECT count(*) FROM document_chunks) AS total
        FROM unnest(to_tsvector('simple', :qtext)) AS u
        WHERE length(u.lexeme) >= :min_len OR u.lexeme ~ '[0-9]'
    ) counted
    WHERE df BETWEEN 1 AND GREATEST((total * :max_df)::int, :min_rare)
"""


def _retrieval_label(in_dense: bool, in_lexical: bool) -> str:
    if in_dense and in_lexical:
        return "both"
    return "lexical" if in_lexical else "dense"


async def _retrieve_and_format(
    req: QueryRequest,
    embedder: EmbeddingClient,
    search_query: str | None = None,
) -> tuple[list[Citation], str | None]:
    """Run hybrid retrieval and build the LLM user prompt.

    Two retrievers cover the same filtered candidates: pgvector cosine for
    meaning, and a tsvector match for literal tokens. They fail in different
    places - vector search is poor at OCR numbers, org numbers and fuse
    ratings, which is precisely what lexical search is best at - so their
    rankings are combined with Reciprocal Rank Fusion. RRF needs no score
    normalisation, which matters because cosine similarity and ts_rank_cd are
    not on comparable scales; only their orderings are.

    The min_score floor applies to the vector side alone. Matching the query's
    terms is already a hard filter on the lexical side, and a chunk found by
    its invoice number is exactly the hit a cosine floor would discard.

    Returns (citations, user_prompt). user_prompt is None when nothing matched,
    in which case the caller should short-circuit with NO_HITS_ANSWER.

    `search_query` is what gets retrieved on (the standalone rewrite of a
    follow-up); the prompt still shows the user's own wording.
    """
    query_text = search_query or req.question
    try:
        q_vec = await embedder.embed_one(query_text)
    except Exception as exc:
        raise HTTPException(502, f"embedding failed: {exc}") from exc

    top_k = req.top_k or settings.retrieval_top_k
    where, params = _build_filter(req)
    params.update(
        {
            "q": str(q_vec),
            "qtext": query_text,
            "pool": max(top_k, settings.retrieval_pool_size),
            "k": top_k,
            "rrf_k": settings.retrieval_rrf_k,
            "min_score": settings.retrieval_min_score,
            "df_cap": _DF_CAP,
            "max_df": settings.retrieval_lexical_max_df,
            "min_rare": _MIN_RARE_CHUNKS,
            "min_len": _MIN_LEXEME_LEN,
        }
    )

    sql = f"""
        WITH lex_terms AS ({_LEXICAL_TERMS}),
        dense AS (
            SELECT c.id,
                   row_number() OVER (ORDER BY c.embedding <=> :q) AS rank
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {where}
            ORDER BY c.embedding <=> :q
            LIMIT :pool
        ),
        lexical AS (
            SELECT c.id,
                   row_number() OVER (
                       ORDER BY SUM(t.idf * t.idf) DESC, c.id
                   ) AS rank
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            JOIN lex_terms t
              ON c.content_tsv @@ to_tsquery('simple', quote_literal(t.lexeme))
            WHERE {where}
            GROUP BY c.id
            ORDER BY SUM(t.idf * t.idf) DESC, c.id
            LIMIT :pool
        ),
        fused AS (
            SELECT COALESCE(dn.id, lx.id)                  AS id,
                   COALESCE(1.0 / (:rrf_k + dn.rank), 0)
                     + COALESCE(1.0 / (:rrf_k + lx.rank), 0) AS rrf,
                   (dn.id IS NOT NULL)                     AS in_dense,
                   (lx.id IS NOT NULL)                     AS in_lexical
            FROM dense dn
            FULL OUTER JOIN lexical lx ON lx.id = dn.id
        )
        SELECT  c.id            AS chunk_id,
                c.document_id,
                c.chunk_index,
                c.page_number,
                c.heading,
                c.content,
                d.original_filename,
                1 - (c.embedding <=> :q) AS score,
                f.in_dense,
                f.in_lexical
        FROM fused f
        JOIN document_chunks c ON c.id = f.id
        JOIN documents d ON d.id = c.document_id
        WHERE f.in_lexical OR (1 - (c.embedding <=> :q)) >= :min_score
        ORDER BY f.rrf DESC
        LIMIT :k
    """
    async with session_scope() as session:
        rows = (await session.execute(text(sql), params)).mappings().all()

    if not rows:
        return [], None

    blocks: list[str] = []
    citations: list[Citation] = []
    for i, r in enumerate(rows, start=1):
        loc = f"{r['original_filename']}"
        if r["page_number"]:
            loc += f", p.{r['page_number']}"
        if r["heading"]:
            loc += f" — {r['heading']}"
        blocks.append(f"[{i}] ({loc})\n{r['content']}")
        citations.append(
            Citation(
                n=i,
                document_id=str(r["document_id"]),
                filename=r["original_filename"],
                page_number=r["page_number"],
                heading=r["heading"],
                score=float(r["score"]),
                retrieval=_retrieval_label(r["in_dense"], r["in_lexical"]),
            )
        )

    user_msg = (
        f"Question: {req.question}\n\n"
        "Sources:\n" + "\n\n".join(blocks) + "\n\n"
        "Please answer the question using only these sources, in a warm and conversational "
        "tone. Do not include [#] citation markers in the answer."
    )
    return citations, user_msg


@app.post("/query", response_model=QueryResponse, dependencies=[Depends(require_service_key)])
async def query(req: QueryRequest, request: Request) -> QueryResponse:
    embedder: EmbeddingClient = request.app.state.embedder
    llm: LLMClient = request.app.state.llm

    search_query = await _rewrite_question(req, llm)
    rewritten = search_query if search_query != req.question else None
    citations, user_msg = await _retrieve_and_format(
        req, embedder, search_query=search_query
    )
    if user_msg is None:
        return QueryResponse(answer=NO_HITS_ANSWER, citations=[], search_query=rewritten)
    try:
        answer = await llm.complete(
            _system_prompt_for(req),
            user_msg,
            max_tokens=800,
            history=_history_turns(req),
        )
    except Exception as exc:
        raise HTTPException(502, f"llm call failed: {exc}") from exc
    return QueryResponse(
        answer=answer.strip(), citations=citations, search_query=rewritten
    )


@app.post("/query/stream", dependencies=[Depends(require_service_key)])
async def query_stream(req: QueryRequest, request: Request) -> StreamingResponse:
    """Same retrieval as /query, but streams the LLM answer as NDJSON.

    Wire format (one JSON object per line, application/x-ndjson):
      {"type":"rewrite","text":"..."}          # only when a follow-up was condensed
      {"type":"citations","data":[{...}, ...]}
      {"type":"delta","text":"..."}            # zero or more, in order
      {"type":"done"}
      {"type":"error","message":"..."}         # only on failure mid-stream
    """
    embedder: EmbeddingClient = request.app.state.embedder
    llm: LLMClient = request.app.state.llm
    return StreamingResponse(
        _run_stream(req, embedder, llm),
        media_type="application/x-ndjson",
    )


def _ndjson(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


async def _run_stream(
    req: QueryRequest,
    embedder: EmbeddingClient,
    llm: LLMClient,
) -> AsyncIterator[bytes]:
    search_query = await _rewrite_question(req, llm)
    if search_query != req.question:
        log.info("rewrote follow-up for retrieval: %r", search_query)
        yield _ndjson({"type": "rewrite", "text": search_query})

    try:
        citations, user_msg = await _retrieve_and_format(
            req, embedder, search_query=search_query
        )
    except HTTPException as exc:
        yield _ndjson({"type": "error", "message": str(exc.detail)})
        return

    yield _ndjson(
        {"type": "citations", "data": [c.model_dump() for c in citations]}
    )

    if user_msg is None:
        yield _ndjson({"type": "delta", "text": NO_HITS_ANSWER})
        yield _ndjson({"type": "done"})
        return

    try:
        async for chunk in llm.stream(
            _system_prompt_for(req),
            user_msg,
            max_tokens=800,
            history=_history_turns(req),
        ):
            if chunk:
                yield _ndjson({"type": "delta", "text": chunk})
    except Exception as exc:  # noqa: BLE001
        log.exception("llm stream failed")
        yield _ndjson({"type": "error", "message": f"llm call failed: {exc}"})
        return

    yield _ndjson({"type": "done"})
