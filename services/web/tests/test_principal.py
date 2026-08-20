"""Fixture tests for the Easy Auth principal parser.

Every case builds the same base64 header Container Apps authentication sends, so
role resolution can be checked without Azure, a container, or `fastapi`:

    cd services/web && python3 -m unittest discover -s tests -t .

The rule under test is least privilege: only an explicit Admin assignment grants
write access, and every other shape of input - unassigned, unknown role, garbled
blob - has to come back as reader.
"""
from __future__ import annotations

import base64
import json
import unittest

from app import principal

ADMIN_VALUE = "Admin"
READER_VALUE = "Reader"

JWT_ROLE = "roles"
WSFED_ROLE = "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"
CUSTOM_ROLE = "urn:contoso:claims:approle"

UPN = "preferred_username"


def encode(claims: list[tuple[str, str]], role_typ: str | None = None) -> str:
    """A principal header holding `claims`, padding stripped like Easy Auth does."""
    payload: dict = {
        "auth_typ": "aad",
        "claims": [{"typ": typ, "val": val} for typ, val in claims],
    }
    if role_typ is not None:
        payload["role_typ"] = role_typ
    raw = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return raw.rstrip("=")


NAME_CLAIM = (UPN, "someone@contoso.com")

# (what it is, header value, expected role)
ROLE_CASES: list[tuple[str, str | None, str]] = [
    (
        "an admin assignment",
        encode([NAME_CLAIM, (JWT_ROLE, ADMIN_VALUE)]),
        principal.ADMIN,
    ),
    (
        "a reader assignment",
        encode([NAME_CLAIM, (JWT_ROLE, READER_VALUE)]),
        principal.READER,
    ),
    (
        "both roles at once - admin outranks reader",
        encode([NAME_CLAIM, (JWT_ROLE, READER_VALUE), (JWT_ROLE, ADMIN_VALUE)]),
        principal.ADMIN,
    ),
    (
        "the same two in the other order",
        encode([NAME_CLAIM, (JWT_ROLE, ADMIN_VALUE), (JWT_ROLE, READER_VALUE)]),
        principal.ADMIN,
    ),
    (
        "casing Entra did not promise to keep",
        encode([NAME_CLAIM, (JWT_ROLE, "aDMIn")]),
        principal.ADMIN,
    ),
    (
        "the WS-Federation role claim Easy Auth also emits",
        encode([NAME_CLAIM, (WSFED_ROLE, ADMIN_VALUE)]),
        principal.ADMIN,
    ),
    (
        "a tenant-specific claim type named by role_typ",
        encode([NAME_CLAIM, (CUSTOM_ROLE, ADMIN_VALUE)], role_typ=CUSTOM_ROLE),
        principal.ADMIN,
    ),
    (
        "role_typ pointing at a type the blob does not carry",
        encode([NAME_CLAIM, (JWT_ROLE, ADMIN_VALUE)], role_typ=CUSTOM_ROLE),
        principal.ADMIN,
    ),
    (
        "an account assigned to no app role",
        encode([NAME_CLAIM]),
        principal.READER,
    ),
    (
        "a role this app knows nothing about",
        encode([NAME_CLAIM, (JWT_ROLE, "Contributor")]),
        principal.READER,
    ),
    (
        "a role value hiding in some unrelated claim type",
        encode([NAME_CLAIM, ("groups", ADMIN_VALUE)]),
        principal.READER,
    ),
    ("no claims at all", encode([]), principal.READER),
    ("no header", None, principal.READER),
    ("an empty header", "", principal.READER),
    ("base64 that does not decode", "not base64 at all!!", principal.READER),
    (
        "valid base64 that is not JSON",
        base64.b64encode(b"plain text").decode("ascii"),
        principal.READER,
    ),
    (
        "JSON that is not an object",
        base64.b64encode(b'["Admin"]').decode("ascii"),
        principal.READER,
    ),
    (
        "an object whose claims are the wrong shape",
        base64.b64encode(b'{"claims": ["Admin", {"typ": 1, "val": 2}]}').decode("ascii"),
        principal.READER,
    ),
]


class ResolveRoleTests(unittest.TestCase):
    def resolve(self, header: str | None) -> str:
        payload = principal.decode_principal(header)
        return principal.resolve_role(payload, ADMIN_VALUE, READER_VALUE)

    def test_role_resolution(self) -> None:
        for label, header, expected in ROLE_CASES:
            with self.subTest(label):
                self.assertEqual(self.resolve(header), expected)

    def test_nothing_but_admin_can_write(self) -> None:
        """The guarantee the upload and delete routes lean on."""
        for label, header, expected in ROLE_CASES:
            if expected == principal.ADMIN:
                continue
            with self.subTest(label):
                self.assertNotEqual(self.resolve(header), principal.ADMIN)

    def test_renamed_app_roles(self) -> None:
        """Deployments that prefix their roles still resolve, and the old names
        stop working - the role value is configuration, not a constant."""
        header = encode([NAME_CLAIM, (JWT_ROLE, "RAG.Admin")])
        payload = principal.decode_principal(header)
        self.assertEqual(
            principal.resolve_role(payload, "RAG.Admin", "RAG.Reader"),
            principal.ADMIN,
        )
        self.assertEqual(
            principal.resolve_role(payload, ADMIN_VALUE, READER_VALUE),
            principal.READER,
        )

    def test_granted_roles_separates_reader_from_unassigned(self) -> None:
        assigned = principal.decode_principal(encode([NAME_CLAIM, (JWT_ROLE, READER_VALUE)]))
        unassigned = principal.decode_principal(encode([NAME_CLAIM]))
        self.assertEqual(
            principal.granted_roles(assigned, ADMIN_VALUE, READER_VALUE),
            {principal.READER},
        )
        self.assertEqual(principal.granted_roles(unassigned, ADMIN_VALUE, READER_VALUE), set())


class ClaimTests(unittest.TestCase):
    def test_repeated_claim_types_are_kept(self) -> None:
        payload = principal.decode_principal(
            encode([(JWT_ROLE, READER_VALUE), (JWT_ROLE, ADMIN_VALUE)])
        )
        self.assertEqual(
            principal.claim_values(payload)[JWT_ROLE], [READER_VALUE, ADMIN_VALUE]
        )

    def test_roles_are_deduplicated_across_claim_types(self) -> None:
        payload = principal.decode_principal(
            encode([(JWT_ROLE, ADMIN_VALUE), (WSFED_ROLE, ADMIN_VALUE)])
        )
        self.assertEqual(principal.roles_from_principal(payload), [ADMIN_VALUE])

    def test_name_prefers_the_sign_in_claim(self) -> None:
        payload = principal.decode_principal(
            encode([("name", "Some One"), (UPN, "someone@contoso.com")])
        )
        self.assertEqual(principal.name_from_principal(payload), "someone@contoso.com")

    def test_name_falls_back_through_the_claim_list(self) -> None:
        payload = principal.decode_principal(encode([("name", "Some One")]))
        self.assertEqual(principal.name_from_principal(payload), "Some One")

    def test_name_is_none_without_a_naming_claim(self) -> None:
        payload = principal.decode_principal(encode([(JWT_ROLE, ADMIN_VALUE)]))
        self.assertIsNone(principal.name_from_principal(payload))
        self.assertIsNone(principal.name_from_principal(None))


if __name__ == "__main__":
    unittest.main()
