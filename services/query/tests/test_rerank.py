"""Re-ranking: parsing the model's reply, merging, and failing open.

    cd services/query && python3 -m unittest discover -s tests -t .

Pure - the model client is a stand-in, so no service dependencies are needed.
"""
from __future__ import annotations

import asyncio
import unittest

from app.rerank import build_prompt, merge, parse_order, rerank


def rows(n: int) -> list[dict]:
    return [{"original_filename": f"doc{i}.pdf", "content": f"passage {i}"} for i in range(n)]


class FakeLLM:
    def __init__(self, reply: str = "", fail: Exception | None = None, delay: float = 0) -> None:
        self.reply, self.fail, self.delay, self.calls = reply, fail, delay, []

    async def complete(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, **kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return self.reply


class ParseTests(unittest.TestCase):
    def test_json_array(self) -> None:
        self.assertEqual(parse_order("[3, 1, 7]", 10), [2, 0, 6])

    def test_numbers_in_prose(self) -> None:
        self.assertEqual(parse_order("The best are 4 and then 2.", 5), [3, 1])

    def test_out_of_range_and_repeats_are_dropped(self) -> None:
        self.assertEqual(parse_order("[0, 2, 2, 99, 1]", 5), [1, 0])

    def test_junk(self) -> None:
        self.assertEqual(parse_order("", 5), [])
        self.assertEqual(parse_order("none of them", 5), [])
        self.assertEqual(parse_order('["a", null, 2]', 5), [1])


class MergeTests(unittest.TestCase):
    def test_picks_first_then_the_fused_order_fills(self) -> None:
        self.assertEqual(merge([6, 2], 10, 6), [6, 2, 0, 1, 3, 4])

    def test_never_more_than_top_k(self) -> None:
        self.assertEqual(merge(list(range(9, -1, -1)), 10, 3), [9, 8, 7])

    def test_no_picks_keeps_the_fused_order(self) -> None:
        self.assertEqual(merge([], 10, 4), [0, 1, 2, 3])


class PromptTests(unittest.TestCase):
    def test_labels_and_truncation(self) -> None:
        text = build_prompt("Which day?", [("a.pdf", "word " * 400, "Sweden"), ("b.pdf", "short", None)], 50)
        self.assertIn("[1] (a.pdf - applies to: Sweden)", text)
        self.assertIn("[2] (b.pdf)", text)
        first = text.split("[2]")[0]
        self.assertLess(len(first), 200)
        self.assertTrue(text.startswith("Question: Which day?"))


class RerankTests(unittest.TestCase):
    def run_rerank(self, llm, n=20, top_k=6, timeout=1.0):
        return asyncio.run(rerank(llm, "q", rows(n), [None] * n, top_k, timeout=timeout, max_chars=100))

    def test_reorders(self) -> None:
        got = self.run_rerank(FakeLLM("[7, 3]"))
        self.assertEqual([r["original_filename"] for r in got][:2], ["doc6.pdf", "doc2.pdf"])
        self.assertEqual(len(got), 6)

    def test_asks_deterministically_and_briefly(self) -> None:
        llm = FakeLLM("[1]")
        self.run_rerank(llm, timeout=2.5)
        self.assertEqual(llm.calls[0]["temperature"], 0)
        self.assertEqual(llm.calls[0]["timeout"], 2.5)

    def test_fails_open_on_error(self) -> None:
        got = self.run_rerank(FakeLLM(fail=RuntimeError("boom")))
        self.assertEqual([r["original_filename"] for r in got], [f"doc{i}.pdf" for i in range(6)])

    def test_fails_open_on_timeout(self) -> None:
        got = self.run_rerank(FakeLLM("[9]", delay=5), timeout=0.05)
        self.assertEqual(got[0]["original_filename"], "doc0.pdf")

    def test_nothing_to_choose_asks_nothing(self) -> None:
        llm = FakeLLM("[2]")
        got = self.run_rerank(llm, n=5, top_k=6)
        self.assertEqual(len(got), 5)
        self.assertEqual(llm.calls, [])


if __name__ == "__main__":
    unittest.main()
