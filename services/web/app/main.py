"""Tiny BFF for the RAG web UI.

The browser talks only to this service; it forwards calls to the internal
ingestion and query services with `x-api-key` attached so secrets never
ship to the browser.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
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
# Deliberately outside STATIC_DIR: everything in there is served to any
# signed-in visitor, readers included. These files are only reachable through
# the admin-gated routes below.
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
            # The voice picker's options; empty whenever the microphone is hidden.
            "voice_languages": voice.VOICE_LANGUAGES if voice.enabled() and auth.is_admin(request) else [],
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


async def _forward(request: Request, method: str, url: str, **kwargs: Any) -> JSONResponse:
    """Send one call to an internal service and hand back its status and body.

    Refusals pass through unchanged - a 409 explaining why a country can't be
    removed is the message the dashboard shows.
    """
    client: httpx.AsyncClient = request.app.state.http
    try:
        resp = await client.request(method, url, headers=_auth_headers(), **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"ingestion service unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=_safe_json(resp))


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
    q: str | None = None,
    who: str | None = None,
    include_global: bool | None = None,
    sort: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> JSONResponse:
    """The corpus inventory, for the dashboard and the admin's document picker.

    Admin-only because the filenames are the inventory: a reader is told which
    documents an answer came from, not everything that was ever uploaded.
    """
    params: dict[str, Any] = {}
    if collection:
        params["collection"] = collection
    if user_id:
        params["user_id"] = user_id
    if q:
        params["q"] = q
    if who:
        params["who"] = who
    if include_global:
        params["include_global"] = "true"
    if sort:
        params["sort"] = sort
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
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
async def api_document_file(document_id: UUID, request: Request, inline: bool = False) -> StreamingResponse:
    """Proxy the original upload from ingestion so the browser can download it,
    or - with inline - show a PDF or JPEG in the dashboard's preview. Ingestion
    decides which types may be inline; everything else stays a download."""
    client: httpx.AsyncClient = request.app.state.http
    url = f"{INGESTION_URL}/documents/{document_id}/file"
    try:
        upstream = await client.send(
            client.build_request(
                "GET", url, headers=_auth_headers(), params={"inline": "true"} if inline else None
            ),
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
    # An uploaded file must never be sniffed into something the browser runs.
    out_headers: dict[str, str] = {"x-content-type-options": "nosniff"}
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


# --- Markets and who each document is for ------------------------------------
# Bodies are checked properly by ingestion, which owns the rules; these models
# only keep the proxy from forwarding arbitrary JSON.


class AudienceBody(BaseModel):
    everyone: bool
    countries: list[str] = Field(default_factory=list, max_length=300)
    groups: list[str] = Field(default_factory=list, max_length=100)


class BulkAudienceBody(AudienceBody):
    document_ids: list[UUID] = Field(..., min_length=1, max_length=500)


class CountryBody(BaseModel):
    code: str = Field(..., min_length=2, max_length=2)


class GroupBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    members: list[str] = Field(..., max_length=300)


def _country_code(code: str) -> str:
    value = code.strip().upper()
    if len(value) != 2 or not value.isascii() or not value.isalpha():
        raise HTTPException(400, f"{code!r} is not a two-letter country code")
    return value


@app.post("/api/documents/rechunk-all", dependencies=[Depends(require_admin)])
async def api_documents_rechunk_all(request: Request) -> JSONResponse:
    """Rebuild every document's search chunks from the text already extracted."""
    return await _forward(request, "POST", f"{INGESTION_URL}/documents/rechunk-all")


@app.put("/api/documents/audience", dependencies=[Depends(require_admin)])
async def api_documents_audience(body: BulkAudienceBody, request: Request) -> JSONResponse:
    return await _forward(
        request, "PUT", f"{INGESTION_URL}/documents/audience", json=body.model_dump(mode="json")
    )


@app.put("/api/documents/{document_id}/audience", dependencies=[Depends(require_admin)])
async def api_document_audience(document_id: UUID, body: AudienceBody, request: Request) -> JSONResponse:
    return await _forward(
        request, "PUT", f"{INGESTION_URL}/documents/{document_id}/audience", json=body.model_dump()
    )


@app.get("/api/markets")
async def api_markets(request: Request) -> JSONResponse:
    """The markets, for the chat's "Answers for" picker and the dashboard.

    Open to readers, because the picker is theirs too - but trimmed to the
    country names. Group make-up and per-country document counts describe the
    corpus, which is admin knowledge.
    """
    resp = await _forward(request, "GET", f"{INGESTION_URL}/markets")
    if auth.is_admin(request) or resp.status_code != 200:
        return resp
    full = json.loads(resp.body)
    countries = [{"code": c["code"], "name": c["name"]} for c in full.get("countries", [])]
    return JSONResponse({"countries": countries})


@app.get("/api/markets/catalogue", dependencies=[Depends(require_admin)])
async def api_market_catalogue(request: Request) -> JSONResponse:
    return await _forward(request, "GET", f"{INGESTION_URL}/markets/catalogue")


@app.post("/api/markets/countries", dependencies=[Depends(require_admin)])
async def api_add_country(body: CountryBody, request: Request) -> JSONResponse:
    code = _country_code(body.code)
    return await _forward(request, "POST", f"{INGESTION_URL}/markets/countries", json={"code": code})


@app.delete("/api/markets/countries/{code}", dependencies=[Depends(require_admin)])
async def api_remove_country(code: str, request: Request) -> JSONResponse:
    return await _forward(request, "DELETE", f"{INGESTION_URL}/markets/countries/{_country_code(code)}")


@app.post("/api/markets/groups", dependencies=[Depends(require_admin)])
async def api_create_group(body: GroupBody, request: Request) -> JSONResponse:
    return await _forward(request, "POST", f"{INGESTION_URL}/markets/groups", json=body.model_dump())


@app.put("/api/markets/groups/{group_id}", dependencies=[Depends(require_admin)])
async def api_update_group(group_id: UUID, body: GroupBody, request: Request) -> JSONResponse:
    return await _forward(
        request, "PUT", f"{INGESTION_URL}/markets/groups/{group_id}", json=body.model_dump()
    )


@app.delete("/api/markets/groups/{group_id}", dependencies=[Depends(require_admin)])
async def api_delete_group(group_id: UUID, request: Request) -> JSONResponse:
    return await _forward(request, "DELETE", f"{INGESTION_URL}/markets/groups/{group_id}")


# --- Dashboard ------------------------------------------------------------------
# To a reader the dashboard does not exist: the page sends them back to the chat
# and its script is a plain 404, so nothing about it can be read off the server.


@app.get("/dashboard")
async def dashboard_page(request: Request) -> Any:
    if not auth.is_admin(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(ADMIN_DIR / "dashboard.html", headers=NO_CACHE)


@app.get("/dashboard/dashboard.js")
async def dashboard_script(request: Request) -> Any:
    if not auth.is_admin(request):
        raise HTTPException(404, "Not Found")
    return FileResponse(ADMIN_DIR / "dashboard.js", media_type="text/javascript", headers=NO_CACHE)


@app.get("/dashboard/dashboard.css")
async def dashboard_styles(request: Request) -> Any:
    if not auth.is_admin(request):
        raise HTTPException(404, "Not Found")
    return FileResponse(ADMIN_DIR / "dashboard.css", media_type="text/css", headers=NO_CACHE)


# --- Interface languages ----------------------------------------------------------
# One dictionary per language in app/locales; en.json is the full set of keys.
# Every language travels in one script, because the choice is made in the
# browser (static/lang.js) and switching must not need another request.

LOCALES_DIR = Path(__file__).parent / "locales"
LANGUAGES = ("en", "sv", "nb", "fi", "da")
# Wording that would tell a reader the Dashboard exists: its page switch and
# everything on it. Only admins are sent these.
ADMIN_KEY_PREFIXES = ("nav.", "dashboard.")


def _load_locales() -> dict[str, dict[str, Any]]:
    locales = {
        lang: json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8")) for lang in LANGUAGES
    }
    # A missing entry falls back to English in the browser, so it is a warning
    # here; tests/test_i18n.py is what keeps the dictionaries complete.
    for lang, entries in locales.items():
        missing = sorted(set(locales["en"]) - set(entries))
        if missing:
            log.warning("locale %s lacks %d keys, e.g. %s", lang, len(missing), missing[:3])
    return locales


def _strings_script(locales: dict[str, dict[str, Any]], admin: bool) -> tuple[bytes, str]:
    payload = {
        lang: {k: v for k, v in entries.items() if admin or not k.startswith(ADMIN_KEY_PREFIXES)}
        for lang, entries in locales.items()
    }
    body = ("Lang.add(" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ");\n").encode()
    return body, '"' + hashlib.sha256(body).hexdigest()[:20] + '"'


_LOCALES = _load_locales()
_STRINGS = {admin: _strings_script(_LOCALES, admin) for admin in (False, True)}


@app.get("/strings.js")
async def strings_script(request: Request) -> Response:
    """The dictionaries for static/lang.js.

    Public, so the sign-in page can be translated too - nothing in it describes
    the corpus. A reader's copy leaves out the Dashboard's wording; the two
    copies have different ETags, so a role change is never answered from cache.
    """
    body, etag = _STRINGS[auth.is_admin(request)]
    headers = {"etag": etag, "cache-control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="text/javascript", headers=headers)


MARKET_PATTERN = r"^[A-Z]{2}$"


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
    # "Answers for": the asker's market, an ISO country code. Steers which
    # documents are searched; it is a preference, not a permission.
    market: str | None = Field(default=None, pattern=MARKET_PATTERN)
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
    market: str | None = Form(default=None, pattern=MARKET_PATTERN),
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
            market=market,
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
