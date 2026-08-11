"""Local extraction of a PDF's embedded text layer.

This is an oracle for the cloud parser's output, not a replacement for it: it
knows nothing about reading order, columns, or tables, but it sees every
character the page actually contains. That makes it useless for producing
markdown and ideal for answering "did the parser lose anything?".

Returns None whenever it can't speak - a non-PDF, an unreadable file, a missing
dependency - so callers treat verification as optional and never fail an ingest
just because the check was unavailable.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def extract_pages(file_path: str, file_type: str = "") -> list[str] | None:
    """Plain text per page, index 0 = page 1. None when unavailable."""
    is_pdf = "pdf" in (file_type or "").lower() or file_path.lower().endswith(".pdf")
    if not is_pdf:
        return None

    try:
        import pypdfium2 as pdfium
    except ImportError:
        log.warning("pypdfium2 not installed - parse verification unavailable")
        return None

    doc = None
    try:
        doc = pdfium.PdfDocument(file_path)
        pages: list[str] = []
        for i in range(len(doc)):
            page = doc[i]
            textpage = page.get_textpage()
            try:
                pages.append((textpage.get_text_bounded() or "").replace("\r\n", "\n"))
            finally:
                textpage.close()
                page.close()
        return pages
    except Exception as exc:  # noqa: BLE001 - never break ingest over the oracle
        log.warning("text-layer extraction failed for %s: %s", file_path, exc)
        return None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass
