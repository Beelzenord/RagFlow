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

Past the gate, a request is either admin - allowed to upload and delete - or
reader, allowed only to ask questions. In entra mode that comes from the Entra
app role in the principal blob; locally there is a single account and it is the
admin.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app import principal

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

# Session field holding the role. Named "admin" from before roles existed;
# renaming it now would sign every open browser out.
SESSION_KEY = "admin"

# The app roles defined on the Entra app registration Easy Auth signs people in
# with. Names, not group GUIDs: a role survives the group being renamed or
# replaced, and it stays in the token where a long group list would be dropped.
ADMIN_ROLE_VALUE = os.environ.get("ENTRA_ADMIN_ROLE", "").strip() or "Admin"
READER_ROLE_VALUE = os.environ.get("ENTRA_READER_ROLE", "").strip() or "Reader"

# Local only: pins every request to one role so a single account can exercise
# the reader UI without a second login. Ignored in entra mode, where the token
# decides, so it cannot weaken a deployment.
_FORCE_ROLE_RAW = os.environ.get("DEV_FORCE_ROLE", "").strip().lower()
FORCED_ROLE = _FORCE_ROLE_RAW if _FORCE_ROLE_RAW in principal.ROLES else ""


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
    if _FORCE_ROLE_RAW and not FORCED_ROLE:
        log.warning(
            "DEV_FORCE_ROLE=%r is not one of %s - ignoring it",
            _FORCE_ROLE_RAW,
            ", ".join(principal.ROLES),
        )
    if entra_mode():
        log.info(
            "auth mode: entra - Container Apps authentication identifies the user, "
            "app roles %r and %r decide what they may do",
            ADMIN_ROLE_VALUE,
            READER_ROLE_VALUE,
        )
        if ADMIN_PASSWORD:
            log.warning(
                "ADMIN_PASSWORD is set but ignored in entra mode - remove it to avoid confusion"
            )
        if _FORCE_ROLE_RAW:
            log.warning("DEV_FORCE_ROLE is ignored in entra mode - the token decides the role")
        return
    if FORCED_ROLE:
        log.warning(
            "DEV_FORCE_ROLE=%s - every request is treated as %s, for local testing only",
            FORCED_ROLE,
            FORCED_ROLE,
        )
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


def entra_user(request: Request) -> str | None:
    """The signed-in work account, or None if the platform did not authenticate
    this request."""
    name = request.headers.get(PRINCIPAL_NAME_HEADER)
    if name:
        return name.strip() or None
    return principal.name_from_principal(_entra_principal(request))


def _entra_principal(request: Request) -> dict | None:
    return principal.decode_principal(request.headers.get(PRINCIPAL_HEADER))


# Accounts already warned about. An idle browser polls every few seconds, so
# without this one unassigned person would fill the log on their own.
_UNASSIGNED_SEEN: set[str] = set()


def _warn_unassigned(request: Request) -> None:
    """Say out loud that someone got the reader fallback because Entra sent no
    role - the likeliest reason a person reports a missing Upload panel."""
    who = entra_user(request) or "an unnamed account"
    if who in _UNASSIGNED_SEEN:
        return
    if len(_UNASSIGNED_SEEN) > 500:
        _UNASSIGNED_SEEN.clear()
    _UNASSIGNED_SEEN.add(who)
    log.warning(
        "%s holds neither the %r nor the %r app role - treating as reader",
        who,
        ADMIN_ROLE_VALUE,
        READER_ROLE_VALUE,
    )


def role(request: Request) -> str:
    """"admin" or "reader" for this request, defaulting to the lesser of the two."""
    if entra_mode():
        payload = _entra_principal(request)
        if payload is not None and not principal.granted_roles(
            payload, ADMIN_ROLE_VALUE, READER_ROLE_VALUE
        ):
            _warn_unassigned(request)
        return principal.resolve_role(payload, ADMIN_ROLE_VALUE, READER_ROLE_VALUE)
    if FORCED_ROLE:
        return FORCED_ROLE
    if not auth_enabled():
        # Open console: there is no identity to grade, so keep local runs usable.
        return principal.ADMIN
    return _session_role(request) if is_logged_in(request) else principal.READER


def is_admin(request: Request) -> bool:
    return role(request) == principal.ADMIN


def current_user(request: Request) -> str | None:
    if entra_mode():
        return entra_user(request)
    if auth_enabled():
        return ADMIN_USERNAME if is_logged_in(request) else None
    return None


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def _session_role(request: Request) -> str:
    # Sessions minted before roles existed stored True rather than a role name.
    stored = request.session.get(SESSION_KEY)
    return stored if stored in principal.ROLES else principal.ADMIN


def sign_in(request: Request) -> None:
    # The one local account owns the console, so signing in grants admin.
    request.session[SESSION_KEY] = principal.ADMIN


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
