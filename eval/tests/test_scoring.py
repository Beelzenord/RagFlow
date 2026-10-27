"""The evaluation's own checks: language detection, scoring and the case file.

    python3 -m unittest discover -s eval/tests -t eval
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from scoring import detect_language, says_not_covered, score, summarise  # noqa: E402

ROOT = HERE.parent


class LanguageTests(unittest.TestCase):
    SAMPLES = {
        "en": "You get paid from the first day, but a deduction applies to the first day of sickness.",
        "sv": "Du får lön från första dagen, men ett karensavdrag görs för den första sjukdagen och det är 20 procent.",
        "nb": "Du kan egenmelde deg i opptil åtte dager om gangen, og hvis du er syk lenger må du ha sykmelding.",
        "da": "Du kan kun bruge tilskuddet til motion, og hvad angår udstyr er det ikke omfattet af ordningen.",
        "fi": "Voit käyttää tukea vain liikuntaan, ja jos sinulla on kysyttävää, ota yhteyttä henkilöstöön.",
    }

    def test_each_language_is_recognised(self) -> None:
        for lang, text in self.SAMPLES.items():
            with self.subTest(lang=lang):
                self.assertEqual(detect_language(text), lang)

    def test_too_little_to_tell(self) -> None:
        self.assertIsNone(detect_language("SEK 300."))
        self.assertIsNone(detect_language(""))


class NotCoveredTests(unittest.TestCase):
    def test_ways_of_saying_it(self) -> None:
        for text in (
            "The documents don't mention a dress code.",
            "I couldn't find any information about pensions in the sources.",
            "There is no information about that in the provided sources.",
            "I looked through the indexed documents but couldn't find anything relevant to that question.",
            "Källorna nämner inte någon klädkod.",
            # Seen in the first baseline run.
            "It looks like the sources you provided don't actually include details about a dress code.",
            # A typographic apostrophe, seen later.
            "The documents focus on gifts and hospitality, but they don’t specify a dress code.",
            "I'm sorry, but the sources provided do not explain what sick pay is or how it operates.",
        ):
            with self.subTest(text=text):
                self.assertTrue(says_not_covered(text))

    def test_an_ordinary_answer_is_not_it(self) -> None:
        for text in (
            "Yes, padel is covered by the wellness allowance.",
            "The allowance does not include equipment or clothing.",
            "No, unused budget does not carry over to the next year.",
        ):
            with self.subTest(text=text):
                self.assertFalse(says_not_covered(text))


class ScoreTests(unittest.TestCase):
    CASE = {"id": "x", "question": "q", "lang": "en", "documents": ["a.pdf"],
            "include": ["SEK\\s*30"], "exclude": ["EUR\\s*0[.,]30"]}

    def test_a_good_answer_passes(self) -> None:
        r = score(self.CASE, "In Sweden the rate is SEK 30 per mil for your own car.", ["A.pdf", "b.pdf"])
        self.assertTrue(r["passed"], r)

    def test_each_failure_is_named(self) -> None:
        r = score(self.CASE, "The rate is EUR 0.30 per km for the car you own.", ["b.pdf"])
        self.assertFalse(r["passed"])
        self.assertEqual(r["checks"], {"retrieval": False, "include": False, "exclude": False,
                                       "language": True, "not_covered": None})
        self.assertEqual(len(r["reasons"]), 3)

    def test_the_wrong_language_fails(self) -> None:
        r = score(self.CASE, "Ersättningen är SEK 30 per mil och den betalas ut med lönen.", ["a.pdf"])
        self.assertFalse(r["checks"]["language"])

    def test_not_covered(self) -> None:
        case = {"id": "y", "question": "q", "not_covered": True, "exclude": ["karensavdrag"]}
        self.assertTrue(score(case, "The sources don't cover sick pay in the UK.", [])["passed"])
        self.assertFalse(score(case, "You get karensavdrag on day one.", [])["passed"])

    def test_summary_counts_only_applicable_checks(self) -> None:
        results = [score(self.CASE, "In Sweden it is SEK 30 for your own car.", ["a.pdf"]),
                   score({"id": "z", "question": "q", "not_covered": True}, "Yes it is.", [])]
        s = summarise(results)
        self.assertEqual(s["cases"], {"passed": 1, "total": 2})
        self.assertEqual(s["not_covered"], {"passed": 0, "total": 1})
        self.assertEqual(s["retrieval"], {"passed": 1, "total": 1})


class CaseFileTests(unittest.TestCase):
    """A broken case would silently pass or fail; catch it before it is asked."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = json.loads((HERE / "cases.json").read_text())["cases"]
        cls.fixtures = json.loads((HERE / "fixtures.json").read_text())

    def test_ids_are_unique(self) -> None:
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_fields(self) -> None:
        allowed = {"id", "question", "market", "lang", "documents", "include", "exclude", "not_covered"}
        for c in self.cases:
            with self.subTest(case=c["id"]):
                self.assertLessEqual(set(c), allowed)
                self.assertTrue(c["question"].strip())
                self.assertIn(c.get("lang"), (None, "en", "sv", "nb", "da", "fi"))
                if c.get("market") is not None:
                    self.assertRegex(c["market"], r"^[A-Z]{2}$")
                # Something must be checked, or the case can never fail.
                self.assertTrue(c.get("include") or c.get("exclude") or c.get("not_covered") or c.get("documents"))

    def test_patterns_compile(self) -> None:
        for c in self.cases:
            for p in (c.get("include") or []) + (c.get("exclude") or []):
                with self.subTest(case=c["id"], pattern=p):
                    re.compile(p)

    def test_expected_documents_are_test_documents(self) -> None:
        known = set(self.fixtures["documents"])
        for c in self.cases:
            for d in c.get("documents") or []:
                with self.subTest(case=c["id"], document=d):
                    self.assertIn(d, known)

    def test_test_documents_exist(self) -> None:
        folder = ROOT / self.fixtures["folder"]
        for name, spec in self.fixtures["documents"].items():
            with self.subTest(document=name):
                self.assertTrue((folder / name).exists())
                self.assertTrue(spec == "global" or all(re.fullmatch(r"[A-Z]{2}", c) for c in spec))


if __name__ == "__main__":
    unittest.main()
