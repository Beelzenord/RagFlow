from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, Response
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from rag_shared.db import session_scope
from rag_shared.security import require_service_key
from rag_shared.settings import settings

from .evidence import render_evidence
from .pipeline import run_ingestion
from .storage import (
    evidence_path,
    purge_evidence,
    resolve_storage_file,
    save_original,
    write_atomic,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ingestion")

app = FastAPI(title="RAG Ingestion Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _validate_upload(file: UploadFile, size: int) -> None:
    mime = (file.content_type or "").lower()
    if mime not in settings.allowed_mime_set:
        raise HTTPException(415, f"unsupported content type: {mime!r}")
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if size > max_bytes:
        raise HTTPException(413, f"file exceeds {settings.max_upload_mb} MB limit")


@app.post("/ingest", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_service_key)])
async def ingest(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: str | None = Form(default=None),
    collection: str | None = Form(default=None),
) -> dict[str, Any]:
    content = await file.read()
    _validate_upload(file, len(content))

    async with session_scope() as session:
        row = await session.execute(
            text(
                "INSERT INTO documents (original_filename, file_type, storage_path, "
                "user_id, collection, status) "
                "VALUES (:n, :t, :p, :u, :c, 'uploaded') RETURNING id"
            ),
            {
                "n": file.filename or "upload.bin",
                "t": file.content_type,
                "p": "",  # filled below once we know the path
                "u": user_id,
                "c": collection,
            },
        )
        document_id = str(row.scalar_one())
        storage_path = save_original(document_id, file.filename or "upload.bin", content)
        await session.execute(
            text("UPDATE documents SET storage_path = :p WHERE id = :id"),
            {"p": storage_path, "id": document_id},
        )

    background.add_task(run_ingestion, document_id, storage_path, file.content_type or "")
    return {"document_id": document_id, "status": "processing"}


@app.get("/documents/{document_id}", dependencies=[Depends(require_service_key)])
async def get_document(document_id: UUID) -> dict[str, Any]:
    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, original_filename, file_type, status, error_message, "
                    "       collection, user_id, created_at, updated_at, "
                    "       (SELECT count(*) FROM document_chunks WHERE document_id = d.id) AS chunk_count "
                    "FROM documents d WHERE id = :id"
                ),
                {"id": str(document_id)},
            )
        ).mappings().first()
    if not row:
        raise HTTPException(404, "document not found")
    return dict(row)


@app.get("/documents/{document_id}/file", dependencies=[Depends(require_service_key)])
async def get_document_file(document_id: UUID) -> FileResponse:
    """Stream the stored original upload for download."""
    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    "SELECT storage_path, original_filename, file_type "
                    "FROM documents WHERE id = :id"
                ),
                {"id": str(document_id)},
            )
        ).mappings().first()
    if not row:
        raise HTTPException(404, "document not found")

    path = resolve_storage_file(row["storage_path"] or "")
    if path is None:
        raise HTTPException(404, "original file not found on disk")

    filename = row["original_filename"] or path.name
    media_type = (row["file_type"] or "").strip() or "application/octet-stream"
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="attachment",
    )


# Serialises the CPU-bound renders. A browser requests a whole strip of tiles
# at once, and this service also has parsing work to get on with.
_render_slots = asyncio.Semaphore(settings.evidence_max_concurrency)

# Keyed by an immutable chunk id, so a cached tile is never stale: re-ingesting
# mints new chunk ids and the old URLs stop being referenced at all.
_EVIDENCE_CACHE_CONTROL = "private, max-age=604800"


@app.get("/chunks/{chunk_id}/evidence", dependencies=[Depends(require_service_key)])
async def get_chunk_evidence(
    chunk_id: UUID,
    variant: Literal["page", "crop"] = Query(default="page"),
) -> Response:
    """PNG of the page this chunk came from, with its text highlighted.

    404 covers every "no picture available" case - disabled, not a PDF, no
    stored original, an unlocatable page - because the caller's only sensible
    response to all of them is the same: show no tile.
    """
    if not settings.evidence_enabled:
        raise HTTPException(404, "evidence images are disabled")

    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    "SELECT c.document_id, c.page_number, c.content, "
                    "       d.storage_path, d.file_type "
                    "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE c.id = :id"
                ),
                {"id": str(chunk_id)},
            )
        ).mappings().first()
    if not row:
        raise HTTPException(404, "chunk not found")

    document_id = str(row["document_id"])
    cached = evidence_path(document_id, str(chunk_id), variant)
    if not cached.is_file():
        source = resolve_storage_file(row["storage_path"] or "")
        if source is None:
            raise HTTPException(404, "original file not found on disk")

        async with _render_slots:
            png = await run_in_threadpool(
                render_evidence,
                str(source),
                row["page_number"] or 1,
                row["content"] or "",
                variant,
                row["file_type"] or "",
            )
        if png is None:
            raise HTTPException(404, "no evidence image available for this chunk")
        write_atomic(cached, png)
        # A failed cache write is not fatal; serve this render from memory and
        # let the next request try again.
        if not cached.is_file():
            return Response(
                content=png,
                media_type="image/png",
                headers={"cache-control": _EVIDENCE_CACHE_CONTROL},
            )

    return FileResponse(
        path=cached,
        media_type="image/png",
        headers={"cache-control": _EVIDENCE_CACHE_CONTROL},
    )


@app.get("/documents", dependencies=[Depends(require_service_key)])
async def list_documents(
    collection: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if collection:
        clauses.append("d.collection = :collection")
        params["collection"] = collection
    if user_id:
        clauses.append("d.user_id = :user_id")
        params["user_id"] = user_id
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT id, original_filename, file_type, status, error_message, "
        "       collection, user_id, created_at, updated_at, "
        "       (SELECT count(*) FROM document_chunks WHERE document_id = d.id) AS chunk_count "
        f"FROM documents d {where} "
        "ORDER BY created_at DESC LIMIT :limit"
    )
    async with session_scope() as session:
        rows = (await session.execute(text(sql), params)).mappings().all()
    return {"documents": [dict(r) for r in rows]}


def _delete_files(document_id: str, storage_path: str | None) -> None:
    """Best-effort removal of original + markdown files. Never raises."""
    candidates = []
    if storage_path:
        candidates.append(Path(storage_path))
    candidates.append(Path(settings.storage_dir) / "markdown" / f"{document_id}.md")
    for p in candidates:
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not unlink %s: %s", p, exc)
    purge_evidence(document_id)


@app.delete("/documents/{document_id}", dependencies=[Depends(require_service_key)])
async def delete_document(document_id: UUID) -> dict[str, Any]:
    async with session_scope() as session:
        row = (
            await session.execute(
                text("SELECT storage_path FROM documents WHERE id = :id"),
                {"id": str(document_id)},
            )
        ).first()
        if not row:
            raise HTTPException(404, "document not found")
        storage_path = row[0]
        await session.execute(
            text("DELETE FROM documents WHERE id = :id"),
            {"id": str(document_id)},
        )

    _delete_files(str(document_id), storage_path)
    log.info("deleted document_id=%s", document_id)
    return {"document_id": str(document_id), "deleted": True}


@app.post("/documents/{document_id}/reprocess", dependencies=[Depends(require_service_key)])
async def reprocess(document_id: UUID, background: BackgroundTasks) -> dict[str, Any]:
    async with session_scope() as session:
        row = (
            await session.execute(
                text("SELECT storage_path, file_type FROM documents WHERE id = :id"),
                {"id": str(document_id)},
            )
        ).first()
        if not row:
            raise HTTPException(404, "document not found")
        storage_path, file_type = row
        await session.execute(
            text("UPDATE documents SET status='processing', error_message=NULL WHERE id = :id"),
            {"id": str(document_id)},
        )
    background.add_task(run_ingestion, str(document_id), storage_path, file_type or "")
    return {"document_id": str(document_id), "status": "processing"}


@app.post("/documents/reprocess-all", dependencies=[Depends(require_service_key)])
async def reprocess_all(background: BackgroundTasks) -> dict[str, Any]:
    """Re-run the full ingestion pipeline for every document with a stored
    original. Intended for one-off migrations (e.g. after changing the
    chunker or the embedding-input format). FastAPI BackgroundTasks runs
    the queued tasks sequentially after the response returns, which also
    keeps LlamaParse rate-limit pressure low."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, storage_path, file_type FROM documents "
                    "WHERE storage_path <> ''"
                )
            )
        ).all()
        for r in rows:
            await session.execute(
                text(
                    "UPDATE documents SET status='processing', error_message=NULL "
                    "WHERE id = :id"
                ),
                {"id": str(r[0])},
            )
    for r in rows:
        background.add_task(run_ingestion, str(r[0]), r[1], r[2] or "")
    log.info("reprocess-all queued %d documents", len(rows))
    return {"queued": len(rows)}
