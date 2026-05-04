"""Query service. Path is intentionally minimal — every expensive operation
already happened during ingestion."""
from __future__ import annotations
import logging
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from rag_shared.db import session_scope
from rag_shared.embeddings import EmbeddingClient
from rag_shared.llm import LLMClient
from rag_shared.security import require_service_key
from rag_shared.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("query")

app = FastAPI(title="RAG Query Service", version="0.1.0")

SYSTEM_PROMPT = (
    "You are a precise document-grounded assistant. Answer ONLY using the provided sources. "
    "Cite sources inline as [#] using the source numbers shown. If the answer is not in the "
    "sources, say so plainly."
)


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    document_id: str | None = None
    collection: str | None = None
    user_id: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)


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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _build_filter(req: QueryRequest) -> tuple[str, dict[str, Any]]:
    clauses = ["d.status = 'completed'"]
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


@app.post("/query", response_model=QueryResponse, dependencies=[Depends(require_service_key)])
async def query(req: QueryRequest) -> QueryResponse:
    embedder = EmbeddingClient()
    llm = LLMClient()
    try:
        q_vec = await embedder.embed_one(req.question)
    except Exception as exc:
        await embedder.aclose()
        await llm.aclose()
        raise HTTPException(502, f"embedding failed: {exc}") from exc

    top_k = req.top_k or settings.retrieval_top_k
    where, params = _build_filter(req)
    params["q"] = str(q_vec)
    params["k"] = top_k
    params["min_score"] = settings.retrieval_min_score

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

    rows = [r for r in rows if r["score"] >= settings.retrieval_min_score]
    if not rows:
        await embedder.aclose()
        await llm.aclose()
        return QueryResponse(
            answer="I couldn't find anything relevant in the indexed documents.",
            citations=[],
        )

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
        "Answer the question using only these sources. Cite with [#]."
    )

    try:
        answer = await llm.complete(SYSTEM_PROMPT, user_msg, max_tokens=800)
    except Exception as exc:
        raise HTTPException(502, f"llm call failed: {exc}") from exc
    finally:
        await embedder.aclose()
        await llm.aclose()

    return QueryResponse(answer=answer.strip(), citations=citations)
