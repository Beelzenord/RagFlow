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


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class FilingProxyTests(unittest.TestCase):
    """Retag and the country catalogue reach ingestion intact, admin-only.

    The BFF holds no database, so these routes are relays. What has to hold is
    that the right call goes upstream, that a reader never causes one, and that
    an upstream refusal - 400 for a bad scope, 409 for a country still in use -
    comes back carrying its explanation instead of becoming a generic failure.
    """

    def setUp(self) -> None:
        from app import main

        self.main = main

    def call(self, method: str, path: str, *, status: int = 200, body=None, reader=False, **kw):
        """Make one request with ingestion stubbed, returning (response, upstream calls)."""
        from app import auth as auth_mod

        calls: list[dict] = []

        async def fake_request(m: str, url: str, **kwargs: object) -> httpx.Response:
            calls.append({"method": m, "url": url, "json": kwargs.get("json")})
            return httpx.Response(
                status, json=body if body is not None else {}, request=httpx.Request(m, url)
            )

        with TestClient(self.main.app, raise_server_exceptions=False) as client:
            setattr(client.app.state.http, "request", fake_request)
            if reader:
                with mock.patch.object(auth_mod, "is_admin", return_value=False):
                    resp = client.request(method, path, follow_redirects=False, **kw)
            else:
                resp = client.request(method, path, follow_redirects=False, **kw)
        return resp, calls

    # ---- retag -------------------------------------------------------------

    def test_single_retag_is_forwarded(self) -> None:
        doc = "11111111-1111-1111-1111-111111111111"
        resp, calls = self.call(
            "PATCH", f"/api/documents/{doc}/scope", json={"scope": "EU,CH"},
            body={"scope_labels": ["European Union", "Switzerland"]},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls[0]["method"], "PATCH")
        self.assertTrue(calls[0]["url"].endswith(f"/documents/{doc}/scope"))
        self.assertEqual(calls[0]["json"], {"scope": "EU,CH"})

    def test_bulk_retag_is_forwarded(self) -> None:
        ids = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
        resp, calls = self.call(
            "PATCH", "/api/documents/scope", json={"document_ids": ids, "scope": "NORDICS"},
            body={"updated": 2},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(calls[0]["url"].endswith("/documents/scope"))
        self.assertEqual(calls[0]["json"], {"document_ids": ids, "scope": "NORDICS"})

    def test_bulk_retag_needs_at_least_one_document(self) -> None:
        resp, calls = self.call("PATCH", "/api/documents/scope", json={"document_ids": [], "scope": "EU"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(calls, [], "an empty selection must not reach ingestion")

    def test_a_refused_scope_comes_back_with_its_reason(self) -> None:
        doc = "11111111-1111-1111-1111-111111111111"
        resp, _ = self.call(
            "PATCH", f"/api/documents/{doc}/scope", json={"scope": "JP"},
            status=400, body={"detail": "JP is not enabled - enable it on the Countries page"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not enabled", resp.json()["detail"])

    def test_retag_is_admin_only(self) -> None:
        doc = "11111111-1111-1111-1111-111111111111"
        for method, path, payload in [
            ("PATCH", f"/api/documents/{doc}/scope", {"scope": "EU"}),
            ("PATCH", "/api/documents/scope", {"document_ids": [doc], "scope": "EU"}),
        ]:
            with self.subTest(path=path):
                resp, calls = self.call(method, path, json=payload, reader=True)
                self.assertEqual(resp.status_code, 403)
                self.assertEqual(calls, [], "a reader must not reach ingestion at all")

    # ---- countries ---------------------------------------------------------

    def test_country_catalogue_is_forwarded(self) -> None:
        resp, calls = self.call("GET", "/api/regions", body={"regions": [{"code": "JP"}]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(calls[0]["url"].endswith("/regions"))

    def test_enabling_a_country_is_forwarded_uppercased(self) -> None:
        resp, calls = self.call("PATCH", "/api/regions/jp", json={"active": True},
                                body={"code": "JP", "active": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(calls[0]["url"].endswith("/regions/JP"))
        self.assertEqual(calls[0]["json"], {"active": True})

    def test_a_malformed_country_code_never_leaves_the_bff(self) -> None:
        for bad in ["JPN", "J", "1A"]:
            with self.subTest(code=bad):
                resp, calls = self.call("PATCH", f"/api/regions/{bad}", json={"active": True})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(calls, [])

    def test_refusing_to_disable_a_used_country_keeps_its_explanation(self) -> None:
        resp, _ = self.call(
            "PATCH", "/api/regions/DE", json={"active": False}, status=409,
            body={"detail": {"message": "Germany is used by the DACH, EU groups",
                             "documents_using": 0, "groups": ["DACH", "EU"]}},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"]["groups"], ["DACH", "EU"])

    def test_countries_are_admin_only(self) -> None:
        for method, path, payload in [
            ("GET", "/api/regions", None),
            ("PATCH", "/api/regions/JP", {"active": True}),
        ]:
            with self.subTest(path=path):
                resp, calls = self.call(method, path, json=payload, reader=True)
                self.assertEqual(resp.status_code, 403)
                self.assertEqual(calls, [])

    def test_backfill_is_admin_only(self) -> None:
        resp, calls = self.call("POST", "/api/documents/backfill-fingerprints", reader=True)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(calls, [])

    # ---- the page ----------------------------------------------------------

    def test_admin_gets_the_filing_page(self) -> None:
        resp, _ = self.call("GET", "/admin")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Filing", resp.text)

    def test_a_reader_is_sent_back_to_the_chat(self) -> None:
        resp, _ = self.call("GET", "/admin", reader=True)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/")

    def test_for_a_reader_the_page_script_does_not_exist(self) -> None:
        resp, _ = self.call("GET", "/admin/admin.js", reader=True)
        self.assertEqual(resp.status_code, 404)

    def test_the_page_is_not_reachable_through_the_static_mount(self) -> None:
        """The static mount serves anything in static/ to every signed-in user.
        The admin files live outside it, so these paths must not resolve."""
        for path in ["/admin.html", "/admin.js", "/static/admin.html"]:
            with self.subTest(path=path):
                resp, _ = self.call("GET", path, reader=True)
                self.assertEqual(resp.status_code, 404)


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class DocumentListParamsTests(unittest.TestCase):
    def test_filters_are_forwarded_and_empty_ones_dropped(self) -> None:
        from app import main

        seen: list[dict] = []
        with TestClient(main.app, raise_server_exceptions=False) as client:

            async def fake_get(url: str, **kw: object) -> httpx.Response:
                seen.append(dict(kw.get("params") or {}))
                return httpx.Response(200, json={"documents": [], "total": 0},
                                      request=httpx.Request("GET", url))

            setattr(client.app.state.http, "get", fake_get)
            resp = client.get(
                "/api/documents",
                params={"q": "faktura", "scope": "unscoped", "sort": "attention",
                        "limit": 25, "offset": 25, "status": ""},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            seen[0],
            {"q": "faktura", "scope": "unscoped", "sort": "attention", "limit": 25, "offset": 25},
        )


if __name__ == "__main__":
    unittest.main()
