"""Heading-aware markdown chunker.

Splits on H1/H2/H3 boundaries, then packs sections into ~chunk_size token windows
with overlap. Tries to preserve heading + page-number hints (LlamaParse emits
`<!-- page: N -->` comments which we detect)."""
from __future__ import annotations
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
    page_number: int | None
    token_count: int


def _tokens(text: str) -> int:
    return len(_ENC.encode(text))


def chunk_markdown(markdown: str) -> list[Chunk]:
    size = settings.chunk_size
    overlap = settings.chunk_overlap
    if not markdown.strip():
        return []

    # Split into (heading, body) sections.
    sections: list[tuple[str | None, str]] = []
    last_end = 0
    current_heading: str | None = None
    for m in _HEADING_RE.finditer(markdown):
        body = markdown[last_end:m.start()].strip()
        if body:
            sections.append((current_heading, body))
        current_heading = m.group(2).strip()
        last_end = m.end()
    tail = markdown[last_end:].strip()
    if tail:
        sections.append((current_heading, tail))
    if not sections:
        sections = [(None, markdown.strip())]

    chunks: list[Chunk] = []
    idx = 0
    current_page: int | None = None
    for heading, body in sections:
        # Track most-recent page marker seen so far.
        for pm in _PAGE_RE.finditer(body):
            try:
                current_page = int(pm.group(1))
            except ValueError:
                pass

        tokens = _ENC.encode(body)
        if len(tokens) <= size:
            chunks.append(Chunk(idx, body, heading, current_page, len(tokens)))
            idx += 1
            continue

        start = 0
        while start < len(tokens):
            end = min(start + size, len(tokens))
            piece = _ENC.decode(tokens[start:end])
            piece_page = current_page
            for pm in _PAGE_RE.finditer(piece):
                try:
                    piece_page = int(pm.group(1))
                except ValueError:
                    pass
            chunks.append(Chunk(idx, piece, heading, piece_page, end - start))
            idx += 1
            if end == len(tokens):
                break
            start = max(0, end - overlap)
    return chunks
