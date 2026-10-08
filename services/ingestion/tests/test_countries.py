"""The ISO catalogue admins pick markets from.

    cd services/ingestion && python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from app.audience import MARKET_CODE
from app.countries import ISO_COUNTRIES


class CatalogueTests(unittest.TestCase):
    def test_every_iso_country_plus_kosovo(self) -> None:
        # 249 official ISO 3166-1 alpha-2 codes, plus the user-assigned XK.
        self.assertEqual(len(ISO_COUNTRIES), 250)
        self.assertEqual(ISO_COUNTRIES["XK"], "Kosovo")

    def test_codes_are_the_shape_the_database_accepts(self) -> None:
        # markets.code has CHECK (code ~ '^[A-Z]{2}$'); a catalogue entry that
        # failed it would be offered in the picker and then refused.
        for code in ISO_COUNTRIES:
            self.assertRegex(code, MARKET_CODE)

    def test_names_are_present_and_readable(self) -> None:
        for code, name in ISO_COUNTRIES.items():
            self.assertTrue(name.strip(), code)
        self.assertEqual(ISO_COUNTRIES["SE"], "Sweden")
        # Common names rather than the formal ISO ones.
        self.assertEqual(ISO_COUNTRIES["KR"], "South Korea")
        # Apostrophes and accents survive the round trip from the source data.
        self.assertEqual(ISO_COUNTRIES["CI"], "Côte d'Ivoire")
        self.assertEqual(ISO_COUNTRIES["AX"], "Åland Islands")


if __name__ == "__main__":
    unittest.main()
