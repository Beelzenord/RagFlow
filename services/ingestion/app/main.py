from __future__ import annotations
import asyncio
import json
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
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from rag_shared.db import session_scope
from rag_shared.security import require_service_key
from rag_shared.settings import settings

from .fingerprint import fingerprint, fingerprint_file
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
    allow_duplicate: bool = Form(default=False),
) -> dict[str, Any]:
    """`scope` is a comma-separated list of region or group codes, e.g. "EU,CH".

    Optional here on purpose. The console requires a choice, because that is
    where a person is deciding; the API does not, so smoke_test.sh and the n8n
    ingest workflow keep working. An upload with no scope is global and is
    listed as unscoped rather than quietly treated as deliberate.

    A file whose bytes are already in the corpus is refused with 409 naming the
    document that holds them, unless `allow_duplicate` is set. Indexing it again
    would pay LlamaParse twice and put two copies of every chunk in competition
    for the same retrieval slots.
    """
    content = await file.read()
    _validate_upload(file, len(content))
    sha256 = fingerprint(content)

    async with session_scope() as session:
        if not allow_duplicate:
            # Held until this transaction commits, so two identical uploads
            # arriving together cannot both find "no match" and both insert.
            # Keyed on the hash: unrelated uploads never wait on each other.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:h, 0))"),
                {"h": sha256},
            )
            existing = (
                await session.execute(
                    text(
                        "SELECT id, original_filename FROM documents "
                        "WHERE content_sha256 = :h ORDER BY created_at LIMIT 1"
                    ),
                    {"h": sha256},
                )
            ).mappings().first()
            # Checked before the scope, because it makes the scope moot: there
            # is no point fixing a typo on a file that is already here.
            if existing:
                raise HTTPException(
                    409,
                    {
                        "message": f"already uploaded as {existing['original_filename']}",
                        "document_id": str(existing["id"]),
                        "original_filename": existing["original_filename"],
                    },
                )

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
                "user_id, collection, status, content_sha256, "
                "applies_to_regions, scope_entries, scope_labels) "
                "VALUES (:n, :t, :p, :u, :c, 'uploaded', :h, "
                ":regions, CAST(:entries AS jsonb), :labels) RETURNING id"
            ),
            {
                "n": file.filename or "upload.bin",
                "t": file.content_type,
                "p": "",  # filled below once we know the path
                "u": user_id,
                "c": collection,
                "h": sha256,
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
    never offer a code that resolve_scope would then refuse. That is also why
    only enabled countries are listed: the catalogue holds every ISO country,
    and offering a disabled one would be offering a guaranteed 400.
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
                    text("SELECT code, label FROM regions WHERE active ORDER BY label")
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
                    "       content_sha256, "
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


DOCUMENT_STATUSES = ("uploaded", "processing", "completed", "failed", "degraded")

# Attention order: what needs a person, most urgent first. A failed document is
# broken; an unscoped one is waiting on a decision; a degraded one parsed with
# repairs and is worth a look. Everything else follows, newest first.
_ATTENTION_ORDER = (
    "CASE "
    "WHEN d.status = 'failed' THEN 0 "
    "WHEN d.scope_entries = '[]'::jsonb THEN 1 "
    "WHEN d.status = 'degraded' THEN 2 "
    "ELSE 3 END, d.created_at DESC"
)
_SORTS = {
    "created_desc": "d.created_at DESC",
    "created_asc": "d.created_at ASC",
    "filename": "lower(d.original_filename) ASC, d.created_at DESC",
    "attention": _ATTENTION_ORDER,
}


def _like_pattern(term: str) -> str:
    """A substring match where % and _ in the search are literal characters.

    Filenames are full of underscores; unescaped, "2024_q1" would also match
    "2024-q1" and "2024xq1".
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@app.get("/documents", dependencies=[Depends(require_service_key)])
async def list_documents(
    q: str | None = Query(default=None, max_length=200),
    scope: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None),
    collection: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    sort: Literal["created_desc", "created_asc", "filename", "attention"] = Query(
        default="created_desc"
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """The corpus inventory, filterable and paged.

    With no parameters this answers exactly as it always has - newest first,
    up to 200 - which is what the console sidebar still asks for. The admin
    page uses the rest.

    `scope` matches what a document was *tagged* with: `EU` finds documents
    whose admin picked EU, not every document that happens to cover an EU
    country. `unscoped` finds the ones nobody has decided about yet.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if q and q.strip():
        clauses.append("d.original_filename ILIKE :q ESCAPE '\\'")
        params["q"] = _like_pattern(q.strip())
    if scope:
        if scope.strip().lower() == "unscoped":
            clauses.append("d.scope_entries = '[]'::jsonb")
        else:
            clauses.append("d.scope_entries @> CAST(:scope_match AS jsonb)")
            params["scope_match"] = json.dumps([{"code": scope.strip().upper()}])
    if status:
        if status not in DOCUMENT_STATUSES:
            raise HTTPException(400, f"status must be one of {', '.join(DOCUMENT_STATUSES)}")
        clauses.append("d.status = :status")
        params["status"] = status
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
        "       content_sha256, "
        "       (SELECT count(*) FROM document_chunks WHERE document_id = d.id) AS chunk_count, "
        # Other documents holding the same bytes - what the duplicate badge
        # shows. Zero when the fingerprint is unknown, never a guess.
        "       CASE WHEN d.content_sha256 IS NULL THEN 0 ELSE ("
        "           SELECT count(*) FROM documents d2 "
        "           WHERE d2.content_sha256 = d.content_sha256 AND d2.id <> d.id"
        "       ) END AS duplicate_count "
        f"FROM documents d {where} "
        f"ORDER BY {_SORTS[sort]} LIMIT :limit OFFSET :offset"
    )
    async with session_scope() as session:
        total = (
            await session.execute(text(f"SELECT count(*) FROM documents d {where}"), params)
        ).scalar_one()
        rows = (
            await session.execute(text(sql), {**params, "limit": limit, "offset": offset})
        ).mappings().all()
    return {
        "documents": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


class ScopeBody(BaseModel):
    scope: str = Field(..., max_length=500)


class BulkScopeBody(BaseModel):
    document_ids: list[UUID] = Field(..., min_length=1, max_length=500)
    scope: str = Field(..., max_length=500)


async def _resolve_for_retag(session, raw: str):
    """resolve_scope, with one extra rule for retagging.

    An empty scope is refused here although /ingest accepts one. On upload, no
    scope means a machine caller that does not know about scoping. On a retag
    it can only be a blank submit, and silently turning a deliberate tag back
    into "unscoped" is the mistake to prevent. Everywhere is GLOBAL.
    """
    if not raw or not raw.strip():
        raise HTTPException(400, "pick a scope - use GLOBAL for everywhere")
    try:
        return await resolve_scope(session, raw)
    except ScopeError as exc:
        raise HTTPException(400, str(exc)) from exc


_RETAG_SQL = (
    "UPDATE documents SET applies_to_regions = :regions, "
    "scope_entries = CAST(:entries AS jsonb), scope_labels = :labels "
)


def _retag_params(resolved) -> dict[str, Any]:
    return {
        "regions": resolved.regions,
        "entries": json.dumps(resolved.entries),
        "labels": resolved.labels,
    }


@app.patch("/documents/scope", dependencies=[Depends(require_service_key)])
async def retag_documents(body: BulkScopeBody) -> dict[str, Any]:
    """Retag many documents at once, all or nothing.

    One transaction, and every id is checked before anything is written: a bulk
    retag that half-lands leaves the admin unable to see which half did.

    Only the scope columns change. Scope is metadata, not vectors, so no
    document is re-parsed or re-embedded and chunk counts stay as they are.
    """
    ids = sorted({str(i) for i in body.document_ids})
    async with session_scope() as session:
        resolved = await _resolve_for_retag(session, body.scope)
        found = {
            str(r[0])
            for r in (
                await session.execute(
                    text("SELECT id FROM documents WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": ids},
                )
            ).all()
        }
        missing = [i for i in ids if i not in found]
        if missing:
            raise HTTPException(
                404, {"message": "some documents do not exist", "missing": missing}
            )
        await session.execute(
            text(_RETAG_SQL + "WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {**_retag_params(resolved), "ids": ids},
        )
    log.info("retagged %d documents to %s", len(ids), resolved.labels or "global")
    return {
        "updated": len(ids),
        "scope_labels": resolved.labels,
        "scope_entries": resolved.entries,
    }


@app.patch("/documents/{document_id}/scope", dependencies=[Depends(require_service_key)])
async def retag_document(document_id: UUID, body: ScopeBody) -> dict[str, Any]:
    """Change what one document applies to, without reprocessing it."""
    async with session_scope() as session:
        resolved = await _resolve_for_retag(session, body.scope)
        row = (
            await session.execute(
                text(_RETAG_SQL + "WHERE id = :id RETURNING id"),
                {**_retag_params(resolved), "id": str(document_id)},
            )
        ).first()
        if not row:
            raise HTTPException(404, "document not found")
    log.info("retagged document_id=%s to %s", document_id, resolved.labels or "global")
    return {
        "document_id": str(document_id),
        "scope_labels": resolved.labels,
        "scope_entries": resolved.entries,
        "applies_to_regions": resolved.regions,
    }


@app.post("/documents/backfill-fingerprints", dependencies=[Depends(require_service_key)])
async def backfill_fingerprints() -> dict[str, Any]:
    """Hash the stored original of every document that predates fingerprints.

    Without this, a document uploaded before migration 06 is invisible to the
    duplicate check forever, and uploading it again would go straight through.
    Safe to re-run: only rows still missing a hash are touched.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, storage_path FROM documents "
                    "WHERE content_sha256 IS NULL ORDER BY created_at"
                )
            )
        ).all()

    hashed: list[str] = []
    missing: list[str] = []
    for doc_id, storage_path in rows:
        path = resolve_storage_file(storage_path or "")
        if path is None:
            # Recorded rather than skipped silently: a document whose original
            # is gone cannot be downloaded or reprocessed either.
            missing.append(str(doc_id))
            continue
        digest = await asyncio.to_thread(fingerprint_file, path)
        async with session_scope() as session:
            await session.execute(
                text("UPDATE documents SET content_sha256 = :h WHERE id = :id"),
                {"h": digest, "id": str(doc_id)},
            )
        hashed.append(str(doc_id))

    log.info("fingerprint backfill: %d hashed, %d missing originals", len(hashed), len(missing))
    return {"hashed": len(hashed), "missing_original": missing}


@app.get("/regions", dependencies=[Depends(require_service_key)])
async def list_regions() -> dict[str, Any]:
    """Every country in the catalogue, enabled or not, with what uses it.

    The usage counts are what let the Countries page explain a refusal before
    the click instead of after it. `documents_using` counts documents whose
    expanded regions include the country, whether it was picked directly or
    arrived through a group.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT r.code, r.label, r.active, "
                    "       (SELECT count(*) FROM documents d "
                    "        WHERE d.applies_to_regions @> ARRAY[r.code]) AS documents_using, "
                    "       COALESCE((SELECT array_agg(g.code ORDER BY g.code) "
                    "                 FROM region_groups g "
                    "                 WHERE r.code = ANY(g.member_regions)), '{}') AS groups "
                    "FROM regions r ORDER BY r.active DESC, r.label"
                )
            )
        ).mappings().all()
    return {"regions": [dict(r) for r in rows]}


class RegionBody(BaseModel):
    active: bool


@app.patch("/regions/{code}", dependencies=[Depends(require_service_key)])
async def set_region_active(code: str, body: RegionBody) -> dict[str, Any]:
    """Enable or disable a country.

    Enabling is always safe: a country that was not in the vocabulary appears
    in no existing tag, so nothing goes stale.

    Disabling is refused while anything uses the country. A document tagged
    with a country the picker can no longer show is a tag nobody can edit back,
    and a group that contains a disabled country would keep expanding to it.
    """
    code = code.strip().upper()
    async with session_scope() as session:
        # FOR UPDATE pairs with the FOR SHARE in resolve_scope: a disable and a
        # tag naming the same country serialise, so the usage counted here
        # cannot be invalidated by a tag that lands a moment later.
        row = (
            await session.execute(
                text("SELECT code, label, active FROM regions WHERE code = :c FOR UPDATE"),
                {"c": code},
            )
        ).mappings().first()
        if not row:
            raise HTTPException(404, f"{code} is not in the country catalogue")

        if not body.active:
            documents_using = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM documents "
                        "WHERE applies_to_regions @> ARRAY[CAST(:c AS text)]"
                    ),
                    {"c": code},
                )
            ).scalar_one()
            groups = [
                r[0]
                for r in (
                    await session.execute(
                        text(
                            "SELECT code FROM region_groups "
                            "WHERE :c = ANY(member_regions) ORDER BY code"
                        ),
                        {"c": code},
                    )
                ).all()
            ]
            if documents_using or groups:
                reasons = []
                if documents_using:
                    reasons.append(
                        f"{documents_using} document{'s' if documents_using != 1 else ''}"
                    )
                if groups:
                    reasons.append(f"the {', '.join(groups)} group{'s' if len(groups) != 1 else ''}")
                raise HTTPException(
                    409,
                    {
                        "message": f"{row['label']} is used by {' and '.join(reasons)}",
                        "documents_using": documents_using,
                        "groups": groups,
                    },
                )

        await session.execute(
            text("UPDATE regions SET active = :a WHERE code = :c"),
            {"a": body.active, "c": code},
        )
    log.info("region %s %s", code, "enabled" if body.active else "disabled")
    return {"code": code, "label": row["label"], "active": body.active}


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
