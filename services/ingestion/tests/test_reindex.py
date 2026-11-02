"""Re-index all: what the Dashboard is told, and that a run always ends.

    cd services/ingestion && python3 -m unittest discover -s tests -t .

The database and the embedder are stand-ins, so no service is needed.
"""
from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import mock

from app import pipeline, reindex
from app.reindex import ReindexRun

try:
    from fastapi.testclient import TestClient

    HTTP_DEPS = True
except ImportError:  # pragma: no cover - depends on the environment
    HTTP_DEPS = False


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(reindex, "_current", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_yet_reads_as_not_running(self) -> None:
        self.assertEqual(reindex.status(), {"running": False, "total": 0})

    def test_progress_counts_every_outcome(self) -> None:
        run = reindex.start(5)
        run.rebuilt = 2
        run.skipped = 1
        run.failed.append("scan.pdf")
        status = reindex.status()
        self.assertTrue(status["running"])
        self.assertEqual((status["done"], status["total"]), (4, 5))
        self.assertEqual(status["failed"], ["scan.pdf"])
        self.assertIsNone(status["finished_at"])

    def test_a_finished_run_says_when(self) -> None:
        reindex.start(1).finish()
        status = reindex.status()
        self.assertFalse(status["running"])
        self.assertIsNotNone(status["finished_at"])

    def test_one_run_at_a_time(self) -> None:
        first = reindex.start(3)
        self.assertIsNone(reindex.start(3))
        first.finish()
        self.assertIsNotNone(reindex.start(3))


class _Embedder:
    async def aclose(self) -> None:
        pass


def rows(documents: dict[str, dict | None]):
    """A stand-in session_scope whose one SELECT answers from `documents`."""

    class Session:
        async def execute(self, _statement, params):
            row = documents.get(params["id"])
            found = SimpleNamespace(first=lambda: row)
            return SimpleNamespace(mappings=lambda: found)

    @asynccontextmanager
    async def scope():
        yield Session()

    return scope


async def _vectors(_embedder, chunks, _name):
    return [[0.0] for _ in chunks]


class RechunkTests(unittest.TestCase):
    documents = {
        "ok": {"original_filename": "ok.pdf", "markdown_text": "# Text\n\nBody"},
        "bad": {"original_filename": "bad.pdf", "markdown_text": "# Text\n\nBody"},
        "empty": {"original_filename": "empty.pdf", "markdown_text": ""},
    }

    def rebuild(self, ids: list[str], *, embedder=_Embedder, replace=None) -> tuple[ReindexRun, mock.AsyncMock]:
        run = ReindexRun(total=len(ids))
        record = mock.AsyncMock()

        async def current_name(_session, document_id, lock=False):
            return self.documents[document_id]["original_filename"]

        with (
            mock.patch.object(pipeline, "session_scope", rows(self.documents)),
            mock.patch.object(pipeline, "EmbeddingClient", embedder),
            mock.patch.object(pipeline, "_embed_chunks", _vectors),
            mock.patch.object(pipeline, "_current_filename", current_name),
            mock.patch.object(pipeline, "_replace_chunks", replace or mock.AsyncMock()),
            mock.patch.object(pipeline, "_record_rebuild", record),
        ):
            asyncio.run(pipeline.run_rechunk(ids, run))
        return run, record

    def test_a_rebuild_is_counted_and_recorded(self) -> None:
        run, record = self.rebuild(["ok"])
        self.assertEqual(run.rebuilt, 1)
        self.assertEqual(record.await_args.args[1:], ("ok", "completed"))
        self.assertIsNotNone(run.finished_at)

    def test_nothing_to_rebuild_from_is_skipped_not_failed(self) -> None:
        run, record = self.rebuild(["empty"])
        self.assertEqual((run.rebuilt, run.skipped, run.failed), (0, 1, []))
        record.assert_not_awaited()

    def test_one_failure_names_the_document_and_the_rest_go_on(self) -> None:
        async def replace(_session, document_id, _chunks, _embeddings):
            if document_id == "bad":
                raise RuntimeError("boom")

        run, record = self.rebuild(["bad", "ok"], replace=replace)
        self.assertEqual(run.failed, ["bad.pdf"])
        self.assertEqual(run.rebuilt, 1)
        self.assertEqual(record.await_args_list[0].args[1:], ("bad", "failed", "boom"))

    def test_a_run_ends_even_when_nothing_can_be_done(self) -> None:
        # No embedder: left "running", the Dashboard would wait for ever, and
        # no further run could start.
        run, _ = self.rebuild(["ok", "bad"], embedder=mock.Mock(side_effect=RuntimeError("no key")))
        self.assertEqual(run.failed, ["ok", "bad"])
        self.assertEqual(run.handled, 2)
        self.assertIsNotNone(run.finished_at)


@unittest.skipUnless(HTTP_DEPS, "needs fastapi")
class RouteTests(unittest.TestCase):
    def test_the_status_is_not_taken_for_a_document_id(self) -> None:
        from app import main

        resp = TestClient(main.app).get("/documents/rechunk-status")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("running", resp.json())


if __name__ == "__main__":
    unittest.main()
