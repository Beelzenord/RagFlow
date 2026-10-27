"""The question log: the coverage check, grouping and the Insights summary.

    cd services/query && python3 -m unittest discover -s tests -t .

Pure - answer_log.py holds no database or model calls, so nothing needs to run.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.answer_log import (
    coverage_prompt,
    group_questions,
    normalize_question,
    parse_coverage,
    sources_of,
    summarize,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def row(question: str, minutes_ago: int = 0, **fields: object) -> dict:
    return {
        "question": question,
        "created_at": NOW - timedelta(minutes=minutes_ago),
        "market": None,
        "coverage": "answered",
        "rating": None,
        "comment": None,
        "answer": f"answer to {question}",
        "lang": "en",
        "sources": [],
        **fields,
    }


class CoverageTests(unittest.TestCase):
    def test_each_word(self) -> None:
        self.assertEqual(parse_coverage("ANSWERED"), "answered")
        self.assertEqual(parse_coverage("PARTIAL"), "partial")
        self.assertEqual(parse_coverage("NOT_COVERED"), "not_covered")

    def test_case_spacing_and_punctuation_do_not_matter(self) -> None:
        self.assertEqual(parse_coverage(" not covered."), "not_covered")
        self.assertEqual(parse_coverage("Partial"), "partial")
        self.assertEqual(parse_coverage("**ANSWERED**"), "answered")

    def test_anything_else_is_left_unchecked(self) -> None:
        for reply in ("", "Yes", "MAYBE", None):
            with self.subTest(reply=reply):
                self.assertIsNone(parse_coverage(reply))

    def test_a_long_answer_is_cut_for_the_check(self) -> None:
        prompt = coverage_prompt("Why?", "x" * 10_000)
        self.assertTrue(prompt.startswith("Question: Why?"))
        self.assertLess(len(prompt), 3_100)


class SourcesTests(unittest.TestCase):
    def test_one_entry_per_document_in_citation_order(self) -> None:
        class C:
            def __init__(self, doc: str, name: str) -> None:
                self.document_id, self.filename = doc, name

        got = sources_of([C("b", "B.pdf"), C("a", "A.pdf"), C("b", "B.pdf")])
        self.assertEqual(got, [{"document_id": "b", "filename": "B.pdf"},
                               {"document_id": "a", "filename": "A.pdf"}])

    def test_plain_dicts_work_too(self) -> None:
        self.assertEqual(sources_of([{"document_id": "a", "filename": "A.pdf"}]),
                         [{"document_id": "a", "filename": "A.pdf"}])


class NormalizeTests(unittest.TestCase):
    def test_the_same_question_written_differently(self) -> None:
        self.assertEqual(normalize_question("What is the  dress code?"),
                         normalize_question("what is the dress code"))

    def test_different_questions_stay_apart(self) -> None:
        self.assertNotEqual(normalize_question("Sick leave in Sweden?"),
                            normalize_question("Sick leave in Norway?"))


class SummaryTests(unittest.TestCase):
    def test_counts(self) -> None:
        got = summarize([
            row("a"),
            row("b", coverage="not_covered", rating=-1),
            row("c", coverage=None, rating=1),
            row("d", coverage="no_hits"),
            row("e", coverage="partial"),
        ])
        self.assertEqual(got, {"total": 5, "answered": 1, "partial": 1, "not_covered": 1,
                               "no_hits": 1, "pending": 1, "up": 1, "down": 1})

    def test_nothing_asked(self) -> None:
        self.assertEqual(set(summarize([]).values()), {0})


class GroupTests(unittest.TestCase):
    def test_answered_and_liked_questions_need_no_attention(self) -> None:
        rows = [row("Vacation days?", rating=1), row("Dress code?", coverage="not_covered")]
        self.assertEqual([g["question"] for g in group_questions(rows)], ["Dress code?"])
        self.assertEqual(len(group_questions(rows, attention_only=False)), 2)

    def test_a_thumbs_down_needs_attention_even_when_answered(self) -> None:
        got = group_questions([row("Parking?", rating=-1, comment="Wrong office")])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["down"], 1)
        self.assertEqual(got[0]["comments"][0]["text"], "Wrong office")

    def test_the_same_question_is_counted_once_with_its_markets(self) -> None:
        rows = [
            row("What is the dress code?", 1, coverage="not_covered", market="SE"),
            row("what is the dress code", 5, coverage="no_hits", market="NO"),
            row("What is the dress code?", 9, coverage="not_covered"),
        ]
        [g] = group_questions(rows)
        self.assertEqual(g["count"], 3)
        self.assertEqual(g["unanswered"], 3)
        self.assertEqual(g["markets"], ["", "NO", "SE"])
        self.assertEqual(g["worst"], "no_hits")
        # The newest wording and answer represent the group.
        self.assertEqual(g["question"], "What is the dress code?")
        self.assertEqual(g["last_asked"], (NOW - timedelta(minutes=1)).isoformat())

    def test_most_in_need_first_then_most_asked_then_newest(self) -> None:
        rows = [
            row("Newest, once", 1, coverage="partial"),
            row("Twice", 2, coverage="partial"),
            row("Twice", 3, coverage="answered"),
            row("Older, once", 4, coverage="partial"),
            row("Disliked", 50, coverage="not_covered", rating=-1),
        ]
        got = [g["question"] for g in group_questions(rows)]
        self.assertEqual(got, ["Disliked", "Twice", "Newest, once", "Older, once"])

    def test_sources_are_the_most_cited_three(self) -> None:
        cited = lambda *names: [{"document_id": n, "filename": n} for n in names]  # noqa: E731
        rows = [
            row("Q", 1, coverage="partial", sources=cited("A", "B", "C", "D")),
            row("Q", 2, coverage="partial", sources=cited("B", "C")),
            row("Q", 3, coverage="partial", sources=cited("C")),
        ]
        self.assertEqual(group_questions(rows)[0]["sources"], ["C", "B", "A"])

    def test_at_most_five_comments(self) -> None:
        rows = [row("Q", i, rating=-1, comment=f"c{i}") for i in range(8)]
        self.assertEqual([c["text"] for c in group_questions(rows)[0]["comments"]],
                         ["c0", "c1", "c2", "c3", "c4"])


if __name__ == "__main__":
    unittest.main()
