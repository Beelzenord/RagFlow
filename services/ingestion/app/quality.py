"""Checks parser output against the PDF's own text layer.

Two failures matter here, and they need different tests:

* **Content loss** - the parser returned a stub, a summary, or nothing at all
  for a page that demonstrably had text. Character coverage catches this.
* **Garbled text** - the parser returned plausible-looking output with
  characters dropped *inside* the words, which is what a subset font with a
  broken encoding produces. Coverage barely notices (every character that
  survived is still "correct"), but every figure on the page is destroyed.
  Word integrity catches this.

Coverage is deliberately order-insensitive and one-directional. A good parse
reorders text - columns, tables, floating sidebars - and legitimately drops
things the text layer contains: running heads, print artefacts, embedded
machine metadata. Output is never penalised for containing *more* than the text
layer either, because pages whose text is baked into images produce exactly
that. So the thresholds are set to catch collapse, not tidying.

Thresholds were calibrated against the live corpus (92 pages). Garbled pages
scored a 1.76 mean word length and 58-61% single-character words; the worst
clean page scored 4.15 and 21%. Coverage alone cannot separate the two - a
clean page scored 57.8% while a garbled one scored 43.4% - so it only serves as
a backstop for wholesale loss.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from rag_shared.settings import settings

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Below this much text in the layer we can't conclude anything: the page is a
# scan, a full-page image, or blank, and the parser's OCR is the only witness.
_MIN_REFERENCE_CHARS = 200
# Word integrity needs a reasonable sample before its ratios mean anything.
_MIN_WORDS_FOR_INTEGRITY = 20


@dataclass(frozen=True)
class PageVerdict:
    page_number: int
    ok: bool
    reason: str
    coverage: float | None = None
    digit_coverage: float | None = None
    mean_word_length: float | None = None
    single_char_ratio: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _normalize(text: str) -> str:
    """Fold to comparable characters.

    NFKC does the heavy lifting: it turns the superscripts LlamaParse emits for
    reference markers into plain digits, and non-breaking spaces into spaces,
    so formatting choices don't read as missing content.
    """
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _coverage(candidate: str, reference: str, digits_only: bool = False) -> float | None:
    """Share of the reference's characters present in the candidate.

    Multiset comparison, so it ignores order entirely and still accounts for
    repeats. Extra content in the candidate cannot lower the score.
    """
    cand, ref = Counter(candidate), Counter(reference)
    if digits_only:
        cand = Counter({k: v for k, v in cand.items() if k.isdigit()})
        ref = Counter({k: v for k, v in ref.items() if k.isdigit()})
    total = sum(ref.values())
    if not total:
        return None
    return sum(min(count, cand.get(ch, 0)) for ch, count in ref.items()) / total


def _word_stats(text: str) -> tuple[float, float, int] | None:
    """(mean word length, single-character word ratio, word count)."""
    words = _WORD_RE.findall(unicodedata.normalize("NFKC", text or ""))
    if len(words) < _MIN_WORDS_FOR_INTEGRITY:
        return None
    singles = sum(1 for w in words if len(w) == 1)
    return sum(len(w) for w in words) / len(words), singles / len(words), len(words)


def assess_page(page_number: int, candidate: str, reference: str) -> PageVerdict:
    """Judge one page of parser output against the same page's text layer."""
    norm_ref = _normalize(reference)
    if len(norm_ref) < _MIN_REFERENCE_CHARS:
        return PageVerdict(page_number, True, "unverifiable: no usable text layer")

    norm_cand = _normalize(candidate)
    coverage = _coverage(norm_cand, norm_ref)
    digit_coverage = _coverage(norm_cand, norm_ref, digits_only=True)

    if coverage is not None and coverage < settings.parse_min_coverage:
        return PageVerdict(
            page_number, False, "content loss", coverage, digit_coverage
        )

    stats = _word_stats(candidate)
    if stats is None:
        # Passed coverage but too short to judge word-by-word; nothing more to test.
        return PageVerdict(page_number, True, "ok", coverage, digit_coverage)

    mean_len, single_ratio, _ = stats
    garbled = (
        mean_len < settings.parse_min_word_length
        or single_ratio > settings.parse_max_single_char_ratio
    )
    return PageVerdict(
        page_number,
        not garbled,
        "garbled text" if garbled else "ok",
        coverage,
        digit_coverage,
        mean_len,
        single_ratio,
    )
