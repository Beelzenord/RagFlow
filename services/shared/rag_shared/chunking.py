"""Heading-aware markdown chunker.

Splits on H1/H2/H3 boundaries. A section that fits in chunk_size tokens is one
chunk. A longer one is packed paragraph by paragraph - blocks separated by a
blank line - so a sentence, a list or an FAQ question and its answer is never
cut in half; consecutive chunks overlap by whole trailing paragraphs, up to
chunk_overlap tokens. Only a single paragraph longer than chunk_size is cut
into token windows. Page numbers come from the `<!-- page: N -->` comments the
parser emits ahead of each page's content."""
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


_BLOCK_SPLIT = re.compile(r"\n[ \t]*\n")


def _blocks(body: str, body_start: int) -> list[tuple[str, int]]:
    """(paragraph, offset in the original markdown), blank-line separated."""
    out: list[tuple[str, int]] = []
    pos = 0
    for m in _BLOCK_SPLIT.finditer(body):
        _add_block(out, body, pos, m.start(), body_start)
        pos = m.end()
    _add_block(out, body, pos, len(body), body_start)
    return out


def _add_block(out: list[tuple[str, int]], body: str, start: int, end: int, body_start: int) -> None:
    raw = body[start:end]
    text = raw.strip()
    if text:
        out.append((text, body_start + start + len(raw) - len(raw.lstrip())))


def _windows(
    text: str, start_char: int, size: int, overlap: int
) -> list[tuple[str, int, int]]:
    """A single paragraph longer than `size`, cut into overlapping token
    windows: (content, offset of its start, token count)."""
    tokens = _ENC.encode(text)
    out: list[tuple[str, int, int]] = []
    start = 0
    # Character offset of `start`, carried along as we go. Decoding the whole
    # preceding prefix on every piece would make chunking quadratic on long
    # documents, and token counts can't be used as a shortcut because they
    # don't map onto character counts.
    while start < len(tokens):
        end = min(start + size, len(tokens))
        out.append((_ENC.decode(tokens[start:end]), start_char, end - start))
        if end == len(tokens):
            break
        next_start = max(start + 1, end - overlap)
        start_char += len(_ENC.decode(tokens[start:next_start]))
        start = next_start
    return out


def _pack(
    blocks: list[tuple[str, int]], size: int, overlap: int
) -> list[tuple[str, int, int]]:
    """Whole paragraphs packed up to `size` tokens: (content, offset, tokens).
    Each chunk after the first repeats the previous one's trailing paragraphs,
    as many as fit in `overlap` tokens, so context carries across the cut."""
    counts = [len(_ENC.encode(t)) for t, _ in blocks]
    out: list[tuple[str, int, int]] = []
    fresh = 0  # the first paragraph no chunk has included yet
    i = 0  # where the next chunk starts: `fresh`, or earlier when overlapping
    while fresh < len(blocks):
        if counts[fresh] > size:
            out.extend(_windows(blocks[fresh][0], blocks[fresh][1], size, overlap))
            fresh = i = fresh + 1
            continue
        start, total = i, 0
        # +2 for the blank line joining two paragraphs.
        while i < len(blocks) and counts[i] <= size and total + counts[i] + (2 if i > start else 0) <= size:
            total += counts[i] + (2 if i > start else 0)
            i += 1
        if i <= fresh:
            # The repeated paragraphs left no room for a new one: start the
            # chunk at the new paragraph instead, without overlap.
            i = fresh
            continue
        text = "\n\n".join(t for t, _ in blocks[start:i])
        out.append((text, blocks[start][1], len(_ENC.encode(text))))
        fresh = i
        # Step back over the trailing paragraphs that fit in the overlap.
        back, carried = i, 0
        while back - 1 > start and carried + counts[back - 1] <= overlap:
            back -= 1
            carried += counts[back]
        i = back
    return out


def chunk_markdown(markdown: str, size: int | None = None, overlap: int | None = None) -> list[Chunk]:
    size = size or settings.chunk_size
    overlap = settings.chunk_overlap if overlap is None else overlap
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

        for content, offset, count in _pack(_blocks(body, body_start), size, overlap):
            chunks.append(Chunk(len(chunks), content, heading, _page_at(offsets, pages, offset), count))
    return chunks
