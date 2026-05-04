from __future__ import annotations
import logging
from typing import Any
from uuid import UUID

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy import text

from rag_shared.db import session_scope
from rag_shared.security import require_service_key
from rag_shared.settings import settings

from .pipeline import run_ingestion
from .storage import save_original

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
