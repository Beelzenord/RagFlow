"""Single-account login gate for the web console.

The browser only ever talks to this service, so one middleware here covers
uploads, questions, deletes and file downloads at once. `ingestion` and `query`
keep their own `x-api-key` check and are not reachable from the browser.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

log = logging.getLogger("web.auth")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(7 * 24 * 3600)))
COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"}

# Signs the session cookie. A per-process fallback keeps local runs working, but
# it invalidates every session on restart and cannot be shared between replicas,
# so the Azure deploy passes a real value.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    _EPHEMERAL_SECRET = True
else:
    _EPHEMERAL_SECRET = False

# /healthz is the Container Apps probe and must answer before login. The login
# page needs its own stylesheet, and browsers ask for the favicon unprompted.
PUBLIC_PATHS = frozenset({"/healthz", "/login", "/api/login", "/styles.css", "/favicon.ico"})

SESSION_KEY = "admin"


def auth_enabled() -> bool:
    return bool(ADMIN_PASSWORD)


def log_startup_state() -> None:
    if not auth_enabled():
        log.warning("ADMIN_PASSWORD is empty - the console is open to anyone who can reach it")
        return
    log.info("login gate active for user %r", ADMIN_USERNAME)
    if _EPHEMERAL_SECRET:
        log.warning("SESSION_SECRET is empty - sessions end on restart and break across replicas")


async def check_credentials(username: str, password: str) -> bool:
    """Compare both fields in constant time, and slow failures down so the
    login form is not a fast oracle for guessing the password."""
    ok_user = hmac.compare_digest(username.encode(), ADMIN_USERNAME.encode())
    ok_pass = hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())
    if ok_user and ok_pass:
        return True
    await asyncio.sleep(0.5)
    return False


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def sign_in(request: Request) -> None:
    request.session[SESSION_KEY] = True


def sign_out(request: Request) -> None:
    request.session.clear()


async def require_login(request: Request, call_next) -> Response:
    """Block everything but PUBLIC_PATHS until a session cookie is present."""
    if not auth_enabled() or request.url.path in PUBLIC_PATHS or is_logged_in(request):
        return await call_next(request)

    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login required"}, status_code=401)
    return RedirectResponse("/login", status_code=302)
