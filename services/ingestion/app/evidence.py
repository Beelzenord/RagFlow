"""Renders the page a citation came from, with the cited passage boxed.

The point is to let a reader check an answer without opening the PDF and
hunting: the tile under an answer is the page itself, so a claimed invoice total
is either visibly there or it is not.

Locating the passage means matching chunk text back onto the page, and the two
are not the same string. Chunks come from LlamaParse markdown - reflowed tables,
escaped asterisks, superscripts folded into odd unicode - while pdfium searches
the page's own text layer literally, including its line breaks. So instead of
one exact lookup this tries a ladder of progressively shorter phrases and
finally single distinctive tokens, and takes the first hit.

Some passages cannot be located at all, and that is expected rather than
exceptional: a table rendered as a raster image exists only in the parser's OCR
output, so nothing in the text layer will ever match it. Those pages still
render, just without a box - a correct page with no highlight is useful, a
missing tile is not. Anything worse than that (not a PDF, unreadable file,
pdfium error) returns None and the caller answers 404.
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from typing import Any, Literal

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from rag_shared.settings import settings

log = logging.getLogger(__name__)

Variant = Literal["page", "crop"]

# Marker-pen fill plus an opaque edge. The edge is what survives the browser
# scaling a full-width band down to thumbnail size.
_FILL = (255, 214, 0, 70)
_EDGE = (230, 145, 0, 255)
_EDGE_WIDTH = 3

# Phrase lengths to try, longest first: a long phrase identifies a passage
# unambiguously, but pdfium matches the text layer literally, so any phrase
# spanning a line break cannot match - hence the ladder down to three words.
_WINDOW_SIZES = (8, 6, 4, 3)
# A search is cheap but not free. Past this many misses the page's text layer
# almost certainly does not contain the passage at all.
_MAX_CANDIDATES = 40
# Shorter than this and a needle stops being distinctive enough to trust.
_MIN_NEEDLE_CHARS = 4
# Tokens worth trying alone once every phrase has failed: identifiers, amounts,
# and long words. Short words are grammar and would match anywhere.
_MIN_TOKEN_CHARS = 8

# Shape of the crop, width over height. It matches the tile the browser shows
# it in: any other ratio means the browser crops the band further, and the part
# it discards could be the highlight itself. Landing near a third of a page also
# keeps enough surrounding text for the quote to have context.
_CROP_ASPECT = 2.2
# A crop is only ever shown as a thumbnail, so full render width is wasted
# bytes. Wide enough to stay sharp on a 3x display.
_CROP_WIDTH = 600

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# Markdown furniture, including the backslashes LlamaParse uses to escape
# asterisks inside table cells. Left in, they turn a table row into "\ \ \".
_FURNITURE_RE = re.compile(r"[|#*`>_~\[\]\\]+")
_SPACE_RE = re.compile(r"[ \t]+")
_DASHES = ("\u2212", "\u2013", "\u2014", "\u2010")


def _clean(markdown: str) -> str:
    """Fold chunk markdown towards what the page's text layer actually holds."""
    s = _COMMENT_RE.sub(" ", markdown or "")
    # NFKC is what turns LlamaParse's superscripts and subscripts back into
    # plain characters, so "two-component" stops being "two₋ component".
    s = unicodedata.normalize("NFKC", s)
    for dash in _DASHES:
        s = s.replace(dash, "-")
    s = _FURNITURE_RE.sub(" ", s)
    return _SPACE_RE.sub(" ", s)


def _candidates(markdown: str) -> list[str]:
    """Needles to try, most trustworthy first."""
    lines = [ln.strip() for ln in _clean(markdown).splitlines()]
    lines = [ln for ln in lines if len(ln) > _MIN_NEEDLE_CHARS]
    if not lines:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def add(needle: str) -> None:
        needle = needle.strip(" .,;:-/")
        key = needle.casefold()
        if len(needle) < _MIN_NEEDLE_CHARS or key in seen:
            return
        seen.add(key)
        out.append(needle)

    # Longest lines first: they carry prose, while short lines are headings and
    # stray table cells that match in too many places.
    ranked = sorted(lines, key=len, reverse=True)
    for size in _WINDOW_SIZES:
        for line in ranked:
            words = line.split()
            if len(words) < size:
                continue
            # Half-window stride: enough overlap that a phrase straddling a
            # line break in the PDF is still covered by a neighbouring window.
            for start in range(0, len(words) - size + 1, max(1, size // 2)):
                add(" ".join(words[start : start + size]))

    tokens = (word for line in lines for word in line.split())
    distinctive = [
        w for w in tokens if len(w) >= _MIN_TOKEN_CHARS or any(c.isdigit() for c in w)
    ]
    for word in sorted(distinctive, key=len, reverse=True):
        add(word)

    return out[:_MAX_CANDIDATES]


def _locate(textpage: Any, needles: list[str]) -> tuple[str, int, int] | None:
    """First needle that occurs on the page, as (needle, char index, count)."""
    for needle in needles:
        searcher = textpage.search(needle, match_case=False)
        try:
            hit = searcher.get_next()
        finally:
            searcher.close()
        if hit:
            return needle, hit[0], hit[1]
    return None


def _boxes(textpage: Any, bitmap: Any, page: Any, index: int, count: int) -> list[tuple[float, ...]]:
    """Pixel boxes for a character range, one per line the match spans."""
    # pdfium builds its rectangle array as a side effect of count_rects, and
    # get_rect refuses to work until it has been primed with the defaults once.
    textpage.count_rects()
    total = textpage.count_rects(index, count)

    posconv = bitmap.get_posconv(page)
    boxes: list[tuple[float, ...]] = []
    for i in range(total):
        left, bottom, right, top = textpage.get_rect(i)
        # PDF space has its origin bottom-left, the bitmap top-left, so the
        # mapped corners come back swapped on the vertical axis.
        x0, y0 = posconv.to_bitmap(left, top)
        x1, y1 = posconv.to_bitmap(right, bottom)
        boxes.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
    return boxes


def _highlight(base: Image.Image, boxes: list[tuple[float, ...]]) -> Image.Image:
    """Composite translucent boxes over the page."""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for box in boxes:
        draw.rectangle(box, fill=_FILL, outline=_EDGE, width=_EDGE_WIDTH)
    return Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")


def _band(size: tuple[int, int], boxes: list[tuple[float, ...]]) -> tuple[int, int, int, int]:
    """Full-width horizontal band centred on the match.

    Only the vertical axis is cropped. Narrowing the width would cut words
    mid-glyph at the margin, which reads as a broken render rather than a crop.
    With nothing to centre on, the top of the page stands in: it carries the
    letterhead or title, so the tile still says which document this is.
    """
    width, height = size
    band = min(height, width / _CROP_ASPECT)
    if boxes:
        middle = (min(b[1] for b in boxes) + max(b[3] for b in boxes)) / 2
    else:
        middle = band / 2
    top = max(0.0, min(middle - band / 2, height - band))
    return 0, int(top), width, int(top + band)


def _thumbnail(image: Image.Image) -> Image.Image:
    if image.width <= _CROP_WIDTH:
        return image
    height = round(image.height * _CROP_WIDTH / image.width)
    return image.resize((_CROP_WIDTH, height), Image.LANCZOS)


def _scale_for(page: Any) -> float:
    """Render scale, held under the pixel cap so an A0 drawing can't blow up."""
    longest_point = max(page.get_size()) or 1
    return max(0.1, min(settings.evidence_scale, settings.evidence_max_dim / longest_point))


def _encode(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_evidence(
    pdf_path: str,
    page_number: int,
    chunk_content: str,
    variant: Variant = "page",
    file_type: str = "",
) -> bytes | None:
    """PNG of `page_number` with the chunk's text boxed, or None if impossible."""
    is_pdf = "pdf" in (file_type or "").lower() or pdf_path.lower().endswith(".pdf")
    if not is_pdf:
        return None

    doc = page = textpage = bitmap = None
    try:
        doc = pdfium.PdfDocument(pdf_path)
        if not 1 <= page_number <= len(doc):
            log.warning("page %s outside %s (%d pages)", page_number, pdf_path, len(doc))
            return None

        page = doc[page_number - 1]
        textpage = page.get_textpage()
        hit = _locate(textpage, _candidates(chunk_content))

        bitmap = page.render(scale=_scale_for(page))
        # convert() detaches the image from pdfium's own buffer, so the bitmap
        # can be released while the image stays valid.
        image = bitmap.to_pil().convert("RGB")
        boxes = _boxes(textpage, bitmap, page, hit[1], hit[2]) if hit else []

        if hit is None:
            # Worth knowing about in aggregate: a page that never matches is
            # usually one whose text is baked into an image.
            log.info("no text-layer match for p%d of %s", page_number, pdf_path)
        else:
            log.debug("p%d matched on %r (%d boxes)", page_number, hit[0], len(boxes))

        if boxes:
            image = _highlight(image, boxes)

        if variant == "crop":
            image = _thumbnail(image.crop(_band(image.size, boxes)))

        return _encode(image)
    except Exception as exc:  # noqa: BLE001 - a missing tile must not 500
        log.warning("evidence render failed for p%s of %s: %s", page_number, pdf_path, exc)
        return None
    finally:
        for handle in (bitmap, textpage, page, doc):
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
