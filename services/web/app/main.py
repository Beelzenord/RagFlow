"""Tiny BFF for the RAG web UI.

The browser talks only to this service; it forwards calls to the internal
ingestion and query services with `x-api-key` attached so secrets never
ship to the browser.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app import auth, voice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("web")

INGESTION_URL = os.environ.get("INGESTION_URL", "http://ingestion:8001").rstrip("/")
QUERY_URL = os.environ.get("QUERY_URL", "http://query:8002").rstrip("/")
SERVICE_API_KEY = os.environ.get("SERVICE_API_KEY", "")
HTTP_TIMEOUT = float(os.environ.get("WEB_HTTP_TIMEOUT", "120"))

STATIC_DIR = Path(__file__).parent / "static"
# Outside STATIC_DIR on purpose: the static mount serves anything in there to
# every signed-in user, readers included. The admin page is only reachable
# through the admin-gated routes below, so for a reader it does not exist.
ADMIN_DIR = Path(__file__).parent / "admin"

# A browser holding a stale app.js keeps polling and never reacts to the 401 the
# login gate returns, so the console looks broken instead of asking for a login.
# StaticFiles still sends an ETag, so revalidating costs a 304.
NO_CACHE = {"cache-control": "no-cache"}


class RevalidatedStatic(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        resp = super().file_response(*args, **kwargs)
        resp.headers["cache-control"] = "no-cache"
        return resp


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Reuse a single httpx.AsyncClient across all requests so the connection
    pool to the internal ingestion/query services survives between calls."""
    app.state.http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    log.info("web service ready (shared http client initialized)")
    auth.log_startup_state()
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="RAG Web UI", version="0.1.0", lifespan=lifespan)

# Added first so SessionMiddleware ends up outermost: require_login reads
# request.session, which only exists once SessionMiddleware has run.
app.middleware("http")(auth.require_login)
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.SESSION_SECRET,
    max_age=auth.SESSION_MAX_AGE,
    https_only=auth.COOKIE_SECURE,
    same_site="lax",
)


class LoginBody(BaseModel):
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=500)


@app.get("/api/me")
async def api_me(request: Request) -> JSONResponse:
    """Who the browser is talking as, and where to send it to sign in or out.

    The UI cannot work this out for itself: in entra mode the identity arrives
    in a header the page never sees, and signing out has to go through the
    platform rather than this app.
    """
    return JSONResponse(
        {
            "user": auth.current_user(request),
            "auth_mode": auth.describe_mode(),
            "login_url": auth.login_url(),
            "logout_url": auth.logout_url(),
            "role": auth.role(request),
            "can_write": auth.is_admin(request),
            # Lets the UI hide the microphone rather than offer a control that
            # would answer 503 (not configured) or 403 (not an admin). Advisory
            # only - require_voice on the endpoint is what actually enforces it.
            "voice_enabled": voice.enabled() and auth.is_admin(request),
        }
    )


@app.get("/login")
async def login_page(request: Request) -> Any:
    # In entra mode the platform owns sign-in, so this app has no login form.
    if auth.entra_mode():
        raise HTTPException(404, "sign-in is handled by Microsoft Entra")
    if not auth.auth_enabled() or auth.is_logged_in(request):
        return RedirectResponse("/", status_code=302)
    # An explicit route: the StaticFiles mount resolves /login to a directory,
    # not to login.html.
    return FileResponse(STATIC_DIR / "login.html", headers=NO_CACHE)


@app.post("/api/login")
async def api_login(body: LoginBody, request: Request) -> JSONResponse:
    if auth.entra_mode():
        raise HTTPException(404, "sign-in is handled by Microsoft Entra")
    if not auth.auth_enabled():
        return JSONResponse({"ok": True})
    if not await auth.check_credentials(body.username, body.password):
        log.warning("failed login for %r", body.username[:64])
        return JSONResponse({"error": "wrong username or password"}, status_code=401)
    auth.sign_in(request)
    return JSONResponse({"ok": True})


@app.post("/api/logout")
async def api_logout(request: Request) -> JSONResponse:
    auth.sign_out(request)
    return JSONResponse({"ok": True})


def require_admin(request: Request) -> None:
    """Guard everything to do with managing the corpus - changing it, and being
    told what is in it.

    The UI hides these from a reader, but hiding is not enforcing: the browser is
    the only client that honours a hidden control, so the check has to live here
    too or the documents list is a curl away.
    """
    if not auth.is_admin(request):
        raise HTTPException(403, "this account may ask questions, not manage documents")


def require_voice(request: Request) -> None:
    """Voice is an admin-only feature.

    Separate from require_admin because the refusal is not about managing the
    corpus - a reader may still ask this same question in writing - and a 403
    that says so is the difference between a understood restriction and a bug
    report. Hiding the microphone in the UI is not enforcing it: /api/me is
    advisory, and the endpoint is a curl away without this.
    """
    if not auth.is_admin(request):
        raise HTTPException(403, "voice is limited to admin accounts; ask in writing instead")


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"x-api-key": SERVICE_API_KEY}
    if extra:
        headers.update(extra)
    return headers


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/upload", dependencies=[Depends(require_admin)])
async def api_upload(
    request: Request,
    file: UploadFile = File(...),
    user_id: str | None = Form(default=None),
    collection: str | None = Form(default=None),
    scope: str | None = Form(default=None),
) -> JSONResponse:
    content = await file.read()
    files = {"file": (file.filename or "upload.bin", content, file.content_type or "application/octet-stream")}
    data: dict[str, str] = {}
    if user_id:
        data["user_id"] = user_id
    if collection:
        data["collection"] = collection
    # Passed through untouched. The vocabulary lives in the database and the
    # ingestion service is what reads it, so validating here would be a second
    # copy of the rules to keep in step - and it answers 400 on an unknown code.
    if scope:
        data["scope"] = scope

    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.post(
            f"{INGESTION_URL}/ingest",
            headers=_auth_headers(),
            files=files,
            data=data,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


@app.get("/api/scopes", dependencies=[Depends(require_admin)])
async def api_scopes(request: Request) -> JSONResponse:
    """The scope vocabulary, for the upload picker.

    Proxied rather than read here: this service holds no database connection by
    design, and the vocabulary belongs to whoever writes documents against it.
    Admin-only because only admins upload.
    """
    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.get(f"{INGESTION_URL}/scopes", headers=_auth_headers())
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


@app.get("/api/documents", dependencies=[Depends(require_admin)])
async def api_documents(
    request: Request,
    collection: str | None = None,
    user_id: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    q: str | None = None,
    scope: str | None = None,
    status: str | None = None,
    sort: str | None = None,
) -> JSONResponse:
    """The corpus inventory, for the sidebar ticker and the admin page.

    Admin-only because the filenames are the inventory: a reader is told which
    documents an answer came from, not everything that was ever uploaded.
    Parameters are forwarded as given; the ingestion service validates them.
    """
    raw = {
        "collection": collection,
        "user_id": user_id,
        "limit": limit,
        "offset": offset,
        "q": q,
        "scope": scope,
        "status": status,
        "sort": sort,
    }
    params: dict[str, Any] = {k: v for k, v in raw.items() if v not in (None, "")}
    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.get(
            f"{INGESTION_URL}/documents",
            headers=_auth_headers(),
            params=params,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


@app.get("/api/documents/{document_id}", dependencies=[Depends(require_admin)])
async def api_document(document_id: UUID, request: Request) -> JSONResponse:
    """One document's ingestion status, polled while an upload is processing."""
    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.get(
            f"{INGESTION_URL}/documents/{document_id}",
            headers=_auth_headers(),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


@app.delete("/api/documents/{document_id}", dependencies=[Depends(require_admin)])
async def api_document_delete(document_id: UUID, request: Request) -> JSONResponse:
    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.delete(
            f"{INGESTION_URL}/documents/{document_id}",
            headers=_auth_headers(),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


async def _forward(
    request: Request, method: str, path: str, **kwargs: Any
) -> JSONResponse:
    """Relay one call to the ingestion service and hand its answer back as is.

    Status codes pass through untouched - a 400 for an unknown scope, a 409 for
    a country that is still in use - because those carry the explanation the
    admin page shows. Turning them into a generic failure here would throw that
    away.
    """
    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.request(
            method, f"{INGESTION_URL}{path}", headers=_auth_headers(), **kwargs
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


class RetagBody(BaseModel):
    scope: str = Field(..., max_length=500)


class BulkRetagBody(BaseModel):
    document_ids: list[UUID] = Field(..., min_length=1, max_length=500)
    scope: str = Field(..., max_length=500)


class RegionToggleBody(BaseModel):
    active: bool


@app.patch("/api/documents/scope", dependencies=[Depends(require_admin)])
async def api_retag_documents(body: BulkRetagBody, request: Request) -> JSONResponse:
    """Retag many documents at once. All or nothing, decided upstream."""
    return await _forward(
        request,
        "PATCH",
        "/documents/scope",
        json={"document_ids": [str(i) for i in body.document_ids], "scope": body.scope},
    )


@app.patch("/api/documents/{document_id}/scope", dependencies=[Depends(require_admin)])
async def api_retag_document(
    document_id: UUID, body: RetagBody, request: Request
) -> JSONResponse:
    """Change what one document applies to. Metadata only - nothing is re-parsed."""
    return await _forward(
        request, "PATCH", f"/documents/{document_id}/scope", json={"scope": body.scope}
    )


@app.post("/api/documents/backfill-fingerprints", dependencies=[Depends(require_admin)])
async def api_backfill_fingerprints(request: Request) -> JSONResponse:
    return await _forward(request, "POST", "/documents/backfill-fingerprints")


@app.get("/api/regions", dependencies=[Depends(require_admin)])
async def api_regions(request: Request) -> JSONResponse:
    """The whole country catalogue, enabled or not, for the Countries page."""
    return await _forward(request, "GET", "/regions")


@app.patch("/api/regions/{code}", dependencies=[Depends(require_admin)])
async def api_region_toggle(
    code: str, body: RegionToggleBody, request: Request
) -> JSONResponse:
    if not code.isalpha() or len(code) != 2:
        raise HTTPException(400, "a country code is two letters")
    return await _forward(
        request, "PATCH", f"/regions/{code.upper()}", json={"active": body.active}
    )


@app.get("/admin")
async def admin_page(request: Request) -> Any:
    """The filing page. Readers are sent back to the chat rather than shown a
    403: a page is not an API, and for them it should simply not exist. The
    endpoints it calls are guarded on their own regardless."""
    if not auth.is_admin(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(ADMIN_DIR / "admin.html", headers=NO_CACHE)


@app.get("/admin/admin.js")
async def admin_script(request: Request) -> Any:
    # 404 rather than 403 for a reader, for the same reason as the page.
    if not auth.is_admin(request):
        raise HTTPException(404, "not found")
    return FileResponse(
        ADMIN_DIR / "admin.js", media_type="text/javascript", headers=NO_CACHE
    )


@app.get("/api/documents/{document_id}/file")
async def api_document_file(document_id: UUID, request: Request) -> StreamingResponse:
    """Proxy the original upload from ingestion so the browser can download it."""
    client: httpx.AsyncClient = request.app.state.http
    url = f"{INGESTION_URL}/documents/{document_id}/file"
    try:
        upstream = await client.send(
            client.build_request("GET", url, headers=_auth_headers()),
            stream=True,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc

    if upstream.status_code >= 400:
        try:
            body_bytes = await upstream.aread()
        finally:
            await upstream.aclose()
        msg = body_bytes.decode(errors="replace")[:500] or f"HTTP {upstream.status_code}"
        raise HTTPException(upstream.status_code, msg)

    media_type = upstream.headers.get("content-type") or "application/octet-stream"
    out_headers: dict[str, str] = {}
    cd = upstream.headers.get("content-disposition")
    if cd:
        out_headers["content-disposition"] = cd
    cl = upstream.headers.get("content-length")
    if cl:
        out_headers["content-length"] = cl

    async def stream_body() -> Any:
        try:
            async for chunk in upstream.aiter_bytes():
                if chunk:
                    yield chunk
        except httpx.HTTPError as exc:
            log.warning("document file stream failed: %s", exc)
        finally:
            await upstream.aclose()

    return StreamingResponse(stream_body(), media_type=media_type, headers=out_headers)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class QueryBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    document_id: str | None = None
    collection: str | None = None
    user_id: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)
    voice: bool | None = None
    lang: str | None = None
    # Prior turns, oldest first, excluding the current question.
    history: list[ChatTurn] | None = Field(default=None, max_length=12)


@app.post("/api/query")
async def api_query(body: QueryBody, request: Request) -> JSONResponse:
    payload = body.model_dump(exclude_none=True)
    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.post(
            f"{QUERY_URL}/query",
            headers=_auth_headers({"content-type": "application/json"}),
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"query service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


@app.post("/api/query/stream")
async def api_query_stream(body: QueryBody, request: Request) -> StreamingResponse:
    """Forward NDJSON streaming bytes from the query service to the browser.

    No buffering: each chunk from upstream is yielded as-is so tokens reach
    the UI as soon as the LLM produces them.
    """
    payload = body.model_dump(exclude_none=True)
    client: httpx.AsyncClient = request.app.state.http

    async def upstream() -> Any:
        try:
            async with client.stream(
                "POST",
                f"{QUERY_URL}/query/stream",
                headers=_auth_headers({"content-type": "application/json"}),
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body_bytes = await resp.aread()
                    msg = body_bytes.decode(errors="replace")[:500] or f"HTTP {resp.status_code}"
                    yield (
                        '{"type":"error","message":'
                        + _json_str(f"upstream {resp.status_code}: {msg}")
                        + "}\n"
                    ).encode("utf-8")
                    return
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            yield (
                '{"type":"error","message":'
                + _json_str(f"query service unreachable: {exc}")
                + "}\n"
            ).encode("utf-8")

    return StreamingResponse(upstream(), media_type="application/x-ndjson")


MAX_VOICE_UPLOAD_BYTES = int(os.environ.get("MAX_VOICE_UPLOAD_BYTES", str(25 * 1024 * 1024)))
# A container header plus a fraction of a second of Opus. Below this there is no
# speech to find, so the recording is a stray click or a recorder that produced
# nothing - reject it before paying Scribe to tell us the same thing. A backstop
# for a broken client rather than the main gate: the browser already refuses to
# upload a clip that is too short or too quiet, and the bitrate this implies
# varies by codec, so the floor is set well under one second of real audio.
MIN_VOICE_UPLOAD_BYTES = int(os.environ.get("MIN_VOICE_UPLOAD_BYTES", "1024"))


@app.post("/api/voice/ask", dependencies=[Depends(require_voice)])
async def api_voice_ask(
    request: Request,
    file: UploadFile = File(...),
    lang: str | None = Form(default=None),
    document_id: str | None = Form(default=None),
    history: str | None = Form(default=None),
) -> StreamingResponse:
    """One spoken turn: audio in, NDJSON (transcript, answer, audio) out.

    Deliberately not an audio response. Returning `audio/mpeg` means the status
    line is committed before the first byte is generated, so an upstream refusal
    becomes a 200 with an empty body and the browser cannot tell success from
    failure - which is exactly how the previous version lost sentences in silence.
    NDJSON keeps failures addressable as `error` events.
    """
    if not voice.enabled():
        raise HTTPException(503, "voice features require ELEVENLABS_API_KEY")

    content = await file.read()
    if not content:
        raise HTTPException(400, "no audio received")
    if len(content) < MIN_VOICE_UPLOAD_BYTES:
        raise HTTPException(400, "recording too short")
    if len(content) > MAX_VOICE_UPLOAD_BYTES:
        raise HTTPException(413, "recording too large")

    turns = _parse_history(history)
    client: httpx.AsyncClient = request.app.state.http

    return StreamingResponse(
        voice.run_turn(
            http=client,
            query_url=QUERY_URL,
            service_api_key=SERVICE_API_KEY,
            audio=content,
            filename=file.filename or "audio.webm",
            content_type=file.content_type or "audio/webm",
            lang=lang,
            document_id=document_id,
            history=turns,
        ),
        media_type="application/x-ndjson",
        headers={"cache-control": "no-store"},
    )


def _parse_history(raw: str | None) -> list[dict[str, str]]:
    """Validate the prior turns a multipart form carried as a JSON string.

    Shaped to match ChatTurn in the query service, and dropped rather than
    rejected on malformed input: losing conversational context is a far better
    failure than refusing the question.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("ignoring malformed voice history")
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, str]] = []
    for item in parsed[-12:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = item.get("content")
        if role in ("user", "assistant") and isinstance(text, str) and text.strip():
            out.append({"role": role, "content": text[:4000]})
    return out


def _json_str(s: str) -> str:
    import json as _json

    return _json.dumps(s, ensure_ascii=False)


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"error": resp.text or f"upstream returned status {resp.status_code}"}


app.mount("/", RevalidatedStatic(directory=str(STATIC_DIR), html=True), name="static")
