"""Upload and backfill must compute the same fingerprint for the same bytes.

If they diverged, a document hashed by the backfill would not be recognised when
someone uploaded it again, and the duplicate check would pass a file it exists
to stop. Standard library only.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.fingerprint import _READ_CHUNK, fingerprint, fingerprint_file


class FingerprintTests(unittest.TestCase):
    def test_is_sha256_hex(self) -> None:
        self.assertEqual(
            fingerprint(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_different_bytes_differ(self) -> None:
        self.assertNotEqual(fingerprint(b"invoice v1"), fingerprint(b"invoice v2"))

    def test_upload_and_backfill_agree(self) -> None:
        content = b"%PDF-1.4 the same file, arriving two different ways"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "original.pdf"
            path.write_bytes(content)
            self.assertEqual(fingerprint_file(path), fingerprint(content))

    def test_backfill_agrees_across_read_chunks(self) -> None:
        """A file larger than one read must not hash differently from its bytes -
        the case a chunked reader gets wrong if it drops or repeats a block."""
        content = (b"0123456789abcdef" * (_READ_CHUNK // 16)) + b"tail past one chunk"
        self.assertGreater(len(content), _READ_CHUNK)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.pdf"
            path.write_bytes(content)
            self.assertEqual(fingerprint_file(path), hashlib.sha256(content).hexdigest())

    def test_filename_does_not_matter(self) -> None:
        """Same bytes under two names is the duplicate this exists to catch."""
        content = b"2026-09-02-ellevio faktura"
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "faktura.pdf"
            b = Path(tmp) / "faktura (1).pdf"
            a.write_bytes(content)
            b.write_bytes(content)
            self.assertEqual(fingerprint_file(a), fingerprint_file(b))


if __name__ == "__main__":
    unittest.main()
