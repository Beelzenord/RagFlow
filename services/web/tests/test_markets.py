"""Markets, who a document is for, and the dashboard: role gating and forwarding.

    cd services/web && python3 -m unittest discover -s tests -t .

The web service holds no rules of its own here - ingestion does - so these
check the two things it is responsible for: a reader is refused (or, for the
dashboard, never told it exists), and an admin's call reaches ingestion intact,
refusals included. Skipped where fastapi and httpx are not installed.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

try:
    import httpx
    from fastapi.testclient import TestClient

    HTTP_DEPS = True
except ImportError:  # pragma: no cover - depends on the environment
    HTTP_DEPS = False

GROUP = "2beda27b-7389-496e-94e2-3f0e62268cef"
DOC = "ca35dd84-0dfe-4f63-9153-e35a213ffdd6"

MARKETS = {
    "countries": [
        {"code": "SE", "name": "Sweden", "documents": 3, "direct_documents": 1, "groups": ["Scandinavia"]},
    ],
    "groups": [{"id": GROUP, "name": "Scandinavia", "members": ["SE"], "documents": 2}],
    "global_documents": 9,
}

# Every route that changes markets or audiences, or reveals the catalogue.
ADMIN_ROUTES = [
    ("GET", "/api/markets/catalogue", None),
    ("POST", "/api/markets/countries", {"code": "se"}),
    ("DELETE", "/api/markets/countries/SE", None),
    ("POST", "/api/markets/groups", {"name": "Scandinavia", "members": ["SE"]}),
    ("PUT", f"/api/markets/groups/{GROUP}", {"name": "Scandinavia", "members": ["SE"]}),
    ("DELETE", f"/api/markets/groups/{GROUP}", None),
    ("PUT", f"/api/documents/{DOC}/audience", {"everyone": True}),
    ("PUT", "/api/documents/audience", {"document_ids": [DOC], "everyone": True}),
    ("POST", "/api/documents/rechunk-all", None),
    ("PATCH", f"/api/documents/{DOC}", {"filename": "Sick leave policy"}),
]


class _Harness(unittest.TestCase):
    """A TestClient with a chosen role, and ingestion/query stubbed out."""

    def setUp(self) -> None:
        from app import auth, main

        self.main = main
        self.auth = auth
        self.calls: list[dict] = []
        self.reply: tuple[int, object] = (200, {"ok": True})

    def client(self, role: str) -> TestClient:
        patcher = mock.patch.object(self.auth, "FORCED_ROLE", role)
        patcher.start()
        self.addCleanup(patcher.stop)
        client = TestClient(self.main.app, raise_server_exceptions=False)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)

        async def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
            self.calls.append({"method": method, "url": url, **kwargs})
            status, body = self.reply
            return httpx.Response(status, json=body, request=httpx.Request(method, url))

        async def fake_post(url: str, **kwargs: object) -> httpx.Response:
            self.calls.append({"method": "POST", "url": url, **kwargs})
            status, body = self.reply
            return httpx.Response(status, json=body, request=httpx.Request("POST", url))

        self.main.app.state.http.request = fake_request
        self.main.app.state.http.post = fake_post
        return client

    def send(self, client: TestClient, method: str, path: str, body: object = None):
        return client.request(method, path, json=body) if body is not None else client.request(method, path)


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class MarketsProxyTests(_Harness):

    def test_every_write_route_refuses_a_reader_without_calling_ingestion(self) -> None:
        client = self.client("reader")
        for method, path, body in ADMIN_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                resp = self.send(client, method, path, body)
                self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_every_write_route_forwards_for_an_admin(self) -> None:
        client = self.client("admin")
        for method, path, body in ADMIN_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                self.calls.clear()
                resp = self.send(client, method, path, body)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(len(self.calls), 1)
                call = self.calls[0]
                self.assertEqual(call["method"], method)
                self.assertEqual(call["url"], self.main.INGESTION_URL + path.removeprefix("/api"))
                self.assertIn("x-api-key", call["headers"])

    def test_country_code_is_upper_cased_before_forwarding(self) -> None:
        client = self.client("admin")
        client.post("/api/markets/countries", json={"code": "se"})
        self.assertEqual(self.calls[0]["json"], {"code": "SE"})

    def test_a_malformed_country_code_never_reaches_ingestion(self) -> None:
        client = self.client("admin")
        resp = client.delete("/api/markets/countries/S1")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_bulk_document_ids_travel_as_strings(self) -> None:
        client = self.client("admin")
        client.put(
            "/api/documents/audience",
            json={"document_ids": [DOC], "everyone": False, "countries": ["SE"]},
        )
        self.assertEqual(
            self.calls[0]["json"],
            {"document_ids": [DOC], "everyone": False, "countries": ["SE"], "groups": []},
        )

    def test_a_rename_carries_only_the_name(self) -> None:
        client = self.client("admin")
        client.patch(f"/api/documents/{DOC}", json={"filename": "Sick leave policy", "status": "completed"})
        self.assertEqual(self.calls[0]["json"], {"filename": "Sick leave policy"})

    def test_a_malformed_rename_never_reaches_ingestion(self) -> None:
        client = self.client("admin")
        cases = [
            (f"/api/documents/{DOC}", {"filename": ""}),
            (f"/api/documents/{DOC}", {"filename": "x" * 256}),
            (f"/api/documents/{DOC}", {}),
            ("/api/documents/not-an-id", {"filename": "Policy"}),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=str(body)[:30]):
                self.assertEqual(client.patch(path, json=body).status_code, 422)
        self.assertEqual(self.calls, [])

    def test_a_refusal_reaches_the_dashboard_intact(self) -> None:
        # The 409 explaining why a country can't be removed is the message shown.
        refusal = {"detail": {"message": "SE can't be removed: it is in Scandinavia.", "groups": ["Scandinavia"]}}
        self.reply = (409, refusal)
        client = self.client("admin")
        resp = client.delete("/api/markets/countries/SE")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json(), refusal)

    def test_markets_list_is_trimmed_to_names_for_a_reader(self) -> None:
        self.reply = (200, MARKETS)
        client = self.client("reader")
        resp = client.get("/api/markets")
        self.assertEqual(resp.status_code, 200)
        # Usage counts and group make-up describe the corpus.
        self.assertEqual(resp.json(), {"countries": [{"code": "SE", "name": "Sweden"}]})

    def test_markets_list_is_complete_for_an_admin(self) -> None:
        self.reply = (200, MARKETS)
        client = self.client("admin")
        self.assertEqual(client.get("/api/markets").json(), MARKETS)

    def test_document_list_forwards_search_filter_and_paging(self) -> None:
        client = self.client("admin")
        calls: list[dict] = []

        async def fake_get(url: str, **kwargs: object) -> httpx.Response:
            calls.append({"url": url, **kwargs})
            return httpx.Response(200, json={"documents": [], "total": 0}, request=httpx.Request("GET", url))

        self.main.app.state.http.get = fake_get
        client.get("/api/documents?q=policy&who=SE&limit=25&offset=50")
        self.assertEqual(calls[0]["params"], {"q": "policy", "who": "SE", "limit": 25, "offset": 50})

    def test_document_list_forwards_scope_and_sort(self) -> None:
        client = self.client("admin")
        calls: list[dict] = []

        async def fake_get(url: str, **kwargs: object) -> httpx.Response:
            calls.append({"url": url, **kwargs})
            return httpx.Response(200, json={"documents": [], "total": 0}, request=httpx.Request("GET", url))

        self.main.app.state.http.get = fake_get
        client.get("/api/documents?who=SE&include_global=true&sort=name_asc")
        self.assertEqual(calls[0]["params"], {"who": "SE", "include_global": "true", "sort": "name_asc"})
        # Off is the default, so it is not sent at all.
        client.get("/api/documents?who=SE&include_global=false")
        self.assertEqual(calls[1]["params"], {"who": "SE"})


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class DashboardPageTests(_Harness):
    """To a reader the dashboard does not exist."""

    def test_reader_is_sent_back_to_the_chat(self) -> None:
        client = self.client("reader")
        resp = client.get("/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/")

    def test_reader_gets_no_script(self) -> None:
        client = self.client("reader")
        self.assertEqual(client.get("/dashboard/dashboard.js").status_code, 404)

    def test_reader_gets_no_styles(self) -> None:
        client = self.client("reader")
        self.assertEqual(client.get("/dashboard/dashboard.css").status_code, 404)

    def test_admin_gets_the_page_and_script(self) -> None:
        client = self.client("admin")
        page = client.get("/dashboard")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Who it's for", page.text)
        script = client.get("/dashboard/dashboard.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("javascript", script.headers["content-type"])
        styles = client.get("/dashboard/dashboard.css")
        self.assertEqual(styles.status_code, 200)
        self.assertIn("text/css", styles.headers["content-type"])

    def test_dashboard_files_are_not_in_the_public_static_folder(self) -> None:
        client = self.client("reader")
        for path in ("/dashboard.html", "/dashboard.js", "/dashboard.css", "/admin/dashboard.html"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)

    def test_chat_page_never_mentions_the_dashboard(self) -> None:
        # The Chat | Dashboard switch is built by shell.js for admins only, so
        # nothing in the page a reader receives - hidden or not - names it.
        client = self.client("reader")
        self.assertNotIn("dashboard", client.get("/").text.lower())


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class AnswersForTests(_Harness):
    """The chat's market reaches the query service, and nothing else does."""

    def test_market_is_forwarded_with_the_question(self) -> None:
        client = self.client("reader")
        self.reply = (200, {"answer": "", "citations": []})
        client.post("/api/query", json={"question": "fika?", "market": "SE"})
        self.assertEqual(self.calls[0]["json"]["market"], "SE")

    def test_a_malformed_market_is_refused(self) -> None:
        client = self.client("reader")
        for bad in ("sweden", "se", "S1"):
            with self.subTest(market=bad):
                resp = client.post("/api/query", json={"question": "fika?", "market": bad})
                self.assertEqual(resp.status_code, 422)
        self.assertEqual(self.calls, [])

    def test_no_market_means_none_is_sent(self) -> None:
        client = self.client("reader")
        self.reply = (200, {"answer": "", "citations": []})
        client.post("/api/query", json={"question": "fika?"})
        self.assertNotIn("market", self.calls[0]["json"])


if __name__ == "__main__":
    unittest.main()
