"""The "Answers for" filter on retrieval.

    cd services/query && python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from app.audience import (
    applies_to_market_sql,
    audience_by_document,
    citation_audience,
    market_filter,
    market_guidance,
    prompt_label,
)


class MarketFilterTests(unittest.TestCase):
    def test_no_market_searches_everything(self) -> None:
        self.assertEqual(market_filter(None, None), (None, {}))
        self.assertEqual(market_filter("", None), (None, {}))

    def test_market_keeps_global_documents_and_that_markets_own(self) -> None:
        clause, params = market_filter("SE", None)
        self.assertEqual(params, {"market": "SE"})
        # Global first: a document nobody limited stays findable from any market.
        self.assertTrue(clause.startswith("(NOT EXISTS (SELECT 1 FROM document_audience a"))
        self.assertIn(" OR EXISTS ", clause)
        self.assertIn("a.market_code = :market OR m.market_code = :market", clause)

    def test_a_named_document_overrides_the_market(self) -> None:
        self.assertEqual(market_filter("SE", "ca35dd84-0dfe-4f63-9153-e35a213ffdd6"), (None, {}))

    def test_clause_is_bracketed_so_it_ands_safely(self) -> None:
        # _build_filter joins clauses with AND; an unbracketed OR would let a
        # Global document past the status check.
        sql = applies_to_market_sql()
        self.assertTrue(sql.startswith("(") and sql.endswith(")"))
        depth = 0
        for i, ch in enumerate(sql):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(sql) - 1:
                self.fail("the outer brackets close before the end of the clause")


DOC_SE = "9b5a178a-0000-0000-0000-000000000001"
DOC_SCAN = "9b5a178a-0000-0000-0000-000000000002"

ROWS = [
    {"document_id": DOC_SE, "code": "SE", "country": "Sweden", "group_name": None, "members": None},
    # Admin-typed names reach the prompt, so stray whitespace and newlines go.
    {"document_id": DOC_SCAN, "code": None, "country": None, "group_name": " Scandi\nnavia ",
     "members": ["Sweden", "Denmark", "Norway"]},
    {"document_id": DOC_SCAN, "code": "DE", "country": "Germany", "group_name": None, "members": None},
]


class AudienceLabelTests(unittest.TestCase):
    def test_rows_are_grouped_by_document_and_sorted(self) -> None:
        by_doc = audience_by_document(ROWS)
        self.assertEqual(by_doc[DOC_SE], {"countries": [{"code": "SE", "name": "Sweden"}], "groups": []})
        scan = by_doc[DOC_SCAN]
        self.assertEqual(scan["groups"], [{"name": "Scandi navia", "members": ["Denmark", "Norway", "Sweden"]}])
        self.assertEqual(scan["countries"], [{"code": "DE", "name": "Germany"}])

    def test_prompt_label_names_group_members(self) -> None:
        by_doc = audience_by_document(ROWS)
        self.assertEqual(prompt_label(by_doc[DOC_SE]), "Sweden")
        # The model cannot know Scandinavia covers Sweden unless it is told.
        self.assertEqual(prompt_label(by_doc[DOC_SCAN]), "Scandi navia (Denmark, Norway, Sweden), Germany")

    def test_global_documents_have_no_label(self) -> None:
        self.assertIsNone(prompt_label(None))
        self.assertIsNone(prompt_label({"countries": [], "groups": []}))
        self.assertIsNone(citation_audience(None))
        self.assertIsNone(citation_audience({"countries": [], "groups": []}))

    def test_the_browser_gets_codes_and_group_names_but_never_members(self) -> None:
        # What a group is made of is admin knowledge; /api/markets hides it
        # from readers too.
        audience = citation_audience(audience_by_document(ROWS)[DOC_SCAN])
        self.assertEqual(audience, {"countries": ["DE"], "groups": ["Scandi navia"]})
        self.assertNotIn("Denmark", str(audience))

    def test_order_is_stable_whatever_order_the_rows_arrive_in(self) -> None:
        self.assertEqual(audience_by_document(ROWS), audience_by_document(list(reversed(ROWS))))


class MarketGuidanceTests(unittest.TestCase):
    def test_market_specific_beats_everyone(self) -> None:
        text = market_guidance(None)
        self.assertIn("market-specific source applies in that market", text)
        self.assertIn("Never present its rule as the rule for everyone", text)

    def test_without_a_chosen_market_each_market_is_named(self) -> None:
        text = market_guidance(None)
        self.assertIn("has not chosen a market", text)
        self.assertIn("In Sweden ..., in Norway ...", text)

    def test_it_keeps_the_answer_in_the_question_language(self) -> None:
        # Without this, the English block turned a Swedish question's answer
        # English.
        for chosen in (None, "Sweden"):
            self.assertIn("same language as the question", market_guidance(chosen))

    def test_it_forbids_inventing_rules(self) -> None:
        # Seen in testing: without these, the model announced "a general
        # guideline" no source gave, and guessed at a market it had no source for.
        text = market_guidance(None)
        self.assertIn("only if a source for everyone actually gives one", text)
        self.assertIn("Do not guess what applies anywhere else", text)

    def test_with_a_chosen_market_the_answer_is_for_it(self) -> None:
        text = market_guidance("Sweden")
        self.assertIn("The person asking works in Sweden: answer with Sweden's rules.", text)
        self.assertNotIn("has not chosen", text)


if __name__ == "__main__":
    unittest.main()
