"""Content fingerprints, so the same file is not indexed twice.

Kept apart from main.py so the hashing can be tested without the web framework,
and so upload and backfill provably compute the same value: if they diverged, a
re-upload of a backfilled document would not be recognised.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# Large enough that hashing a 50 MB upload is a few dozen reads, small enough
# that backfill never holds a whole original in memory.
_READ_CHUNK = 1 << 20


def fingerprint(content: bytes) -> str:
    """SHA-256 of the raw bytes, as lowercase hex."""
    return hashlib.sha256(content).hexdigest()


def fingerprint_file(path: Path) -> str:
    """The same value fingerprint() gives for this file's bytes, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_READ_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()
