"""Heading-aware markdown chunker.

Splits on H1/H2/H3 boundaries, then packs sections into ~chunk_size token
windows with overlap. Page numbers come from the `<!-- page: N -->` comments
the parser emits ahead of each page's content."""
from __future__ import annotations
import bisect
import re
from dataclasses import dataclass
import tiktoken
from .settings import settings

_ENC = tiktoken.get_encoding("cl100k_base")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)
_PAGE_RE = re.compile(r"<!--\s*page:\s*(\d+)\s*-->", re.IGNORECASE)


@dataclass
class Chunk:
    index: int
    content: str
    heading: str | None
    page_number: int
    token_count: int


def _page_marks(markdown: str) -> tuple[list[int], list[int]]:
    """Character offset of each page marker, and the page it opens."""
    offsets: list[int] = []
    pages: list[int] = []
    for m in _PAGE_RE.finditer(markdown):
        offsets.append(m.start())
        pages.append(int(m.group(1)))
    return offsets, pages


def _page_at(offsets: list[int], pages: list[int], offset: int) -> int:
    """The page that the text at `offset` sits on.

    Text is attributed to where it *begins*. Using the last marker a chunk
    happens to contain instead labels anything spanning a page break with the
    page it ends on - which is how an invoice number printed at the top of
    page 1 ends up cited as page 2.

    Falling back to page 1 covers the single-page case: the parser only emits
    markers when there is more than one page to tell apart, so markdown with
    no markers at all is one page.
    """
    i = bisect.bisect_right(offsets, offset) - 1
    return pages[i] if i >= 0 else 1


def _sections(markdown: str) -> list[tuple[str | None, str, int]]:
    """Split into (heading, body, offset of body in the original markdown).

    The offset is what makes page attribution possible later, so it has to
    survive the strip() that trims the body.
    """
    out: list[tuple[str | None, str, int]] = []

    def add(heading: str | None, start: int, end: int) -> None:
        raw = markdown[start:end]
        body = raw.strip()
        if body:
            out.append((heading, body, start + len(raw) - len(raw.lstrip())))

    heading: str | None = None
    last_end = 0
    for m in _HEADING_RE.finditer(markdown):
        add(heading, last_end, m.start())
        heading = m.group(2).strip()
        last_end = m.end()
    add(heading, last_end, len(markdown))
    return out


def chunk_markdown(markdown: str) -> list[Chunk]:
    size = settings.chunk_size
    overlap = settings.chunk_overlap
    if not markdown.strip():
        return []

    offsets, pages = _page_marks(markdown)
    sections = _sections(markdown) or [
        (None, markdown.strip(), len(markdown) - len(markdown.lstrip()))
    ]

    chunks: list[Chunk] = []
    for heading, body, body_start in sections:
        tokens = _ENC.encode(body)
        if len(tokens) <= size:
            chunks.append(
                Chunk(
                    len(chunks),
                    body,
                    heading,
                    _page_at(offsets, pages, body_start),
                    len(tokens),
                )
            )
            continue

        start = 0
        # Character offset of `start` in the original markdown, carried along
        # as we go. Decoding the whole preceding prefix on every piece would
        # make chunking quadratic on long documents, and token counts can't be
        # used as a shortcut because they don't map onto character counts.
        start_char = body_start
        while start < len(tokens):
            end = min(start + size, len(tokens))
            chunks.append(
                Chunk(
                    len(chunks),
                    _ENC.decode(tokens[start:end]),
                    heading,
                    _page_at(offsets, pages, start_char),
                    end - start,
                )
            )
            if end == len(tokens):
                break
            next_start = max(0, end - overlap)
            start_char += len(_ENC.decode(tokens[start:next_start]))
            start = next_start
    return chunks
