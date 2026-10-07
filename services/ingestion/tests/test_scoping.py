"""Expansion of picked scope codes into the columns retrieval will filter on.

This is the logic a typo has to die in. Every case here is either "a mistake is
refused" or "an equivalent picking stores identically" - because a scope that
silently widened to global is the failure the whole mechanism exists to prevent.

The session is faked rather than hit: the rules under test are about codes,
overlap and refusal, none of which need a database to be wrong.
"""
from __future__ import annotations

import asyncio
import unittest

try:
    from app.scoping import GLOBAL_CODE, ScopeError, parse_codes, resolve_scope

    DEPS = True
except ImportError:  # pragma: no cover - depends on the environment
    DEPS = False

GROUPS = {
    "GLOBAL": {"code": "GLOBAL", "label": "Everywhere", "member_regions": [], "specificity": 0},
    "EU": {"code": "EU", "label": "European Union",
           "member_regions": ["DE", "FR", "SE", "AT"], "specificity": 1},
    "DACH": {"code": "DACH", "label": "DACH",
             "member_regions": ["DE", "AT", "CH"], "specificity": 1},
    "EMPTY": {"code": "EMPTY", "label": "Empty", "member_regions": [], "specificity": 1},
}
REGIONS = {
    "DE": {"code": "DE", "label": "Germany", "active": True},
    "CH": {"code": "CH", "label": "Switzerland", "active": True},
    "SE": {"code": "SE", "label": "Sweden", "active": True},
    # In the catalogue but not switched on - the state every country outside
    # the starter set is in until an admin enables it.
    "JP": {"code": "JP", "label": "Japan", "active": False},
    "KR": {"code": "KR", "label": "South Korea", "active": False},
}


class _Result:
    def __init__(self, rows): self._rows = rows
    def mappings(self): return self._rows


class FakeSession:
    """Answers the two vocabulary lookups resolve_scope makes."""

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        codes = (params or {}).get("codes", [])
        table = GROUPS if "region_groups" in sql else REGIONS
        return _Result([table[c] for c in codes if c in table])


def run(raw):
    return asyncio.run(resolve_scope(FakeSession(), raw))


@unittest.skipUnless(DEPS, "needs sqlalchemy")
class ParseCodesTests(unittest.TestCase):
    def test_splits_uppercases_and_trims(self) -> None:
        self.assertEqual(parse_codes(" eu , ch "), ["EU", "CH"])

    def test_drops_duplicates_but_keeps_order(self) -> None:
        """The entries a person sees back should be the ones they picked."""
        self.assertEqual(parse_codes("EU,CH,EU"), ["EU", "CH"])

    def test_empty_inputs(self) -> None:
        for raw in [None, "", "  ", ",,"]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_codes(raw), [])


@unittest.skipUnless(DEPS, "needs sqlalchemy")
class ResolveScopeTests(unittest.TestCase):
    def test_no_scope_is_global(self) -> None:
        """What smoke_test.sh and the n8n workflow send. Global, and listed as
        unscoped rather than recorded as a decision."""
        resolved = run(None)
        self.assertIsNone(resolved.regions)
        self.assertTrue(resolved.is_global)
        self.assertEqual(resolved.entries, [])

    def test_global_is_the_absence_of_a_constraint(self) -> None:
        resolved = run("GLOBAL")
        self.assertIsNone(resolved.regions)
        self.assertEqual(resolved.entries[0]["kind"], "global")
        self.assertEqual(resolved.entries[0]["specificity"], 0)

    def test_global_with_anything_else_is_refused(self) -> None:
        """Not silently collapsed to global: it is a contradiction, and the
        person meant one of the two."""
        with self.assertRaises(ScopeError):
            run(f"{GLOBAL_CODE},DE")

    def test_unknown_code_is_refused(self) -> None:
        with self.assertRaises(ScopeError) as ctx:
            run("EU,BOGUS")
        self.assertIn("BOGUS", str(ctx.exception))

    def test_group_expands_to_members(self) -> None:
        resolved = run("EU")
        self.assertEqual(resolved.regions, ["AT", "DE", "FR", "SE"])
        self.assertEqual(resolved.entries, [{"code": "EU", "kind": "group", "specificity": 1}])
        self.assertEqual(resolved.labels, ["European Union"])

    def test_overlapping_groups_deduplicate(self) -> None:
        """Germany is in both EU and DACH. It must appear once."""
        resolved = run("EU,DACH")
        self.assertEqual(resolved.regions.count("DE"), 1)
        self.assertEqual(resolved.regions, ["AT", "CH", "DE", "FR", "SE"])
        self.assertEqual([e["code"] for e in resolved.entries], ["EU", "DACH"])

    def test_leaf_region_is_the_narrowest_tier(self) -> None:
        resolved = run("DE")
        self.assertEqual(resolved.regions, ["DE"])
        self.assertEqual(resolved.entries[0], {"code": "DE", "kind": "region", "specificity": 2})

    def test_group_and_region_together_keep_their_own_specificity(self) -> None:
        """A document scoped {EU, CH} is matched by a German through EU and by a
        Swiss employee through CH - which is why specificity is per entry."""
        resolved = run("EU,CH")
        self.assertEqual([e["specificity"] for e in resolved.entries], [1, 2])
        self.assertIn("CH", resolved.regions)

    def test_an_empty_group_is_refused(self) -> None:
        """Scoping to nothing reads as "nobody can find it" rather than as an
        error, so say it is an error."""
        with self.assertRaises(ScopeError):
            run("EMPTY")

    def test_a_disabled_country_is_refused(self) -> None:
        with self.assertRaises(ScopeError) as ctx:
            run("JP")
        self.assertIn("not enabled", str(ctx.exception))

    def test_disabled_is_not_reported_as_unknown(self) -> None:
        """JP is a real country nobody switched on. Calling it unknown would send
        an admin looking for a typo instead of to the Countries page."""
        with self.assertRaises(ScopeError) as ctx:
            run("JP")
        self.assertNotIn("unknown", str(ctx.exception))
        self.assertIn("Countries page", str(ctx.exception))

    def test_several_disabled_countries_are_named_together(self) -> None:
        with self.assertRaises(ScopeError) as ctx:
            run("JP,KR")
        message = str(ctx.exception)
        self.assertIn("JP", message)
        self.assertIn("KR", message)
        self.assertIn("are not enabled", message)

    def test_a_disabled_country_poisons_an_otherwise_valid_pick(self) -> None:
        """No partial tag: if one code is refused, nothing is stored."""
        with self.assertRaises(ScopeError):
            run("EU,JP")

    def test_an_unknown_code_is_reported_before_a_disabled_one(self) -> None:
        """A typo is the more fundamental mistake, and enabling JP would not fix it."""
        with self.assertRaises(ScopeError) as ctx:
            run("JP,BOGUS")
        self.assertIn("BOGUS", str(ctx.exception))
        self.assertIn("unknown", str(ctx.exception))

    def test_equivalent_pickings_store_identically(self) -> None:
        self.assertEqual(run("EU,DACH").regions, run("DACH,EU").regions)


if __name__ == "__main__":
    unittest.main()
