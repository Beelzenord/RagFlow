"""Login gate for the web console.

The browser only ever talks to this service, so one middleware here covers
uploads, questions, deletes and file downloads at once. `ingestion` and `query`
keep their own `x-api-key` check and are not reachable from the browser.

Two modes, selected by AUTH_MODE:

  password  One shared account from ADMIN_USERNAME/ADMIN_PASSWORD, checked
            against a signed session cookie. For local runs.
  entra     Container Apps authentication ("Easy Auth") has already signed the
            person in with their work account, and identifies them in a request
            header. For Azure. Trusting a header is only safe because the
            platform sets it: Easy Auth strips any client-supplied copy before
            the request reaches this container. Never set entra locally, where
            nothing strips it.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

log = logging.getLogger("web.auth")

AUTH_MODE = os.environ.get("AUTH_MODE", "password").strip().lower()
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

# Set by Easy Auth on every authenticated request.
PRINCIPAL_NAME_HEADER = "x-ms-client-principal-name"
PRINCIPAL_HEADER = "x-ms-client-principal"

# Easy Auth's own endpoints, served by the platform rather than this app.
ENTRA_LOGIN_URL = "/.auth/login/aad"
ENTRA_LOGOUT_URL = "/.auth/logout"

# /healthz is the Container Apps probe and must answer before login. The login
# page needs its own stylesheet, and browsers ask for the favicon unprompted.
# /api/me is public so the UI can always find out where to send someone to sign
# in; it names no user unless the request is already authenticated.
PUBLIC_PATHS = frozenset(
    {"/healthz", "/login", "/api/login", "/api/me", "/styles.css", "/favicon.ico"}
)

SESSION_KEY = "admin"

# Claims Entra may carry the sign-in name in, best first.
_NAME_CLAIMS = (
    "preferred_username",
    "upn",
    "email",
    "emails",
    "name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
)


def entra_mode() -> bool:
    return AUTH_MODE == "entra"


def auth_enabled() -> bool:
    """Whether this app checks credentials itself. False in entra mode: the
    platform already did, and there is no password to check."""
    return not entra_mode() and bool(ADMIN_PASSWORD)


def describe_mode() -> str:
    """What the UI should assume: "entra", "password", or "none" (open)."""
    if entra_mode():
        return "entra"
    return "password" if auth_enabled() else "none"


def logout_url() -> str | None:
    """Where "Log out" should send the browser, or None when there is no session
    to end. In entra mode this has to be the platform endpoint - clearing our
    own cookie would leave the person signed in to Microsoft."""
    if entra_mode():
        return ENTRA_LOGOUT_URL
    return "/login" if auth_enabled() else None


def login_url() -> str:
    return ENTRA_LOGIN_URL if entra_mode() else "/login"


def log_startup_state() -> None:
    if entra_mode():
        log.info("auth mode: entra - Container Apps authentication identifies the user")
        if ADMIN_PASSWORD:
            log.warning(
                "ADMIN_PASSWORD is set but ignored in entra mode - remove it to avoid confusion"
            )
        return
    if not auth_enabled():
        log.warning("ADMIN_PASSWORD is empty - the console is open to anyone who can reach it")
        return
    log.info("auth mode: password - login gate active for user %r", ADMIN_USERNAME)
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


def _claim_name(encoded: str) -> str | None:
    """Dig a display name out of the base64 principal blob Easy Auth sends."""
    try:
        # Standard base64 without the trailing padding.
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError):
        return None

    claims: dict[str, str] = {}
    for claim in payload.get("claims") or ():
        typ, val = claim.get("typ"), claim.get("val")
        if typ and val and typ not in claims:
            claims[typ] = val
    for key in _NAME_CLAIMS:
        if claims.get(key):
            return claims[key]
    return None


def entra_user(request: Request) -> str | None:
    """The signed-in work account, or None if the platform did not authenticate
    this request."""
    name = request.headers.get(PRINCIPAL_NAME_HEADER)
    if name:
        return name.strip() or None
    encoded = request.headers.get(PRINCIPAL_HEADER)
    return _claim_name(encoded) if encoded else None


def current_user(request: Request) -> str | None:
    if entra_mode():
        return entra_user(request)
    if auth_enabled():
        return ADMIN_USERNAME if is_logged_in(request) else None
    return None


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def sign_in(request: Request) -> None:
    request.session[SESSION_KEY] = True


def sign_out(request: Request) -> None:
    request.session.clear()


async def require_login(request: Request, call_next) -> Response:
    """Block everything but PUBLIC_PATHS until the request carries an identity."""
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)

    if entra_mode():
        # Easy Auth normally rejects anonymous callers before this code runs.
        # Checking anyway means a misconfigured "allow unauthenticated" setting
        # fails closed instead of publishing the whole corpus.
        if entra_user(request):
            return await call_next(request)
        return _challenge(request, ENTRA_LOGIN_URL)

    if not auth_enabled() or is_logged_in(request):
        return await call_next(request)
    return _challenge(request, "/login")


def _challenge(request: Request, redirect_to: str) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login required"}, status_code=401)
    return RedirectResponse(redirect_to, status_code=302)
