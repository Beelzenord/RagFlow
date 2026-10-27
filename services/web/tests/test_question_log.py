"""The question log through the web service: everyone may rate an answer, only
admins see Insights.

    cd services/web && python3 -m unittest discover -s tests -t .

Skipped where fastapi and httpx are not installed.
"""
from __future__ import annotations

import unittest

from tests.test_markets import HTTP_DEPS, _Harness

ANSWER = "0f5c5d0e-8a52-4f0b-9d55-4a3b1c3e8f21"


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class FeedbackTests(_Harness):
    def test_a_reader_can_rate_an_answer(self) -> None:
        client = self.client("reader")
        resp = client.post(f"/api/answers/{ANSWER}/feedback", json={"rating": -1, "comment": "No Norway"})
        self.assertEqual(resp.status_code, 200, resp.text)
        [call] = self.calls
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{self.main.QUERY_URL}/answers/{ANSWER}/feedback")
        self.assertEqual(call["json"], {"rating": -1, "comment": "No Norway"})
        self.assertIn("x-api-key", call["headers"])

    def test_nothing_about_who_rated_is_passed_on(self) -> None:
        client = self.client("reader")
        client.post(f"/api/answers/{ANSWER}/feedback", json={"rating": 1})
        self.assertEqual(self.calls[0]["json"], {"rating": 1, "comment": None})
        self.assertEqual(set(self.calls[0]["headers"]), {"x-api-key"})

    def test_bad_ratings_and_ids_never_reach_the_query_service(self) -> None:
        client = self.client("admin")
        cases = [
            (f"/api/answers/{ANSWER}/feedback", {"rating": 0}),
            (f"/api/answers/{ANSWER}/feedback", {"rating": 5}),
            (f"/api/answers/{ANSWER}/feedback", {"rating": -1, "comment": "x" * 501}),
            ("/api/answers/not-an-id/feedback", {"rating": 1}),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=str(body)[:40]):
                self.assertEqual(client.post(path, json=body).status_code, 422)
        self.assertEqual(self.calls, [])

    def test_the_chat_cannot_opt_out_of_the_log(self) -> None:
        # "log": false is for test traffic sent with the service key; the web
        # service drops it, so what people ask is always counted.
        self.reply = (200, {"answer": "", "citations": []})
        client = self.client("reader")
        client.post("/api/query", json={"question": "Dress code?", "log": False})
        self.assertNotIn("log", self.calls[0]["json"])

    def test_an_unknown_answer_is_a_404(self) -> None:
        self.reply = (404, {"detail": "answer not found"})
        client = self.client("reader")
        resp = client.post(f"/api/answers/{ANSWER}/feedback", json={"rating": 1})
        self.assertEqual(resp.status_code, 404)


@unittest.skipUnless(HTTP_DEPS, "needs fastapi and httpx")
class InsightsTests(_Harness):
    def test_a_reader_is_refused_without_calling_the_query_service(self) -> None:
        client = self.client("reader")
        self.assertEqual(client.get("/api/insights").status_code, 403)
        self.assertEqual(self.calls, [])

    def test_an_admin_gets_them_with_the_filters(self) -> None:
        self.reply = (200, {"summary": {}, "groups": []})
        client = self.client("admin")
        resp = client.get("/api/insights?days=7&market=SE&kind=all&limit=500")
        self.assertEqual(resp.status_code, 200, resp.text)
        [call] = self.calls
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], f"{self.main.QUERY_URL}/insights")
        self.assertEqual(call["params"], {"days": 7, "market": "SE", "kind": "all", "limit": 500})

    def test_defaults(self) -> None:
        client = self.client("admin")
        client.get("/api/insights")
        self.assertEqual(self.calls[0]["params"], {"days": 30, "market": "", "kind": "attention", "limit": 100})

    def test_all_markets_and_none(self) -> None:
        client = self.client("admin")
        client.get("/api/insights?market=none")
        self.assertEqual(self.calls[0]["params"]["market"], "none")

    def test_bad_filters_are_refused(self) -> None:
        client = self.client("admin")
        for query in ("days=0", "days=1000", "market=se", "market=Sweden", "kind=everything", "limit=501"):
            with self.subTest(query=query):
                self.assertEqual(client.get(f"/api/insights?{query}").status_code, 422)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
