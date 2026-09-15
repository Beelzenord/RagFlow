"""Scope reaches ingestion, and an unknown code never becomes a document.

The BFF holds no database, so it cannot validate a scope code itself - it
forwards and lets the ingestion service refuse. These tests pin that contract:
what is sent, that it is admin-only, and that a refusal is passed back rather
than swallowed.
"""
from __future__ import annotations

import unittest
from unittest import mock

try:
    import httpx
    from fastapi.testclient import TestClient

    HTTP_DEPS = True
except ImportError:  # pragma: no cover - depends on the environment
    HTTP_DEPS = False


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class UploadScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import main

        self.main = main

    def upload(self, capture: list[dict], status: int = 202, body: dict | None = None, **form: str):
        """Post a file, recording the form the BFF forwards to ingestion."""
        with TestClient(self.main.app, raise_server_exceptions=False) as client:

            async def fake_post(url: str, **kw: object) -> httpx.Response:
                capture.append(dict(kw.get("data") or {}))
                request = httpx.Request("POST", url)
                return httpx.Response(status, json=body or {"document_id": "x"}, request=request)

            setattr(client.app.state.http, "post", fake_post)
            return client.post(
                "/api/upload",
                files={"file": ("a.pdf", b"%PDF-1.4 test", "application/pdf")},
                data=form,
            )

    def test_scope_is_forwarded(self) -> None:
        sent: list[dict] = []
        resp = self.upload(sent, scope="EU,CH")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(sent[0]["scope"], "EU,CH")

    def test_absent_scope_is_not_invented(self) -> None:
        """An upload with no scope must not acquire one on the way through."""
        sent: list[dict] = []
        self.upload(sent)
        self.assertNotIn("scope", sent[0])

    def test_a_refused_scope_is_passed_back(self) -> None:
        """The vocabulary lives in one place. A 400 from there has to reach the
        person who mistyped, not be turned into a success."""
        sent: list[dict] = []
        resp = self.upload(sent, status=400, body={"detail": "unknown scope(s): BOGUS"}, scope="BOGUS")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("BOGUS", resp.json()["detail"])

    def test_upload_is_admin_only(self) -> None:
        from app import auth as auth_mod

        sent: list[dict] = []
        with mock.patch.object(auth_mod, "is_admin", return_value=False):
            resp = self.upload(sent, scope="EU")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(sent, [], "a reader must not reach ingestion at all")


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class ScopeVocabularyTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import main

        self.main = main

    def test_scopes_are_proxied_not_read_locally(self) -> None:
        """This service has no database by design; the vocabulary is fetched."""
        with TestClient(self.main.app, raise_server_exceptions=False) as client:
            seen: list[str] = []

            async def fake_get(url: str, **kw: object) -> httpx.Response:
                seen.append(url)
                return httpx.Response(
                    200,
                    json={"groups": [{"code": "EU", "label": "European Union"}], "regions": []},
                    request=httpx.Request("GET", url),
                )

            setattr(client.app.state.http, "get", fake_get)
            resp = client.get("/api/scopes")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(seen[0].endswith("/scopes"))
        self.assertEqual(resp.json()["groups"][0]["code"], "EU")

    def test_scopes_are_admin_only(self) -> None:
        from app import auth as auth_mod

        with TestClient(self.main.app, raise_server_exceptions=False) as client:
            with mock.patch.object(auth_mod, "is_admin", return_value=False):
                resp = client.get("/api/scopes")
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
