"""Answers stay in the question's language.

    cd services/query && python3 -m unittest discover -s tests -t .

Pure - no service dependencies needed.
"""
from __future__ import annotations

import unittest

from app.language import answer_language_rule, detect_language


class DetectTests(unittest.TestCase):
    QUESTIONS = {
        "en": ["Do I get paid on my first sick day?", "What is the sick pay?",
               "Can I use the wellness allowance for padel?", "How many days a week can I work remotely?"],
        "sv": ["Får jag lön första sjukdagen?", "Hur många dagar i veckan får jag jobba hemifrån?",
               "Kan jag använda friskvårdsbidraget till padel?"],
        "nb": ["Hva skjer hvis jeg blir syk i ferien?", "Hvordan melder jeg fra om sykdom til deg?"],
        "da": ["Hvad gør jeg, hvis jeg bliver syg?", "Hvordan melder jeg mig syg, og hvad får jeg?"],
        "fi": ["Saanko palkkaa ensimmäiseltä sairauspäivältä?", "Paljonko on matkakorvaus ja miten se maksetaan?"],
    }

    def test_questions_in_each_language(self) -> None:
        for lang, questions in self.QUESTIONS.items():
            for q in questions:
                with self.subTest(lang=lang, question=q):
                    self.assertEqual(detect_language(q), lang)

    def test_too_short_to_tell(self) -> None:
        self.assertIsNone(detect_language("Padel?"))
        self.assertIsNone(detect_language(""))

    def test_scandinavian_i_is_not_english(self) -> None:
        # "i" means "in" in Swedish, Norwegian and Danish.
        self.assertEqual(detect_language("Hur många dagar i veckan får jag jobba hemifrån?"), "sv")


class RuleTests(unittest.TestCase):
    def test_a_detected_language_is_named(self) -> None:
        rule = answer_language_rule("Do I get paid on my first sick day?")
        self.assertTrue(rule.startswith("Answer in English, the language of the question"))
        self.assertIn("not the language of the sources", rule)

    def test_voice_knows_what_it_heard(self) -> None:
        self.assertEqual(answer_language_rule("Padel?", "sv"), "Answer in Swedish.")

    def test_unknown_language_is_described(self) -> None:
        rule = answer_language_rule("Padel?")
        self.assertTrue(rule.startswith("Answer in the language the question is written in"))


if __name__ == "__main__":
    unittest.main()
