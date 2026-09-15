from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any
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
from fastapi.responses import FileResponse
from sqlalchemy import text

from rag_shared.db import session_scope
from rag_shared.security import require_service_key
from rag_shared.settings import settings

from .pipeline import run_ingestion
from .scoping import ScopeError, resolve_scope
from .storage import resolve_storage_file, save_original

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
    scope: str | None = Form(default=None),
) -> dict[str, Any]:
    """`scope` is a comma-separated list of region or group codes, e.g. "EU,CH".

    Optional here on purpose. The console requires a choice, because that is
    where a person is deciding; the API does not, so smoke_test.sh and the n8n
    ingest workflow keep working. An upload with no scope is global and is
    listed as unscoped rather than quietly treated as deliberate.
    """
    content = await file.read()
    _validate_upload(file, len(content))

    async with session_scope() as session:
        # Before the row is written: a rejected scope must not leave a document
        # behind, and the expansion needs the same session to read the
        # vocabulary tables.
        try:
            resolved = await resolve_scope(session, scope)
        except ScopeError as exc:
            raise HTTPException(400, str(exc)) from exc

        row = await session.execute(
            text(
                "INSERT INTO documents (original_filename, file_type, storage_path, "
                "user_id, collection, status, "
                "applies_to_regions, scope_entries, scope_labels) "
                "VALUES (:n, :t, :p, :u, :c, 'uploaded', "
                ":regions, CAST(:entries AS jsonb), :labels) RETURNING id"
            ),
            {
                "n": file.filename or "upload.bin",
                "t": file.content_type,
                "p": "",  # filled below once we know the path
                "u": user_id,
                "c": collection,
                "regions": resolved.regions,
                "entries": json.dumps(resolved.entries),
                "labels": resolved.labels,
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


@app.get("/scopes", dependencies=[Depends(require_service_key)])
async def list_scopes() -> dict[str, Any]:
    """The scope vocabulary the console's picker is built from.

    Served from the tables rather than a constant so that editing the
    vocabulary is a migration, not a frontend release - and so the picker can
    never offer a code that resolve_scope would then refuse.
    """
    async with session_scope() as session:
        groups = [
            dict(r)
            for r in (
                await session.execute(
                    text(
                        "SELECT code, label, specificity, member_regions "
                        "FROM region_groups ORDER BY specificity, label"
                    )
                )
            ).mappings()
        ]
        regions = [
            dict(r)
            for r in (
                await session.execute(
                    text("SELECT code, label FROM regions ORDER BY label")
                )
            ).mappings()
        ]
    return {"groups": groups, "regions": regions}


@app.get("/documents/{document_id}", dependencies=[Depends(require_service_key)])
async def get_document(document_id: UUID) -> dict[str, Any]:
    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, original_filename, file_type, status, error_message, "
                    "       collection, user_id, created_at, updated_at, "
                    "       applies_to_regions, scope_entries, scope_labels, sensitivity, "
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
        "       applies_to_regions, scope_entries, scope_labels, sensitivity, "
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
