"""Renaming a document: what name is stored.

    cd services/ingestion && python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest

from app.naming import MAX_NAME_CHARS, clean_filename, extension_of


class ExtensionTests(unittest.TestCase):
    def test_the_usual_endings(self) -> None:
        self.assertEqual(extension_of("Policy.pdf"), ".pdf")
        self.assertEqual(extension_of("scan.2024.JPEG"), ".JPEG")

    def test_no_extension(self) -> None:
        for name in ("README", ".pdf", "Minutes 3.final version", ""):
            with self.subTest(name=name):
                self.assertEqual(extension_of(name), "")


class CleanFilenameTests(unittest.TestCase):
    def test_the_extension_is_kept(self) -> None:
        self.assertEqual(clean_filename("Sick leave policy", "scan_0042.pdf"), "Sick leave policy.pdf")

    def test_typing_the_extension_does_not_double_it(self) -> None:
        self.assertEqual(clean_filename("Policy.pdf", "scan.pdf"), "Policy.pdf")
        self.assertEqual(clean_filename("Policy.PDF", "scan.pdf"), "Policy.pdf")

    def test_another_extension_stays_part_of_the_name(self) -> None:
        self.assertEqual(clean_filename("Policy.docx", "scan.pdf"), "Policy.docx.pdf")

    def test_the_current_extension_keeps_its_case(self) -> None:
        self.assertEqual(clean_filename("Photo", "IMG_0001.JPG"), "Photo.JPG")

    def test_whitespace_is_collapsed(self) -> None:
        self.assertEqual(clean_filename("  Sick   leave\tpolicy \n", "a.pdf"), "Sick leave policy.pdf")
        self.assertEqual(clean_filename("Policy .pdf", "a.pdf"), "Policy.pdf")

    def test_a_document_without_an_extension(self) -> None:
        self.assertEqual(clean_filename("Handbook", "upload"), "Handbook")

    def test_unicode_names_are_fine(self) -> None:
        self.assertEqual(clean_filename("Läkarintyg – Sverige", "x.pdf"), "Läkarintyg – Sverige.pdf")

    def test_refused(self) -> None:
        cases = {
            "": "empty",
            "   ": "empty",
            ".pdf": "empty",
            "..": "empty",
            "HR/Policy": "/",
            "HR\\Policy": "/",
            "fdp‮exe": "invisible",
            "a\x00b": "invisible",
            "x" * MAX_NAME_CHARS: "at most",
        }
        for name, reason in cases.items():
            with self.subTest(name=name[:20]):
                with self.assertRaisesRegex(ValueError, reason):
                    clean_filename(name, "scan.pdf")

    def test_the_longest_name_allowed(self) -> None:
        stem = "x" * (MAX_NAME_CHARS - 4)
        self.assertEqual(len(clean_filename(stem, "a.pdf")), MAX_NAME_CHARS)


if __name__ == "__main__":
    unittest.main()
