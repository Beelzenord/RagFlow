"""Query service. Path is intentionally minimal — every expensive operation
already happened during ingestion."""
from __future__ import annotations

import asyncio
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
    /query call. The reranker model is also loaded once (heavyweight: ~600 MB
    on disk, ~1-2 GB resident) and held on app.state; if it can't be loaded
    (model missing, torch not installed) we degrade cleanly to ANN-only."""
    app.state.embedder = EmbeddingClient()
    app.state.llm = LLMClient()
    app.state.reranker = None
    if settings.reranker_enabled:
        try:
            from .reranker import Reranker

            app.state.reranker = Reranker()
            log.info("reranker ready (%s)", settings.reranker_model)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "reranker disabled: failed to load %s: %s",
                settings.reranker_model,
                exc,
            )
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
    score: float


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


async def _retrieve_and_format(
    req: QueryRequest,
    embedder: EmbeddingClient,
    reranker: Any | None = None,
    search_query: str | None = None,
) -> tuple[list[Citation], str | None]:
    """Run embedding + vector search, optionally rerank, and build the LLM
    user prompt.

    Returns (citations, user_prompt). user_prompt is None when no rows match,
    in which case the caller should short-circuit with NO_HITS_ANSWER.

    `search_query` is what gets embedded and reranked (the standalone rewrite of
    a follow-up); the prompt still shows the user's own wording.

    When a reranker is supplied, we pull a larger ANN pool (retrieval_pool_size)
    and let the cross-encoder pick the final top_k. The min_score gate is
    always applied to the **vector cosine** score (cross-encoder scores are
    unbounded and not comparable).
    """
    query_text = search_query or req.question
    try:
        q_vec = await embedder.embed_one(query_text)
    except Exception as exc:
        raise HTTPException(502, f"embedding failed: {exc}") from exc

    top_k = req.top_k or settings.retrieval_top_k
    pool = max(top_k, settings.retrieval_pool_size) if reranker else top_k
    where, params = _build_filter(req)
    params["q"] = str(q_vec)
    params["k"] = pool

    sql = f"""
        SELECT  c.id            AS chunk_id,
                c.document_id,
                c.chunk_index,
                c.page_number,
                c.heading,
                c.content,
                d.original_filename,
                1 - (c.embedding <=> :q) AS score
        FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE {where}
        ORDER BY c.embedding <=> :q
        LIMIT :k
    """
    async with session_scope() as session:
        rows = (await session.execute(text(sql), params)).mappings().all()

    # Gate on vector cosine before reranking so obvious junk never enters the
    # cross-encoder (saves time) and never reaches the LLM (saves accuracy).
    rows = [r for r in rows if r["score"] >= settings.retrieval_min_score]
    if not rows:
        return [], None

    if reranker and len(rows) > top_k:
        # CrossEncoder.predict is CPU-bound; offload so we don't stall the
        # event loop for other concurrent requests.
        rows = await asyncio.to_thread(
            reranker.rerank, query_text, list(rows), top_k
        )
    else:
        rows = list(rows[:top_k])

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
    reranker = request.app.state.reranker

    search_query = await _rewrite_question(req, llm)
    rewritten = search_query if search_query != req.question else None
    citations, user_msg = await _retrieve_and_format(
        req, embedder, reranker, search_query=search_query
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
    reranker = request.app.state.reranker
    return StreamingResponse(
        _run_stream(req, embedder, llm, reranker),
        media_type="application/x-ndjson",
    )


def _ndjson(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


async def _run_stream(
    req: QueryRequest,
    embedder: EmbeddingClient,
    llm: LLMClient,
    reranker: Any | None = None,
) -> AsyncIterator[bytes]:
    search_query = await _rewrite_question(req, llm)
    if search_query != req.question:
        log.info("rewrote follow-up for retrieval: %r", search_query)
        yield _ndjson({"type": "rewrite", "text": search_query})

    try:
        citations, user_msg = await _retrieve_and_format(
            req, embedder, reranker, search_query=search_query
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
