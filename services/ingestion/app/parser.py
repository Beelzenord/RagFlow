"""Wraps LlamaParse and checks what it hands back.

Pipes raw bytes in, gets markdown out (with page markers when LlamaParse can
derive them - true for PDFs, not for single JPEGs). Every page is then verified
against the PDF's own text layer, because the parser can fail silently: on some
subset-font PDFs it returns confident, well-formed markdown with a third of the
characters missing from inside the words, which destroys every figure on the
page while still looking like a successful parse.

Failed pages go through a two-step repair. First they are re-parsed in premium
mode, which reads the rendered page instead of the embedded text and handles
exactly the fonts that defeat the default path. If that still fails, the raw
text layer is substituted: ugly, unstructured, and containing every number,
which beats clean markdown that lost them.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from llama_parse import LlamaParse
from rag_shared.settings import settings

from .quality import PageVerdict, assess_page
from .textlayer import extract_pages

log = logging.getLogger(__name__)


@dataclass
class ParseResult:
    markdown: str
    verdicts: list[PageVerdict] = field(default_factory=list)
    # Pages rescued by re-parsing in premium mode: still proper markdown.
    repaired_pages: list[int] = field(default_factory=list)
    # Pages replaced with the raw text layer: content intact, layout lost.
    recovered_pages: list[int] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.recovered_pages)

    def as_metadata(self) -> dict[str, Any]:
        """Compact report for documents.metadata - detail only where it matters."""
        if not self.verdicts:
            return {"parse": {"verified": False}}
        touched = set(self.repaired_pages) | set(self.recovered_pages)
        coverages = [v.coverage for v in self.verdicts if v.coverage is not None]
        return {
            "parse": {
                "verified": True,
                "pages_checked": len(self.verdicts),
                "min_coverage": round(min(coverages), 4) if coverages else None,
                "repaired_pages": self.repaired_pages,
                "recovered_pages": self.recovered_pages,
                "degraded": self.degraded,
                "details": [v.as_dict() for v in self.verdicts if v.page_number in touched],
            }
        }


def _client(**overrides: Any) -> LlamaParse:
    if not settings.llama_cloud_api_key:
        raise RuntimeError("LLAMA_CLOUD_API_KEY is not set")
    kwargs: dict[str, Any] = dict(
        api_key=settings.llama_cloud_api_key,
        result_type="markdown",
        # 'auto' lets LlamaParse pick OCR mode for image-only PDFs / JPEGs.
        parsing_instruction=(
            "Extract all text faithfully as clean Markdown. Preserve headings, "
            "tables (as Markdown tables), and lists. Do not summarize."
        ),
    )
    kwargs.update(overrides)
    return LlamaParse(**kwargs)


async def _load(parser: LlamaParse, file_path: str) -> list[str]:
    # llama_parse exposes a sync `load_data` that does its own HTTP - run it in
    # a thread so we don't block the event loop.
    docs = await asyncio.to_thread(parser.load_data, file_path)
    return [(d.text or "") for d in docs]


def _join(pages: list[str]) -> str:
    if len(pages) == 1:
        return pages[0]
    return "\n\n".join(
        f"<!-- page: {i} -->\n\n{p.strip()}" for i, p in enumerate(pages, start=1)
    )


async def _reparse_premium(file_path: str, page_numbers: list[int]) -> dict[int, str]:
    """Re-parse only the given pages with the vision/LLM path.

    Costs materially more per page than the default parse, so it is scoped to
    the pages that already failed verification.
    """
    targets = ",".join(str(n - 1) for n in page_numbers)  # LlamaParse counts from 0
    try:
        pages = await _load(
            _client(
                premium_mode=True,
                target_pages=targets,
                invalidate_cache=True,
                ignore_errors=False,
            ),
            file_path,
        )
    except Exception as exc:  # noqa: BLE001 - fall through to the text layer
        log.warning("premium re-parse failed (pages %s): %s", page_numbers, exc)
        return {}
    if len(pages) != len(page_numbers):
        log.warning(
            "premium re-parse returned %d pages for %d requested - discarding",
            len(pages),
            len(page_numbers),
        )
        return {}
    return dict(zip(page_numbers, pages))


async def parse_to_markdown(file_path: str, file_type: str) -> ParseResult:
    """Parse to Markdown, verifying and repairing per page where possible."""
    pages = await _load(_client(), file_path)
    if not pages:
        return ParseResult("")

    reference = extract_pages(file_path, file_type) if settings.parse_verify_enabled else None
    if reference is None or len(reference) != len(pages):
        if reference is not None:
            # Page counts diverge (merged pages, partial parse) so per-page
            # comparison would line up the wrong text. Skip rather than guess.
            log.info(
                "parse verification skipped: %d parsed pages vs %d PDF pages",
                len(pages),
                len(reference),
            )
        return ParseResult(_join(pages))

    verdicts = [
        assess_page(n, cand, ref)
        for n, (cand, ref) in enumerate(zip(pages, reference), start=1)
    ]
    failed = [v.page_number for v in verdicts if not v.ok]
    if not failed:
        return ParseResult(_join(pages), verdicts)

    log.warning(
        "parse verification failed: %s",
        "; ".join(f"p{v.page_number} {v.reason}" for v in verdicts if not v.ok),
    )

    if len(failed) <= settings.parse_repair_max_pages:
        reparsed = await _reparse_premium(file_path, failed)
    else:
        log.warning(
            "%d pages failed verification (limit %d) - skipping premium re-parse",
            len(failed),
            settings.parse_repair_max_pages,
        )
        reparsed = {}

    repaired: list[int] = []
    recovered: list[int] = []
    for page_number in failed:
        idx = page_number - 1
        candidate = reparsed.get(page_number)
        if candidate is not None:
            verdict = assess_page(page_number, candidate, reference[idx])
            if verdict.ok:
                pages[idx] = candidate
                verdicts[idx] = verdict
                repaired.append(page_number)
                continue
        before = verdicts[idx]
        pages[idx] = reference[idx].strip()
        verdicts[idx] = PageVerdict(
            page_number,
            True,
            "recovered from text layer",
            before.coverage,
            before.digit_coverage,
            before.mean_word_length,
            before.single_char_ratio,
        )
        recovered.append(page_number)

    log.info(
        "parse repair complete: %d page(s) re-parsed, %d page(s) recovered from text layer",
        len(repaired),
        len(recovered),
    )
    return ParseResult(_join(pages), verdicts, repaired, recovered)
