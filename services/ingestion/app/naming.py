"""A document's name, when an admin renames it.

Pure, so it is tested without a database. The stored original keeps its path
on disk: the name only changes what people see, what a download is saved as
and what the passages are embedded with.
"""
from __future__ import annotations

import re
import unicodedata

MAX_NAME_CHARS = 200

# Short letters-and-digits endings only: in "Minutes 3.final version" nothing
# after the dot is an extension.
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def extension_of(name: str) -> str:
    """".pdf" for "Policy.pdf"; "" for a name without one, or for ".pdf" alone."""
    m = _EXTENSION.search(name or "")
    return m.group(0) if m and m.start() > 0 else ""


def clean_filename(new: str, current: str) -> str:
    """The name to store, keeping the current extension: "Policy" and
    "Policy.PDF" both become "Policy.pdf". A different extension typed stays
    part of the name - the file is still what it was uploaded as.

    Raises ValueError, with a message for the admin, for a name that is empty,
    has a slash or invisible characters, or is too long.
    """
    name = " ".join((new or "").split())
    ext = extension_of(current)
    if ext and name.lower().endswith(ext.lower()):
        name = name[: -len(ext)].rstrip()
    if not name.strip("."):
        raise ValueError("the name is empty")
    if "/" in name or "\\" in name:
        raise ValueError("a name cannot contain / or \\")
    # Cf as well as Cc: a right-to-left override can make "fdp.exe" read as
    # "exe.pdf".
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in name):
        raise ValueError("the name contains invisible characters")
    name += ext
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"a name can be at most {MAX_NAME_CHARS} characters")
    return name
