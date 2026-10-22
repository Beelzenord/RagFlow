"""How retrieved sources are presented to the model, with and without markets.

Needs the query service's dependencies, so it is skipped where they are not
installed and run inside the container:

    docker compose run --rm --no-deps -v ./services/query/tests:/app/tests query \\
        python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest import mock

try:
    from sqlalchemy.exc import ProgrammingError

    from app import main

    DEPS = True
except Exception:  # pragma: no cover - depends on the environment
    DEPS = False

DOC_SE = "9b5a178a-0000-0000-0000-000000000001"
DOC_GLOBAL = "9b5a178a-0000-0000-0000-000000000003"


def row(doc: str, n: int, name: str, content: str) -> dict:
    return {
        "document_id": doc,
        "original_filename": name,
        "page_number": n,
        "heading": "3. Remote days",
        "content": content,
        "score": 0.3,
        "in_dense": True,
        "in_lexical": False,
    }


@unittest.skipUnless(DEPS, "needs the query service's dependencies")
class FormatSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.req = main.QueryRequest(question="How many remote days?")
        self.rows = [
            row(DOC_SE, 1, "remote-se.pdf", "Three days."),
            row(DOC_GLOBAL, 2, "handbook.pdf", "Ask your manager."),
        ]

    def test_without_markets_the_prompt_is_exactly_as_before(self) -> None:
        # The promise that keeps a corpus without markets unchanged in
        # behaviour and cost: the user message is byte-for-byte the old one.
        citations, user_msg = main._format_sources(self.req, self.rows, {}, None)
        expected = (
            "Question: How many remote days?\n\n"
            "Sources:\n"
            "[1] (remote-se.pdf, p.1 — 3. Remote days)\nThree days.\n\n"
            "[2] (handbook.pdf, p.2 — 3. Remote days)\nAsk your manager.\n\n"
            "Please answer the question using only these sources, in a warm and conversational "
            "tone. Do not include [#] citation markers in the answer."
        )
        self.assertEqual(user_msg, expected)
        self.assertTrue(all(c.audience is None for c in citations))

    def test_a_limited_source_marks_every_source_and_adds_guidance(self) -> None:
        audiences = {DOC_SE: {"countries": [{"code": "SE", "name": "Sweden"}], "groups": []}}
        citations, user_msg = main._format_sources(self.req, self.rows, audiences, None)
        self.assertIn("[1] (remote-se.pdf, p.1 — 3. Remote days) — applies to: Sweden\n", user_msg)
        self.assertIn("[2] (handbook.pdf, p.2 — 3. Remote days) — applies to: everyone\n", user_msg)
        self.assertIn("has not chosen a market", user_msg)
        self.assertLess(user_msg.index("has not chosen"), user_msg.index("Please answer"))
        self.assertEqual(citations[0].audience.model_dump(), {"countries": ["SE"], "groups": []})
        self.assertIsNone(citations[1].audience)

    def test_the_chosen_market_is_named(self) -> None:
        audiences = {DOC_SE: {"countries": [{"code": "SE", "name": "Sweden"}], "groups": []}}
        _, user_msg = main._format_sources(self.req, self.rows, audiences, "Sweden")
        self.assertIn("The person asking is in Sweden. Answer for Sweden.", user_msg)


class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@unittest.skipUnless(DEPS, "needs the query service's dependencies")
class MissingMigrationTests(unittest.TestCase):
    """A database without migration 05 loses the labels, never the answer."""

    def setUp(self) -> None:
        main._audience_retry_at = 0.0
        self.addCleanup(setattr, main, "_audience_retry_at", 0.0)
        self.calls = 0

    def scope_raising(self, sqlstate: str):
        test = self

        @asynccontextmanager
        async def scope():
            test.calls += 1
            raise ProgrammingError("SELECT ...", {}, _Orig(sqlstate))
            yield  # pragma: no cover

        return scope

    def run_context(self):
        rows = [row(DOC_SE, 1, "remote-se.pdf", "Three days.")]
        return asyncio.run(main._audience_context(rows, main.QueryRequest(question="q", market="SE")))

    def test_missing_tables_mean_no_labels_and_no_retry_storm(self) -> None:
        with mock.patch.object(main, "session_scope", self.scope_raising("42P01")):
            self.assertEqual(self.run_context(), ({}, None))
            self.assertEqual(self.run_context(), ({}, None))
        # The second question did not hit the database again.
        self.assertEqual(self.calls, 1)

    def test_other_database_errors_are_not_swallowed(self) -> None:
        with mock.patch.object(main, "session_scope", self.scope_raising("42601")):
            with self.assertRaises(ProgrammingError):
                self.run_context()


if __name__ == "__main__":
    unittest.main()
