"""Tiny BFF for the RAG web UI.

The browser talks only to this service; it forwards calls to the internal
ingestion and query services with `x-api-key` attached so secrets never
ship to the browser.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app import auth

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("web")

INGESTION_URL = os.environ.get("INGESTION_URL", "http://ingestion:8001").rstrip("/")
QUERY_URL = os.environ.get("QUERY_URL", "http://query:8002").rstrip("/")
SERVICE_API_KEY = os.environ.get("SERVICE_API_KEY", "")
HTTP_TIMEOUT = float(os.environ.get("WEB_HTTP_TIMEOUT", "120"))

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_VOICE_ID_SV = os.environ.get("ELEVENLABS_VOICE_ID_SV", "kPdGSxhZAqy4bmPAf9iJ")
ELEVENLABS_TTS_MODEL = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2")
ELEVENLABS_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v1")
ELEVENLABS_BASE_URL = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io").rstrip("/")

ELEVENLABS_VOICES: dict[str, str] = {
    "en": ELEVENLABS_VOICE_ID,
    "sv": ELEVENLABS_VOICE_ID_SV,
}


def _voice_for_lang(lang: str | None) -> str:
    key = (lang or "").lower()
    return ELEVENLABS_VOICES.get(key) or ELEVENLABS_VOICE_ID


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


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"x-api-key": SERVICE_API_KEY}
    if extra:
        headers.update(extra)
    return headers


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/upload")
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


@app.get("/api/documents")
async def api_documents(
    request: Request,
    collection: str | None = None,
    user_id: str | None = None,
    limit: int | None = None,
) -> JSONResponse:
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


@app.get("/api/documents/{document_id}")
async def api_document(document_id: UUID, request: Request) -> JSONResponse:
    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.get(
            f"{INGESTION_URL}/documents/{document_id}",
            headers=_auth_headers(),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


@app.delete("/api/documents/{document_id}")
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


@app.get("/api/chunks/{chunk_id}/evidence")
async def api_chunk_evidence(
    chunk_id: UUID,
    request: Request,
    variant: Literal["page", "crop"] = "page",
) -> Response:
    """Proxy the rendered page image behind a citation.

    Buffered rather than streamed: these are small PNGs that ingestion has
    already materialised on disk, so there is nothing to gain from a generator.
    Upstream's 404 for "no image available" is passed through unchanged, since
    the page treats it as "draw no tile".
    """
    client: httpx.AsyncClient = request.app.state.http
    try:
        upstream = await client.get(
            f"{INGESTION_URL}/chunks/{chunk_id}/evidence",
            params={"variant": variant},
            headers=_auth_headers(),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc

    if upstream.status_code >= 400:
        msg = upstream.text[:500] or f"HTTP {upstream.status_code}"
        raise HTTPException(upstream.status_code, msg)

    headers = {}
    cc = upstream.headers.get("cache-control")
    if cc:
        headers["cache-control"] = cc
    return Response(
        content=upstream.content,
        media_type=upstream.headers.get("content-type") or "image/png",
        headers=headers,
    )


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


def _elevenlabs_unavailable() -> HTTPException:
    return HTTPException(503, "voice features require ELEVENLABS_API_KEY")


@app.post("/api/voice/transcribe")
async def api_voice_transcribe(
    request: Request,
    file: UploadFile = File(...),
    lang: str | None = Form(default=None),
) -> JSONResponse:
    """Forward an audio blob to ElevenLabs Scribe and return the transcript."""
    if not ELEVENLABS_API_KEY:
        raise _elevenlabs_unavailable()
    content = await file.read()
    files = {
        "file": (
            file.filename or "audio.webm",
            content,
            file.content_type or "audio/webm",
        ),
    }
    data: dict[str, str] = {"model_id": ELEVENLABS_STT_MODEL}
    if lang and lang.lower() in ELEVENLABS_VOICES:
        data["language_code"] = lang.lower()
    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.post(
            f"{ELEVENLABS_BASE_URL}/v1/speech-to-text",
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            files=files,
            data=data,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"elevenlabs unreachable: {exc}") from exc

    if resp.status_code >= 400:
        return JSONResponse(
            status_code=resp.status_code,
            content={"error": resp.text[:500] or f"HTTP {resp.status_code}"},
        )
    body = _safe_json(resp)
    text = ""
    if isinstance(body, dict):
        text = body.get("text") or body.get("transcript") or ""
    return JSONResponse({"text": text})


class TTSBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    voice_id: str | None = None
    lang: str | None = None


@app.post("/api/voice/tts")
async def api_voice_tts(body: TTSBody, request: Request) -> StreamingResponse:
    """Stream audio/mpeg bytes from ElevenLabs TTS straight to the browser."""
    if not ELEVENLABS_API_KEY:
        raise _elevenlabs_unavailable()
    voice_id = body.voice_id or _voice_for_lang(body.lang)
    payload = {
        "text": body.text,
        "model_id": ELEVENLABS_TTS_MODEL,
    }
    client: httpx.AsyncClient = request.app.state.http

    async def upstream() -> Any:
        try:
            async with client.stream(
                "POST",
                f"{ELEVENLABS_BASE_URL}/v1/text-to-speech/{voice_id}/stream",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "content-type": "application/json",
                    "accept": "audio/mpeg",
                },
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body_bytes = await resp.aread()
                    log.warning(
                        "elevenlabs tts %s: %s",
                        resp.status_code,
                        body_bytes.decode(errors="replace")[:300],
                    )
                    return
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            log.warning("elevenlabs tts unreachable: %s", exc)

    return StreamingResponse(upstream(), media_type="audio/mpeg")


def _json_str(s: str) -> str:
    import json as _json

    return _json.dumps(s, ensure_ascii=False)


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"error": resp.text or f"upstream returned status {resp.status_code}"}


app.mount("/", RevalidatedStatic(directory=str(STATIC_DIR), html=True), name="static")
