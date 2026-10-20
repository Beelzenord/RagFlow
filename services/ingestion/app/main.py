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
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from rag_shared.db import session_scope
from rag_shared.security import require_service_key
from rag_shared.settings import settings

from .audience import (
    MARKET_CODE,
    Audience,
    AudienceError,
    is_global_sql,
    normalize_code,
    targets_market_sql,
    validate_audience,
)
from .countries import ISO_COUNTRIES
from .pipeline import run_ingestion
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


# Who each document is for, as two JSON arrays. Group members ride along so the
# dashboard can say what a group covers without a second request.
_AUDIENCE_COLUMNS = (
    "COALESCE((SELECT json_agg(json_build_object('code', m.code, 'name', m.name) ORDER BY m.name) "
    "          FROM document_audience a JOIN markets m ON m.code = a.market_code "
    "          WHERE a.document_id = d.id), '[]'::json) AS audience_countries, "
    "COALESCE((SELECT json_agg(json_build_object("
    "                   'id', g.id, 'name', g.name, "
    "                   'members', ARRAY(SELECT gm.market_code FROM market_group_members gm "
    "                                    WHERE gm.group_id = g.id ORDER BY gm.market_code)"
    "                 ) ORDER BY lower(g.name)) "
    "          FROM document_audience a JOIN market_groups g ON g.id = a.group_id "
    "          WHERE a.document_id = d.id), '[]'::json) AS audience_groups"
)

_DOCUMENT_COLUMNS = (
    "d.id, d.original_filename, d.file_type, d.status, d.error_message, "
    "d.collection, d.user_id, d.created_at, d.updated_at, "
    "(SELECT count(*) FROM document_chunks WHERE document_id = d.id) AS chunk_count, "
    + _AUDIENCE_COLUMNS
)


def _json_value(value: Any) -> Any:
    # Untyped text() columns can come back from asyncpg as the raw JSON string.
    return json.loads(value) if isinstance(value, str) else value


def _document_out(row: Any) -> dict[str, Any]:
    doc = dict(row)
    countries = _json_value(doc.pop("audience_countries")) or []
    groups = _json_value(doc.pop("audience_groups")) or []
    doc["audience"] = {
        "everyone": not countries and not groups,
        "countries": countries,
        "groups": groups,
    }
    return doc


@app.get("/documents/{document_id}", dependencies=[Depends(require_service_key)])
async def get_document(document_id: UUID) -> dict[str, Any]:
    async with session_scope() as session:
        row = (
            await session.execute(
                text(f"SELECT {_DOCUMENT_COLUMNS} FROM documents d WHERE d.id = :id"),
                {"id": str(document_id)},
            )
        ).mappings().first()
    if not row:
        raise HTTPException(404, "document not found")
    return _document_out(row)


# Types a browser renders itself, for the dashboard's preview. Anything else is
# always a download - an uploaded file the browser would sniff and execute as
# HTML must never be served inline from this origin.
INLINE_TYPES = {"application/pdf", "image/jpeg"}


@app.get("/documents/{document_id}/file", dependencies=[Depends(require_service_key)])
async def get_document_file(document_id: UUID, inline: bool = Query(default=False)) -> FileResponse:
    """Stream the stored original upload, for download or (inline) preview."""
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
    disposition = "inline" if inline and media_type.lower() in INLINE_TYPES else "attachment"
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=filename,
        content_disposition_type=disposition,
        headers={"x-content-type-options": "nosniff"},
    )


def _like_pattern(q: str) -> str:
    """A substring ILIKE pattern in which the user's % and _ are literal."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@app.get("/documents", dependencies=[Depends(require_service_key)])
async def list_documents(
    collection: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    who: str | None = Query(
        default=None,
        description="'global', a country code (directly or through a group), or a group id",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if collection:
        clauses.append("d.collection = :collection")
        params["collection"] = collection
    if user_id:
        clauses.append("d.user_id = :user_id")
        params["user_id"] = user_id
    if q and q.strip():
        clauses.append("d.original_filename ILIKE :q ESCAPE '\\'")
        params["q"] = _like_pattern(q.strip())
    if who:
        if who == "global":
            clauses.append(is_global_sql())
        elif MARKET_CODE.match(who.upper()):
            clauses.append(targets_market_sql(param="who"))
            params["who"] = who.upper()
        else:
            try:
                params["who_group"] = str(UUID(who))
            except ValueError:
                raise HTTPException(400, "who must be 'global', a country code or a group id") from None
            clauses.append(
                "EXISTS (SELECT 1 FROM document_audience a "
                "WHERE a.document_id = d.id AND a.group_id = :who_group)"
            )
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT {_DOCUMENT_COLUMNS} FROM documents d {where} "
        "ORDER BY d.created_at DESC, d.id LIMIT :limit OFFSET :offset"
    )
    async with session_scope() as session:
        total = (await session.execute(text(f"SELECT count(*) FROM documents d {where}"), params)).scalar_one()
        rows = (
            await session.execute(text(sql), {**params, "limit": limit, "offset": offset})
        ).mappings().all()
    return {"documents": [_document_out(r) for r in rows], "total": total}


# --- Who a document is for ------------------------------------------------------


class AudienceBody(BaseModel):
    everyone: bool
    countries: list[str] = Field(default_factory=list, max_length=300)
    groups: list[str] = Field(default_factory=list, max_length=100)


class BulkAudienceBody(AudienceBody):
    # The whole set or none of it: a half-applied bulk change is worse than a
    # refused one, because nobody can see which half landed.
    document_ids: list[UUID] = Field(..., min_length=1, max_length=500)


def _sqlstate(exc: IntegrityError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _validated(body: AudienceBody) -> Audience:
    try:
        return validate_audience(body.everyone, body.countries, body.groups)
    except AudienceError as exc:
        raise HTTPException(400, str(exc)) from None


async def _check_audience_exists(session: Any, audience: Audience) -> None:
    """Name anything the selection refers to that is not (or no longer) there,
    rather than letting a foreign key turn it into an opaque error."""
    if audience.countries:
        found = set(
            (
                await session.execute(
                    text("SELECT code FROM markets WHERE code = ANY(:codes)"),
                    {"codes": list(audience.countries)},
                )
            ).scalars()
        )
        missing = [c for c in audience.countries if c not in found]
        if missing:
            raise HTTPException(
                400, f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not one of your markets"
            )
    if audience.groups:
        found = {
            str(g)
            for g in (
                await session.execute(
                    text("SELECT id FROM market_groups WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": list(audience.groups)},
                )
            ).scalars()
        }
        if len(found) != len(audience.groups):
            raise HTTPException(400, "a selected group no longer exists - reload and try again")


async def _write_audience(session: Any, document_ids: list[str], audience: Audience) -> None:
    await session.execute(
        text("DELETE FROM document_audience WHERE document_id = ANY(CAST(:docs AS uuid[]))"),
        {"docs": document_ids},
    )
    if audience.countries:
        await session.execute(
            text(
                "INSERT INTO document_audience (document_id, market_code) "
                "SELECT d, c FROM unnest(CAST(:docs AS uuid[])) d "
                "CROSS JOIN unnest(CAST(:codes AS text[])) c"
            ),
            {"docs": document_ids, "codes": list(audience.countries)},
        )
    if audience.groups:
        await session.execute(
            text(
                "INSERT INTO document_audience (document_id, group_id) "
                "SELECT d, g FROM unnest(CAST(:docs AS uuid[])) d "
                "CROSS JOIN unnest(CAST(:groups AS uuid[])) g"
            ),
            {"docs": document_ids, "groups": list(audience.groups)},
        )


async def _set_audience(document_ids: list[str], body: AudienceBody) -> None:
    """Replace who these documents are for. Metadata only - nothing is re-parsed
    or re-embedded, so it takes effect on the next question."""
    audience = _validated(body)
    try:
        async with session_scope() as session:
            # Locks the rows so a concurrent delete cannot slip between the
            # existence check and the insert.
            found = {
                str(r)
                for r in (
                    await session.execute(
                        text("SELECT id FROM documents WHERE id = ANY(CAST(:docs AS uuid[])) FOR UPDATE"),
                        {"docs": document_ids},
                    )
                ).scalars()
            }
            missing = [d for d in document_ids if d not in found]
            if missing:
                raise HTTPException(404, {"message": "document not found", "missing": missing})
            await _check_audience_exists(session, audience)
            await _write_audience(session, document_ids, audience)
    except IntegrityError as exc:
        # A market or group removed between the check above and the insert.
        if _sqlstate(exc) == "23503":
            raise HTTPException(409, "a market or group changed while saving - reload and try again") from None
        raise


@app.put("/documents/audience", dependencies=[Depends(require_service_key)])
async def set_documents_audience(body: BulkAudienceBody) -> dict[str, Any]:
    ids = list(dict.fromkeys(str(d) for d in body.document_ids))
    await _set_audience(ids, body)
    return {"updated": len(ids)}


@app.put("/documents/{document_id}/audience", dependencies=[Depends(require_service_key)])
async def set_document_audience(document_id: UUID, body: AudienceBody) -> dict[str, Any]:
    await _set_audience([str(document_id)], body)
    return await get_document(document_id)


# --- Markets: countries and groups -----------------------------------------------


@app.get("/markets", dependencies=[Depends(require_service_key)])
async def list_markets() -> dict[str, Any]:
    """Every market country and group, with what uses each.

    A country's `documents` counts every document limited to it, directly or
    through a group; `direct_documents` and `groups` are what block removing it.
    The dashboard lists the documents behind each count with list_documents'
    `who` filter, so these counts must stay the same test as that filter
    (targets_market_sql, the group id match, is_global_sql).
    """
    async with session_scope() as session:
        countries = (
            await session.execute(
                text(
                    "SELECT m.code, m.name, "
                    "  (SELECT count(DISTINCT a.document_id) FROM document_audience a "
                    "     LEFT JOIN market_group_members gm ON gm.group_id = a.group_id "
                    "     WHERE a.market_code = m.code OR gm.market_code = m.code) AS documents, "
                    "  (SELECT count(*) FROM document_audience a WHERE a.market_code = m.code) "
                    "     AS direct_documents, "
                    "  ARRAY(SELECT g.name FROM market_group_members gm "
                    "        JOIN market_groups g ON g.id = gm.group_id "
                    "        WHERE gm.market_code = m.code ORDER BY lower(g.name)) AS groups "
                    "FROM markets m ORDER BY m.name"
                )
            )
        ).mappings().all()
        groups = (
            await session.execute(
                text(
                    "SELECT g.id, g.name, "
                    "  ARRAY(SELECT gm.market_code FROM market_group_members gm "
                    "        WHERE gm.group_id = g.id ORDER BY gm.market_code) AS members, "
                    "  (SELECT count(*) FROM document_audience a WHERE a.group_id = g.id) AS documents "
                    "FROM market_groups g ORDER BY lower(g.name)"
                )
            )
        ).mappings().all()
        global_documents = (
            await session.execute(text(f"SELECT count(*) FROM documents d WHERE {is_global_sql()}"))
        ).scalar_one()
    return {
        "countries": [dict(r) for r in countries],
        "groups": [{**dict(r), "id": str(r["id"])} for r in groups],
        "global_documents": global_documents,
    }


@app.get("/markets/catalogue", dependencies=[Depends(require_service_key)])
async def market_catalogue() -> dict[str, Any]:
    """Every ISO country, marked with whether it is already a market."""
    async with session_scope() as session:
        added = set((await session.execute(text("SELECT code FROM markets"))).scalars())
    countries = [
        {"code": code, "name": name, "added": code in added}
        for code, name in sorted(ISO_COUNTRIES.items(), key=lambda kv: kv[1])
    ]
    return {"countries": countries}


class CountryBody(BaseModel):
    code: str = Field(..., min_length=2, max_length=2)


@app.post("/markets/countries", dependencies=[Depends(require_service_key)])
async def add_country(body: CountryBody) -> dict[str, Any]:
    try:
        code = normalize_code(body.code)
    except AudienceError as exc:
        raise HTTPException(400, str(exc)) from None
    name = ISO_COUNTRIES.get(code)
    if name is None:
        raise HTTPException(400, f"{code} is not an ISO country code")
    async with session_scope() as session:
        inserted = (
            await session.execute(
                text(
                    "INSERT INTO markets (code, name) VALUES (:c, :n) "
                    "ON CONFLICT (code) DO NOTHING RETURNING code"
                ),
                {"c": code, "n": name},
            )
        ).first()
    # Adding a country that is already there is not an error: the admin's
    # intent - "Sweden is a market" - holds either way.
    return {"code": code, "name": name, "added": inserted is not None}


async def _country_usage(code: str) -> dict[str, Any]:
    async with session_scope() as session:
        documents = (
            await session.execute(
                text("SELECT count(*) FROM document_audience WHERE market_code = :c"), {"c": code}
            )
        ).scalar_one()
        groups = list(
            (
                await session.execute(
                    text(
                        "SELECT g.name FROM market_group_members gm "
                        "JOIN market_groups g ON g.id = gm.group_id "
                        "WHERE gm.market_code = :c ORDER BY lower(g.name)"
                    ),
                    {"c": code},
                )
            ).scalars()
        )
    return {"documents": documents, "groups": groups}


@app.delete("/markets/countries/{code}", dependencies=[Depends(require_service_key)])
async def remove_country(code: str) -> dict[str, Any]:
    try:
        code = normalize_code(code)
    except AudienceError as exc:
        raise HTTPException(400, str(exc)) from None
    # The foreign keys are the guard; the usage lookup only explains a refusal.
    try:
        async with session_scope() as session:
            deleted = (
                await session.execute(
                    text("DELETE FROM markets WHERE code = :c RETURNING name"), {"c": code}
                )
            ).first()
    except IntegrityError as exc:
        if _sqlstate(exc) != "23503":
            raise
        usage = await _country_usage(code)
        parts = []
        if usage["groups"]:
            parts.append("it is in " + ", ".join(usage["groups"]))
        if usage["documents"]:
            n = usage["documents"]
            parts.append(f"{n} document{'s are' if n != 1 else ' is'} limited to it")
        reason = " and ".join(parts) or "it is still in use"
        raise HTTPException(
            409, {"message": f"{code} can't be removed: {reason}.", **usage}
        ) from None
    if deleted is None:
        raise HTTPException(404, f"{code} is not one of your markets")
    return {"code": code, "removed": True}


class GroupBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    members: list[str] = Field(..., max_length=300)


def _group_input(body: GroupBody) -> tuple[str, list[str]]:
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(400, "give the group a name")
    try:
        members = list(dict.fromkeys(normalize_code(c) for c in body.members if c.strip()))
    except AudienceError as exc:
        raise HTTPException(400, str(exc)) from None
    if not members:
        raise HTTPException(400, "a group needs at least one country")
    return name, members


async def _write_group(session: Any, group_id: str, members: list[str]) -> None:
    found = set(
        (
            await session.execute(
                text("SELECT code FROM markets WHERE code = ANY(:codes)"), {"codes": members}
            )
        ).scalars()
    )
    missing = [c for c in members if c not in found]
    if missing:
        raise HTTPException(400, f"add {', '.join(missing)} under Countries first")
    await session.execute(text("DELETE FROM market_group_members WHERE group_id = :g"), {"g": group_id})
    await session.execute(
        text(
            "INSERT INTO market_group_members (group_id, market_code) "
            "SELECT :g, c FROM unnest(CAST(:codes AS text[])) c"
        ),
        {"g": group_id, "codes": members},
    )


def _group_integrity_error(exc: IntegrityError, name: str) -> HTTPException:
    state = _sqlstate(exc)
    if state == "23505":
        return HTTPException(409, f"a group called {name} already exists")
    if state == "23503":
        return HTTPException(409, "a country changed while saving - reload and try again")
    raise exc


@app.post("/markets/groups", dependencies=[Depends(require_service_key)])
async def create_group(body: GroupBody) -> dict[str, Any]:
    name, members = _group_input(body)
    try:
        async with session_scope() as session:
            group_id = str(
                (
                    await session.execute(
                        text("INSERT INTO market_groups (name) VALUES (:n) RETURNING id"), {"n": name}
                    )
                ).scalar_one()
            )
            await _write_group(session, group_id, members)
    except IntegrityError as exc:
        raise _group_integrity_error(exc, name) from None
    return {"id": group_id, "name": name, "members": members, "documents": 0}


@app.put("/markets/groups/{group_id}", dependencies=[Depends(require_service_key)])
async def update_group(group_id: UUID, body: GroupBody) -> dict[str, Any]:
    """Rename a group and replace its members. Documents tagged with it follow
    at once - groups are resolved when a question is asked, not stored."""
    name, members = _group_input(body)
    try:
        async with session_scope() as session:
            updated = (
                await session.execute(
                    text("UPDATE market_groups SET name = :n WHERE id = :g RETURNING id"),
                    {"n": name, "g": str(group_id)},
                )
            ).first()
            if updated is None:
                raise HTTPException(404, "group not found")
            await _write_group(session, str(group_id), members)
            documents = (
                await session.execute(
                    text("SELECT count(*) FROM document_audience WHERE group_id = :g"),
                    {"g": str(group_id)},
                )
            ).scalar_one()
    except IntegrityError as exc:
        raise _group_integrity_error(exc, name) from None
    return {"id": str(group_id), "name": name, "members": members, "documents": documents}


@app.delete("/markets/groups/{group_id}", dependencies=[Depends(require_service_key)])
async def delete_group(group_id: UUID) -> dict[str, Any]:
    try:
        async with session_scope() as session:
            deleted = (
                await session.execute(
                    text("DELETE FROM market_groups WHERE id = :g RETURNING name"), {"g": str(group_id)}
                )
            ).first()
    except IntegrityError as exc:
        if _sqlstate(exc) != "23503":
            raise
        async with session_scope() as session:
            n = (
                await session.execute(
                    text("SELECT count(*) FROM document_audience WHERE group_id = :g"),
                    {"g": str(group_id)},
                )
            ).scalar_one()
        raise HTTPException(
            409,
            {
                "message": f"this group can't be deleted: {n} document{'s are' if n != 1 else ' is'} "
                "limited to it. Change who those documents are for first.",
                "documents": n,
            },
        ) from None
    if deleted is None:
        raise HTTPException(404, "group not found")
    return {"id": str(group_id), "deleted": True}


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
