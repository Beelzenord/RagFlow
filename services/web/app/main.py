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
            # would answer 503.
            "voice_enabled": voice.enabled(),
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
) -> JSONResponse:
    content = await file.read()
    files = {"file": (file.filename or "upload.bin", content, file.content_type or "application/octet-stream")}
    data: dict[str, str] = {}
    if user_id:
        data["user_id"] = user_id
    if collection:
        data["collection"] = collection

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


@app.get("/api/documents", dependencies=[Depends(require_admin)])
async def api_documents(
    request: Request,
    collection: str | None = None,
    user_id: str | None = None,
    limit: int | None = None,
) -> JSONResponse:
    """The corpus inventory, for the admin sidebar and its status polling.

    Admin-only because the filenames are the inventory: a reader is told which
    documents an answer came from, not everything that was ever uploaded.
    """
    params: dict[str, Any] = {}
    if collection:
        params["collection"] = collection
    if user_id:
        params["user_id"] = user_id
    if limit is not None:
        params["limit"] = limit
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


@app.post("/api/voice/ask")
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
