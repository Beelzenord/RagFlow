"""The "Answers for" filter on retrieval.

    cd services/query && python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from app.audience import applies_to_market_sql, market_filter


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


if __name__ == "__main__":
    unittest.main()
