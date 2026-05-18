"""End-to-end ingestion: parse → chunk → embed → persist. Runs in a background
task. All heavy work happens here — never at query time."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from sqlalchemy import text

from rag_shared.db import session_scope
from rag_shared.embeddings import EmbeddingClient
from rag_shared.chunking import chunk_markdown
from .parser import parse_to_markdown
from .storage import save_markdown

log = logging.getLogger(__name__)


async def _set_doc_status(session, document_id: str, status: str, error: str | None = None) -> None:
    await session.execute(
        text(
            "UPDATE documents SET status = :s, error_message = :e WHERE id = :id"
        ),
        {"s": status, "e": error, "id": document_id},
    )


async def _start_job(session, document_id: str) -> str:
    row = await session.execute(
        text(
            "INSERT INTO ingestion_jobs (document_id, status, started_at) "
            "VALUES (:d, 'running', now()) RETURNING id"
        ),
        {"d": document_id},
    )
    return str(row.scalar_one())


async def _finish_job(session, job_id: str, status: str, error: str | None = None) -> None:
    await session.execute(
        text(
            "UPDATE ingestion_jobs SET status = :s, completed_at = now(), error_message = :e "
            "WHERE id = :id"
        ),
        {"s": status, "e": error, "id": job_id},
    )


async def run_ingestion(document_id: str, storage_path: str, file_type: str) -> None:
    """Top-level pipeline. Always finalizes the document + job rows, even on failure."""
    job_id: str | None = None
    embedder: EmbeddingClient | None = None
    try:
        # 1. Mark processing + open job, and grab the filename for embedding context.
        async with session_scope() as session:
            await _set_doc_status(session, document_id, "processing")
            job_id = await _start_job(session, document_id)
            row = await session.execute(
                text("SELECT original_filename FROM documents WHERE id = :id"),
                {"id": document_id},
            )
            original_filename = row.scalar_one_or_none() or ""

        # 2. Parse to markdown (network-bound to LlamaCloud)
        markdown = await parse_to_markdown(storage_path, file_type)
        if not markdown.strip():
            raise RuntimeError("LlamaParse returned empty markdown")
        md_path = save_markdown(document_id, markdown)

        # 3. Chunk
        chunks = chunk_markdown(markdown)
        if not chunks:
            raise RuntimeError("Chunker produced no chunks")

        # 4. Embed (batched). The embedding input is enriched with filename +
        # heading so the vector "knows" where the chunk sits; the stored
        # `content` column stays raw so the LLM prompt is unchanged.
        embedder = EmbeddingClient()
        BATCH = 64
        embeddings: list[list[float]] = []
        for i in range(0, len(chunks), BATCH):
            batch = [
                (
                    f"{original_filename}\n# {c.heading}\n\n{c.content}"
                    if c.heading
                    else f"{original_filename}\n\n{c.content}"
                )
                for c in chunks[i : i + BATCH]
            ]
            embeddings.extend(await embedder.embed(batch))

        # 5. Persist atomically: wipe old chunks, insert markdown + new chunks
        async with session_scope() as session:
            await session.execute(
                text("DELETE FROM document_chunks WHERE document_id = :d"),
                {"d": document_id},
            )
            await session.execute(
                text(
                    "UPDATE documents SET markdown_text = :t, markdown_storage_path = :p "
                    "WHERE id = :id"
                ),
                {"t": markdown, "p": md_path, "id": document_id},
            )
            for chunk, vec in zip(chunks, embeddings):
                await session.execute(
                    text(
                        "INSERT INTO document_chunks "
                        "(document_id, chunk_index, page_number, heading, content, "
                        " token_count, embedding, metadata) "
                        "VALUES (:d, :i, :p, :h, :c, :t, :e, CAST(:m AS jsonb))"
                    ),
                    {
                        "d": document_id,
                        "i": chunk.index,
                        "p": chunk.page_number,
                        "h": chunk.heading,
                        "c": chunk.content,
                        "t": chunk.token_count,
                        "e": str(vec),  # pgvector accepts the textual "[..]" form
                        "m": "{}",
                    },
                )
            await _set_doc_status(session, document_id, "completed")
            await _finish_job(session, job_id, "completed")

        log.info("ingestion completed document_id=%s chunks=%d", document_id, len(chunks))

    except Exception as exc:  # noqa: BLE001
        log.exception("ingestion failed document_id=%s", document_id)
        async with session_scope() as session:
            await _set_doc_status(session, document_id, "failed", str(exc)[:1000])
            if job_id:
                await _finish_job(session, job_id, "failed", str(exc)[:1000])
    finally:
        if embedder is not None:
            await embedder.aclose()
