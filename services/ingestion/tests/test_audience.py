"""Who a document is for: the rules the API applies before anything is stored.

    cd services/ingestion && python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from app.audience import (
    DEFAULT_SORT,
    SORTS,
    Audience,
    AudienceError,
    applies_to_market_sql,
    is_global_sql,
    normalize_code,
    targets_market_sql,
    validate_audience,
    who_filter,
)

GROUP = "2beda27b-7389-496e-94e2-3f0e62268cef"


class ValidateAudienceTests(unittest.TestCase):
    def test_everyone_stores_no_rows(self) -> None:
        self.assertEqual(validate_audience(True, [], []), Audience())
        self.assertTrue(validate_audience(True, [], []).everyone)

    def test_everyone_wins_over_leftover_ticks(self) -> None:
        # A form that kept its checkboxes when the admin switched back to
        # Everyone still means Everyone.
        self.assertEqual(validate_audience(True, ["SE"], [GROUP]), Audience())

    def test_empty_restricted_selection_is_refused(self) -> None:
        # Stored as no rows it would read back as Global - the opposite of what
        # was asked for.
        with self.assertRaisesRegex(AudienceError, "Choose Everyone"):
            validate_audience(False, [], [])
        with self.assertRaises(AudienceError):
            validate_audience(False, ["  "], [])

    def test_codes_are_upper_cased_and_deduped_in_order(self) -> None:
        audience = validate_audience(False, ["se", "DK", "SE", " no "], [])
        self.assertEqual(audience.countries, ("SE", "DK", "NO"))
        self.assertFalse(audience.everyone)

    def test_groups_are_normalised_and_deduped(self) -> None:
        audience = validate_audience(False, [], [GROUP.upper(), GROUP])
        self.assertEqual(audience.groups, (GROUP,))

    def test_bad_values_are_named(self) -> None:
        with self.assertRaisesRegex(AudienceError, "SWE"):
            validate_audience(False, ["SWE"], [])
        with self.assertRaisesRegex(AudienceError, "not a group id"):
            validate_audience(False, [], ["scandinavia"])

    def test_normalize_code(self) -> None:
        self.assertEqual(normalize_code(" se "), "SE")
        for bad in ("", "S", "S1", "SWE", "ÅÅ"):
            with self.assertRaises(AudienceError, msg=bad):
                normalize_code(bad)


class SqlTests(unittest.TestCase):
    def test_targets_market_resolves_groups_at_read_time(self) -> None:
        sql = targets_market_sql(param="who")
        self.assertIn("market_group_members", sql)
        self.assertIn("a.market_code = :who", sql)
        self.assertIn("m.market_code = :who", sql)
        self.assertIn("d.id", sql)

    def test_global_means_no_audience_rows(self) -> None:
        self.assertEqual(
            is_global_sql("x"),
            "NOT EXISTS (SELECT 1 FROM document_audience a WHERE a.document_id = x.id)",
        )


    def test_applies_to_market_is_everyone_or_limited_to_it(self) -> None:
        # What answers for a market draw on - the same test retrieval uses.
        self.assertEqual(
            applies_to_market_sql(param="who"),
            f"({is_global_sql()} OR {targets_market_sql(param='who')})",
        )


class WhoFilterTests(unittest.TestCase):
    def test_no_filter(self) -> None:
        self.assertEqual(who_filter(None), (None, {}))
        self.assertEqual(who_filter(""), (None, {}))

    def test_global(self) -> None:
        self.assertEqual(who_filter("global"), (is_global_sql(), {}))

    def test_country_is_upper_cased(self) -> None:
        self.assertEqual(who_filter("se"), (targets_market_sql(param="who"), {"who": "SE"}))

    def test_country_with_global_is_what_its_answers_draw_on(self) -> None:
        self.assertEqual(
            who_filter("SE", include_global=True),
            (applies_to_market_sql(param="who"), {"who": "SE"}),
        )

    def test_group(self) -> None:
        sql, params = who_filter(GROUP.upper())
        self.assertIn("a.group_id = :who_group", sql)
        self.assertEqual(params, {"who_group": GROUP})

    def test_include_global_needs_a_country(self) -> None:
        for who in (None, "global", GROUP):
            with self.subTest(who=who), self.assertRaises(AudienceError):
                who_filter(who, include_global=True)

    def test_garbage_is_refused(self) -> None:
        for who in ("Sweden", "S", "1; DROP TABLE documents"):
            with self.subTest(who=who), self.assertRaises(AudienceError):
                who_filter(who)


class SortTests(unittest.TestCase):
    def test_every_order_breaks_ties_on_the_id(self) -> None:
        # Otherwise documents with the same name or time could repeat or go
        # missing between pages.
        for name, order in SORTS.items():
            with self.subTest(name):
                self.assertTrue(order.endswith(", d.id"), order)

    def test_default_is_newest_first(self) -> None:
        self.assertEqual(SORTS[DEFAULT_SORT], "d.created_at DESC, d.id")

    def test_names_are_plain_words(self) -> None:
        # They become a regex alternation in the route's validation.
        for name in SORTS:
            self.assertRegex(name, r"^[a-z_]+$")


if __name__ == "__main__":
    unittest.main()
