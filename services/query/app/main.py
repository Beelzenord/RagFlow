"""Query service. Path is intentionally minimal — every expensive operation
already happened during ingestion."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from rag_shared.db import session_scope
from rag_shared.embeddings import EmbeddingClient
from rag_shared.llm import LLMClient, Turn
from rag_shared.security import require_service_key
from rag_shared.settings import settings

from .answer_log import (
    ANSWER_MAX_CHARS,
    COVERAGE_MAX_TOKENS,
    COVERAGE_SQL,
    COVERAGE_SYSTEM,
    COVERAGE_TIMEOUT_S,
    CURRENT_NAMES_SQL,
    FEEDBACK_SQL,
    INSERT_SQL,
    INSIGHTS_ROW_CAP,
    INSIGHTS_SQL,
    PURGE_SQL,
    cited_document_ids,
    coverage_prompt,
    current_sources,
    group_questions,
    parse_coverage,
    sources_of,
    summarize,
)
from .language import answer_language_rule, detect_language
from .rerank import rerank
from .audience import (
    AUDIENCE_LABELS_SQL,
    MARKET_NAME_SQL,
    audience_by_document,
    citation_audience,
    market_filter,
    market_guidance,
    prompt_label,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("query")


async def _warm_retrieval() -> None:
    """Pay the first query's fixed startup costs at boot instead of in a user's
    latency budget.

    Two things are cold in a freshly started replica: the SQLAlchemy pool holds
    no connection yet (so the first query pays a TCP + TLS handshake to
    Postgres), and the HNSW index is not in the server's shared_buffers (so the
    first ANN scan reads it off disk). A throwaway search warms both and also
    lets Postgres plan the query once.

    The probe vector is read back out of the table rather than embedded, so
    this costs zero API tokens. Cast to text in SQL because the driver may hand
    a vector back as a type whose repr pgvector cannot parse.

    Best-effort by design: an empty corpus or a database that is still coming
    up must not stop the service from serving.
    """
    try:
        async with session_scope() as session:
            # Opens the pool even when there is nothing indexed yet.
            await session.execute(text("SELECT 1"))
            probe = (
                await session.execute(
                    text("SELECT embedding::text FROM document_chunks "
                         "WHERE embedding IS NOT NULL LIMIT 1")
                )
            ).scalar()
            if probe is None:
                log.info("warm-up: pool opened, no chunks indexed yet")
                return
            await session.execute(
                text("SELECT 1 FROM document_chunks ORDER BY embedding <=> :q LIMIT 1"),
                {"q": probe},
            )
        log.info("warm-up: connection pool and vector index primed")
    except Exception as exc:  # noqa: BLE001
        log.warning("warm-up skipped: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the embedding + LLM clients once per process and reuse their
    connection pools across all requests. Saves a TLS handshake (or two) per
    /query call."""
    app.state.embedder = EmbeddingClient()
    app.state.llm = LLMClient()
    await _warm_retrieval()
    purge = asyncio.create_task(_purge_answer_log_daily())
    log.info("query service ready (embedder + llm clients initialized)")
    try:
        yield
    finally:
        purge.cancel()
        await app.state.embedder.aclose()
        await app.state.llm.aclose()


app = FastAPI(title="RAG Query Service", version="0.1.0", lifespan=lifespan)

SYSTEM_PROMPT = (
    "You are a warm, helpful assistant that answers questions from the user's documents. "
    "Use a friendly, conversational tone - clear and human, never robotic - but stay focused "
    "and avoid filler. Answer ONLY using the provided sources; never invent facts or rely on "
    "outside knowledge. Do NOT include bracketed citation markers like [1], [2] in your "
    "answer; the UI shows downloadable source documents separately. If the answer is not in "
    "the sources, say so honestly and kindly in your first sentence, and suggest what the "
    "user could try next (rephrasing the question, or sharing a document that covers it). "
    "Never explain the topic, define terms or fill gaps from general knowledge - not even as "
    "background before saying the sources do not cover it."
)

SYSTEM_PROMPT_VOICE = (
    "You are a warm, helpful voice assistant that answers from the user's documents. Your "
    "reply is going to be spoken aloud, so write it the way a person talks, not the way a "
    "document reads.\n"
    "- Aim for 40 words. Never exceed 3 sentences.\n"
    "- One idea per sentence. No lists, no headings, no bullet points, no parentheses, no "
    "bracketed citation markers like [1] - the screen shows the sources separately.\n"
    "- Write numbers, prices, dates and reference numbers the way you would say them out "
    "loud, in the language you are answering in, so they can be read straight off the page "
    "by a speech synthesiser.\n"
    "- Answer ONLY from the provided sources and never invent facts. If the answer is not "
    "there, say so in one sentence and suggest what to try next.\n"
    "- Give the single most useful fact first. The user can simply ask a follow-up question "
    "for anything more, so do not try to cover everything at once."
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

ANSWER_MAX_TOKENS = 800
# 0, so the same question gets the same answer. At the provider's default (1.0
# for OpenAI) an answer the documents covered sometimes opened with "the sources
# don't say" and then answered anyway - a coin toss per question, not a matter
# of wording: rewording the prompt against it made answers stretch related rules
# into a "yes" instead.
ANSWER_TEMPERATURE = 0.0
# A spoken answer has to be short, and asking the prompt nicely does not achieve
# that - a model handed six retrieved chunks and 800 tokens of headroom writes an
# essay whatever the system prompt says. The ceiling is the enforcement; the
# wording in SYSTEM_PROMPT_VOICE only shapes what fits inside it. ~40 words.
VOICE_MAX_TOKENS = 160


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
    # The asker's market (ISO country code). Answers then draw on documents for
    # everyone plus documents for that market; None searches everything.
    market: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    # Prior turns, oldest first, excluding the current question.
    history: list[ChatTurn] = Field(default_factory=list, max_length=12)
    # False keeps test traffic (eval/run.py) out of the question log. The web
    # service never sends it, so the chat cannot opt out of being counted.
    log: bool = True


def _system_prompt_for(req: QueryRequest) -> str:
    base = SYSTEM_PROMPT_VOICE if req.voice else SYSTEM_PROMPT
    if req.history:
        base += (
            " Earlier turns of this conversation are included for context: use them to "
            "understand what the user is referring to, but ground every fact in the sources "
            "provided with the current question."
        )
    # Always last, so nothing after it - sources in Swedish, a market in Norway -
    # can pull the answer into another language.
    return f"{base} {answer_language_rule(req.question, req.lang)}"


def _max_tokens_for(req: QueryRequest) -> int:
    return VOICE_MAX_TOKENS if req.voice else ANSWER_MAX_TOKENS


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


class CitationAudience(BaseModel):
    """Who a cited document applies to. Country codes, so the page can name them
    in the reader's language, and group names - never group members."""

    countries: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)


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
    # None for a document that is for everyone.
    audience: CitationAudience | None = None


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    # Set only when a follow-up was condensed into a standalone search question.
    search_query: str | None = None
    # For feedback on this answer (POST /answers/{answer_id}/feedback).
    answer_id: str | None = None


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
    market_clause, market_params = market_filter(req.market, req.document_id)
    if market_clause:
        clauses.append(market_clause)
        params.update(market_params)
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
    llm: LLMClient | None = None,
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
    # With re-ranking, a wider fused pool goes to the model, which picks the
    # top_k; without it, the fused order is cut straight to top_k.
    reranking = settings.rerank_provider == "llm" and llm is not None
    limit = max(top_k, settings.rerank_pool) if reranking else top_k
    where, params = _build_filter(req)
    params.update(
        {
            "q": str(q_vec),
            "qtext": query_text,
            "pool": max(top_k, settings.retrieval_pool_size),
            "limit": limit,
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
        LIMIT :limit
    """
    async with session_scope() as session:
        rows = (await session.execute(text(sql), params)).mappings().all()

    if not rows:
        return [], None

    audiences, chosen_market = await _audience_context(rows, req)
    if reranking:
        labels = [prompt_label(audiences.get(str(r["document_id"]))) for r in rows]
        rows = await rerank(
            llm, query_text, rows, labels, top_k,
            timeout=settings.rerank_timeout_s, max_chars=settings.rerank_passage_chars,
        )
    else:
        rows = rows[:top_k]
    return _format_sources(req, rows, audiences, chosen_market)


def _format_sources(
    req: QueryRequest,
    rows: list[Any],
    audiences: dict[str, dict[str, list]],
    chosen_market: str | None,
) -> tuple[list[Citation], str]:
    """The citations and the user message for the retrieved rows. Pure, so the
    "no markets, no change" promise below is testable without a database."""
    # Marks and guidance only when something is limited to a market: a corpus
    # without markets, or an answer drawn only from documents for everyone, gets
    # exactly the prompt it always did.
    limited = any(prompt_label(audiences.get(str(r["document_id"]))) for r in rows)

    blocks: list[str] = []
    citations: list[Citation] = []
    for i, r in enumerate(rows, start=1):
        audience = audiences.get(str(r["document_id"]))
        loc = f"{r['original_filename']}"
        if r["page_number"]:
            loc += f", p.{r['page_number']}"
        if r["heading"]:
            loc += f" — {r['heading']}"
        header = f"[{i}] ({loc})"
        if limited:
            header += f" — applies to: {prompt_label(audience) or 'everyone'}"
        blocks.append(f"{header}\n{r['content']}")
        browser_audience = citation_audience(audience)
        citations.append(
            Citation(
                n=i,
                document_id=str(r["document_id"]),
                filename=r["original_filename"],
                page_number=r["page_number"],
                heading=r["heading"],
                score=float(r["score"]),
                retrieval=_retrieval_label(r["in_dense"], r["in_lexical"]),
                audience=CitationAudience(**browser_audience) if browser_audience else None,
            )
        )

    # Ahead of the sources: after them, an answer on All markets followed source
    # [1] and gave Sweden's mileage rate alone, with the Group's rate right there.
    guidance = market_guidance(chosen_market) + "\n\n" if limited else ""
    user_msg = (
        f"Question: {req.question}\n\n" + guidance +
        "Sources:\n" + "\n\n".join(blocks) + "\n\n"
        "Please answer the question using only these sources, in a warm and conversational "
        "tone. Do not include [#] citation markers in the answer."
    )
    return citations, user_msg


# While the markets tables are missing, labels are skipped instead of looked up
# on every question; checked again after this many seconds, so applying the
# migration by hand to a running stack takes effect without a restart.
_AUDIENCE_RETRY_SECONDS = 600
_audience_retry_at = 0.0


async def _audience_context(
    rows: list[Any], req: QueryRequest
) -> tuple[dict[str, dict[str, list]], str | None]:
    """Who each retrieved document is for, and the asker's market by name.

    Its own session, so a database that predates the markets migration (Azure
    before migrate.sh) loses the labels but still answers - the main retrieval
    has already finished by now. A named document overrides the market, exactly
    as it does in market_filter, so no market is claimed for the asker then.
    """
    global _audience_retry_at
    if time.monotonic() < _audience_retry_at:
        return {}, None
    ids = list(dict.fromkeys(str(r["document_id"]) for r in rows))
    try:
        async with session_scope() as session:
            found = (await session.execute(text(AUDIENCE_LABELS_SQL), {"ids": ids})).mappings().all()
            chosen: str | None = None
            if req.market and not req.document_id:
                name = (
                    await session.execute(text(MARKET_NAME_SQL), {"code": req.market})
                ).scalar_one_or_none()
                chosen = name or req.market
    except ProgrammingError as exc:
        # 42P01 undefined_table: the markets tables are not there yet.
        orig = exc.orig
        if (getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)) != "42P01":
            raise
        log.warning("market tables missing - answering without market labels (apply migration 05)")
        _audience_retry_at = time.monotonic() + _AUDIENCE_RETRY_SECONDS
        return {}, None
    return audience_by_document(found), chosen


@app.post("/query", response_model=QueryResponse, dependencies=[Depends(require_service_key)])
async def query(req: QueryRequest, request: Request) -> QueryResponse:
    embedder: EmbeddingClient = request.app.state.embedder
    llm: LLMClient = request.app.state.llm

    search_query = await _rewrite_question(req, llm)
    rewritten = search_query if search_query != req.question else None
    citations, user_msg = await _retrieve_and_format(
        req, embedder, search_query=search_query, llm=llm
    )
    answer_id = _new_answer_id(req)
    if user_msg is None:
        _in_background(_record_answer(answer_id, req, search_query, NO_HITS_ANSWER, [], llm, no_hits=True))
        return QueryResponse(
            answer=NO_HITS_ANSWER, citations=[], search_query=rewritten, answer_id=answer_id
        )
    try:
        answer = await llm.complete(
            _system_prompt_for(req),
            user_msg,
            max_tokens=_max_tokens_for(req),
            history=_history_turns(req),
            temperature=ANSWER_TEMPERATURE,
        )
    except Exception as exc:
        raise HTTPException(502, f"llm call failed: {exc}") from exc
    answer = answer.strip()
    _in_background(_record_answer(answer_id, req, search_query, answer, citations, llm))
    return QueryResponse(
        answer=answer, citations=citations, search_query=rewritten, answer_id=answer_id
    )


@app.post("/query/stream", dependencies=[Depends(require_service_key)])
async def query_stream(req: QueryRequest, request: Request) -> StreamingResponse:
    """Same retrieval as /query, but streams the LLM answer as NDJSON.

    Wire format (one JSON object per line, application/x-ndjson):
      {"type":"rewrite","text":"..."}          # only when a follow-up was condensed
      {"type":"citations","data":[{...}, ...]}
      {"type":"delta","text":"..."}            # zero or more, in order
      {"type":"done","answer_id":"..."}        # answer_id: for feedback on it, when logged
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
            req, embedder, search_query=search_query, llm=llm
        )
    except HTTPException as exc:
        yield _ndjson({"type": "error", "message": str(exc.detail)})
        return

    yield _ndjson(
        {"type": "citations", "data": [c.model_dump() for c in citations]}
    )

    answer_id = _new_answer_id(req)
    done = {"type": "done", "answer_id": answer_id} if answer_id else {"type": "done"}
    if user_msg is None:
        yield _ndjson({"type": "delta", "text": NO_HITS_ANSWER})
        _in_background(_record_answer(answer_id, req, search_query, NO_HITS_ANSWER, [], llm, no_hits=True))
        yield _ndjson(done)
        return

    parts: list[str] = []
    try:
        async for chunk in llm.stream(
            _system_prompt_for(req),
            user_msg,
            max_tokens=_max_tokens_for(req),
            history=_history_turns(req),
            temperature=ANSWER_TEMPERATURE,
        ):
            if chunk:
                parts.append(chunk)
                yield _ndjson({"type": "delta", "text": chunk})
    except Exception as exc:  # noqa: BLE001
        log.exception("llm stream failed")
        yield _ndjson({"type": "error", "message": f"llm call failed: {exc}"})
        return

    # An answer cut short by the person (the stream closed early) never gets
    # here, so only answers they saw in full are logged. Logged before "done",
    # so a page that closes the stream on "done" cannot stop it.
    _in_background(_record_answer(answer_id, req, search_query, "".join(parts).strip(), citations, llm))
    yield _ndjson(done)


# --- The question log (answer_log.py) ---------------------------------------------------
# Never slows or breaks an answer: it runs after the answer has been sent, in
# the background, and any failure is a warning. Before migration 06 the table is
# missing; that is said once and retried every ten minutes.

_LOG_RETRY_SECONDS = 600
_log_retry_at = 0.0
# Background tasks are only weakly referenced by the event loop; keep them
# until they finish.
_background: set[asyncio.Task] = set()


def _new_answer_id(req: QueryRequest) -> str | None:
    """The id an answer is rated by - none when it is not logged (the log is
    off, or the caller asked), so nothing offers a rating that would go nowhere."""
    return str(uuid.uuid4()) if settings.answer_log_enabled and req.log else None


def _in_background(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _undefined_table(exc: ProgrammingError) -> bool:
    orig = exc.orig
    return (getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)) == "42P01"


async def _record_answer(
    answer_id: str | None,
    req: QueryRequest,
    search_query: str,
    answer: str,
    citations: list[Citation],
    llm: LLMClient,
    no_hits: bool = False,
) -> None:
    """Store one answer, then check whether it answered the question."""
    global _log_retry_at
    if answer_id is None or time.monotonic() < _log_retry_at:
        return
    try:
        async with session_scope() as session:
            await session.execute(
                text(INSERT_SQL),
                {
                    "id": answer_id,
                    "question": req.question,
                    "search_query": search_query if search_query != req.question else None,
                    "lang": req.lang or detect_language(req.question),
                    "market": None if req.document_id else req.market,
                    "document_id": req.document_id,
                    "voice": bool(req.voice),
                    "answer": answer[:ANSWER_MAX_CHARS],
                    "sources": json.dumps(sources_of(citations)),
                    "coverage": "no_hits" if no_hits else None,
                },
            )
    except ProgrammingError as exc:
        if not _undefined_table(exc):
            log.warning("could not log an answer: %s", exc)
            return
        log.warning("answer_log table missing - questions are not being logged (apply migration 06)")
        _log_retry_at = time.monotonic() + _LOG_RETRY_SECONDS
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("could not log an answer: %s", exc)
        return
    if no_hits or not answer:
        return
    try:
        reply = await llm.complete(
            COVERAGE_SYSTEM,
            coverage_prompt(req.question, answer),
            max_tokens=COVERAGE_MAX_TOKENS,
            temperature=0,
            timeout=COVERAGE_TIMEOUT_S,
        )
        coverage = parse_coverage(reply)
        if coverage:
            async with session_scope() as session:
                await session.execute(text(COVERAGE_SQL), {"coverage": coverage, "id": answer_id})
    except Exception as exc:  # noqa: BLE001
        log.warning("could not check whether an answer answered: %s", exc)


async def _purge_answer_log() -> None:
    try:
        async with session_scope() as session:
            result = await session.execute(text(PURGE_SQL), {"days": settings.answer_log_days})
        if result.rowcount:
            log.info("deleted %d logged answers older than %d days", result.rowcount, settings.answer_log_days)
    except ProgrammingError as exc:
        if not _undefined_table(exc):
            log.warning("could not purge the question log: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not purge the question log: %s", exc)


async def _purge_answer_log_daily() -> None:
    # Runs even with the log turned off, so turning it off also clears it out.
    while True:
        await _purge_answer_log()
        await asyncio.sleep(24 * 3600)


class FeedbackBody(BaseModel):
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=500)


@app.post("/answers/{answer_id}/feedback", dependencies=[Depends(require_service_key)])
async def answer_feedback(answer_id: uuid.UUID, body: FeedbackBody) -> dict[str, Any]:
    """A thumb up or down on one answer, with an optional word on what was missing."""
    if not settings.answer_log_enabled:
        raise HTTPException(404, "the question log is turned off")
    comment = " ".join((body.comment or "").split())[:500] or None
    for _ in range(3):
        try:
            async with session_scope() as session:
                result = await session.execute(
                    text(FEEDBACK_SQL), {"rating": body.rating, "comment": comment, "id": str(answer_id)}
                )
        except ProgrammingError as exc:
            if _undefined_table(exc):
                raise HTTPException(503, "the question log is not set up (apply migration 06)") from None
            raise
        if result.rowcount:
            return {"ok": True}
        # The row is written just after the answer; a very quick click can beat it.
        await asyncio.sleep(1)
    raise HTTPException(404, "answer not found")


@app.get("/insights", dependencies=[Depends(require_service_key)])
async def insights(
    days: int = Query(default=30, ge=1, le=365),
    market: str = Query(default="", pattern=r"^([A-Z]{2}|none)?$"),
    kind: Literal["attention", "all"] = "attention",
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """What people asked over `days`: totals, and the questions grouped - by
    default only those that were not (fully) answered or got a thumbs down.
    `market` narrows to questions asked for one market, or "none" for those
    asked for all markets."""
    clause, params = "", {"days": days, "cap": INSIGHTS_ROW_CAP}
    if market == "none":
        clause = "AND market IS NULL"
    elif market:
        clause, params["market"] = "AND market = :market", market
    try:
        async with session_scope() as session:
            found = (await session.execute(text(INSIGHTS_SQL.format(market=clause)), params)).mappings().all()
            rows = []
            for r in found:
                row = dict(r)
                if isinstance(row.get("sources"), str):
                    row["sources"] = json.loads(row["sources"])
                rows.append(row)
            ids = cited_document_ids(rows)
            if ids:
                names = (await session.execute(text(CURRENT_NAMES_SQL), {"ids": ids})).all()
                rows = current_sources(rows, {doc_id: name for doc_id, name in names})
    except ProgrammingError as exc:
        if not _undefined_table(exc):
            raise
        return {"enabled": settings.answer_log_enabled, "ready": False, "days": days, "market": market,
                "summary": summarize([]), "groups": [], "truncated": False}
    return {
        "enabled": settings.answer_log_enabled,
        "ready": True,
        "days": days,
        "market": market,
        "retention_days": settings.answer_log_days,
        "summary": summarize(rows),
        "groups": group_questions(rows, attention_only=kind == "attention")[:limit],
        "truncated": len(rows) >= INSIGHTS_ROW_CAP,
    }
