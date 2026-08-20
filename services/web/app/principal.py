"""Reads the identity blob Container Apps authentication puts on each request.

Easy Auth base64-encodes a small JSON document into `X-MS-CLIENT-PRINCIPAL`,
holding every claim from the person's token - including the app roles they are
assigned. This module only parses it; deciding what a role may do belongs in
`auth.py`.

Kept free of `fastapi` on purpose: the parsing is then unit testable on the host,
with no container and no dependencies installed.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

ADMIN = "admin"
READER = "reader"
ROLES = (ADMIN, READER)

# Claims Entra may carry the sign-in name in, best first.
NAME_CLAIMS = (
    "preferred_username",
    "upn",
    "email",
    "emails",
    "name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
)

# The blob names its own role claim in the top-level `role_typ`, but that field
# is not always populated. These are the two types app role assignments actually
# arrive as: the short JWT name, and the WS-Federation URI Easy Auth emits when
# it translates a token into the SOAP-style claim set.
_ROLE_CLAIMS = (
    "roles",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
)


def decode_principal(encoded: str | None) -> dict[str, Any] | None:
    """The claims document behind the header value, or None if it is unusable."""
    if not encoded:
        return None
    try:
        # Standard base64 without the trailing padding.
        payload = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (binascii.Error, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def claim_values(payload: dict[str, Any] | None) -> dict[str, list[str]]:
    """Claim values grouped by type, in the order they appear.

    A type can repeat - one entry per app role - so nothing may be collapsed to
    a single value here.
    """
    grouped: dict[str, list[str]] = {}
    for claim in (payload or {}).get("claims") or ():
        if not isinstance(claim, dict):
            continue
        typ, val = claim.get("typ"), claim.get("val")
        if isinstance(typ, str) and isinstance(val, str) and typ and val:
            grouped.setdefault(typ, []).append(val)
    return grouped


def name_from_principal(payload: dict[str, Any] | None) -> str | None:
    """A display name for the signed-in account, or None if no claim carries one."""
    grouped = claim_values(payload)
    for key in NAME_CLAIMS:
        values = grouped.get(key)
        if values:
            return values[0]
    return None


def roles_from_principal(payload: dict[str, Any] | None) -> list[str]:
    """Every app role the token was issued with, deduplicated."""
    grouped = claim_values(payload)
    declared = (payload or {}).get("role_typ")
    types = [declared] if isinstance(declared, str) and declared else []
    types += [t for t in _ROLE_CLAIMS if t != declared]

    roles: list[str] = []
    for typ in types:
        for value in grouped.get(typ, ()):
            if value not in roles:
                roles.append(value)
    return roles


def granted_roles(
    payload: dict[str, Any] | None,
    admin_value: str,
    reader_value: str,
) -> set[str]:
    """Which of the two configured app roles this principal actually holds.

    An empty set means the account was assigned neither, so a caller can tell
    "explicitly a reader" apart from "not assigned to the app at all" and say so
    in a log line.
    """
    known = {admin_value.casefold(): ADMIN, reader_value.casefold(): READER}
    held = (value.casefold() for value in roles_from_principal(payload))
    return {known[value] for value in held if value in known}


def resolve_role(
    payload: dict[str, Any] | None,
    admin_value: str,
    reader_value: str,
) -> str:
    """The role to act on, biased towards the lower privilege.

    An absent, unrecognised or undecodable roles claim is treated exactly like an
    account that was never assigned anything: it can read, and nothing more. The
    Admin role wins when a person holds both, which is what happens while someone
    is in the admin group and the reader group at once.
    """
    granted = granted_roles(payload, admin_value, reader_value)
    return ADMIN if ADMIN in granted else READER
