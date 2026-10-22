"""A tiny PDF writer for test policies: no dependencies, A4, Helvetica.

Supports a title, a meta line, a banner, headings, paragraphs, bullets, two- or
three-column tables and a Q&A block, with wrapping and page breaks, and a
footer with page numbers. Text is WinAnsi (cp1252), which covers Swedish,
Norwegian, Danish and Finnish letters.
"""
from __future__ import annotations

import unicodedata
from pathlib import Path

W, H = 595.28, 841.89
ML, MR, MT, MB = 58, 58, 62, 70
TW = W - ML - MR

_HELV = dict(zip(
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~",
    [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278]
    + [556] * 10
    + [278, 278, 584, 584, 584, 556, 1015,
       667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611,
       722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333,
       556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278,
       556, 500, 722, 500, 500, 500, 334, 260, 334, 584],
))
_BOLD = dict(zip(
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~",
    [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278]
    + [556] * 10
    + [333, 333, 584, 584, 584, 611, 975,
       722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611,
       722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333,
       556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333,
       611, 556, 778, 556, 556, 500, 389, 280, 389, 584],
))
_SPECIAL = {"ø": 611, "Ø": 778, "æ": 889, "Æ": 1000, "ß": 611, "€": 556, "–": 556, "—": 1000,
            "’": 222, "‘": 222, "“": 333, "”": 333, "•": 350, "·": 278, "×": 584}


def width(text: str, size: float, bold: bool = False) -> float:
    table = _BOLD if bold else _HELV
    total = 0
    for ch in text:
        if ch in table:
            total += table[ch]
        elif ch in _SPECIAL:
            total += _SPECIAL[ch]
        else:
            base = unicodedata.normalize("NFD", ch)[0]
            total += table.get(base, 556)
    return total * size / 1000


def wrap(text: str, size: float, maxw: float, bold: bool = False) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}" if line else word
            if width(trial, size, bold) <= maxw:
                line = trial
                continue
            if line:
                lines.append(line)
            # A single word wider than the line is split by character.
            while width(word, size, bold) > maxw:
                cut = len(word)
                while cut > 1 and width(word[:cut], size, bold) > maxw:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def _pdf_string(text: str) -> bytes:
    raw = text.encode("cp1252")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


class Doc:
    def __init__(self, title: str, footer: str, subject: str = "") -> None:
        self.title = title
        self.footer = footer
        self.subject = subject
        self.pages: list[list[bytes]] = []
        self.y = 0.0
        self.pending: str | None = None
        self.new_page()

    # --- drawing primitives -------------------------------------------------
    def new_page(self) -> None:
        self.pages.append([])
        self.y = H - MT

    def ops(self, op: str | bytes) -> None:
        self.pages[-1].append(op.encode() if isinstance(op, str) else op)

    def text(self, x: float, y: float, s: str, size: float, bold: bool = False, rgb=(0.12, 0.13, 0.15)) -> None:
        font = "F2" if bold else "F1"
        self.ops(f"BT /{font} {size:.2f} Tf {rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg {x:.2f} {y:.2f} Td ".encode()
                 + _pdf_string(s) + b" Tj ET")

    def rect(self, x: float, y: float, w: float, h: float, fill=None, stroke=None, lw: float = 0.6) -> None:
        parts = []
        if fill:
            parts.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg")
        if stroke:
            parts.append(f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG {lw:.2f} w")
        op = "B" if fill and stroke else "f" if fill else "S"
        parts.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re {op}")
        self.ops(" ".join(parts))

    def ensure(self, needed: float) -> None:
        if self.y - needed < MB:
            self.new_page()

    # --- blocks --------------------------------------------------------------
    def heading_title(self, s: str) -> None:
        for line in wrap(s, 20, TW, True):
            self.ensure(26)
            self.y -= 22
            self.text(ML, self.y, line, 20, True)
        self.y -= 8

    def meta(self, s: str) -> None:
        for line in wrap(s, 8.6, TW, True):
            self.ensure(12)
            self.y -= 11.5
            self.text(ML, self.y, line, 8.6, True, (0.36, 0.38, 0.42))
        self.y -= 10

    def banner(self, head: str, body: str) -> None:
        lines = wrap(body, 9, TW - 20)
        h = 16 + 12 * (len(lines) + 1)
        self.ensure(h + 6)
        top = self.y
        self.rect(ML, top - h, TW, h, fill=(1.0, 0.965, 0.84), stroke=(0.86, 0.70, 0.25))
        y = top - 16
        self.text(ML + 10, y, head, 9, True, (0.45, 0.32, 0.0))
        for line in lines:
            y -= 12
            self.text(ML + 10, y, line, 9, False, (0.30, 0.24, 0.05))
        self.y = top - h - 14

    def h(self, s: str) -> None:
        # Drawn by the next block, which knows how much of itself must stay on
        # the same page - so a heading never ends a page alone.
        self.pending = s

    def _heading(self, keep: float) -> None:
        if self.pending is None:
            return
        lines = wrap(self.pending, 12.5, TW, True)
        self.pending = None
        self.ensure(8 + 16 * len(lines) + 4 + keep)
        self.y -= 8
        for line in lines:
            self.y -= 16
            self.text(ML, self.y, line, 12.5, True, (0.07, 0.20, 0.42))
        self.y -= 4

    def p(self, s: str, size: float = 10.4, lead: float = 14.4) -> None:
        self._heading(lead * min(2, len(wrap(s, size, TW))))
        for line in wrap(s, size, TW):
            self.ensure(lead)
            self.y -= lead
            self.text(ML, self.y, line, size)
        self.y -= 6

    def bullets(self, items: list[str], size: float = 10.4, lead: float = 14.4) -> None:
        self._heading(lead * 2)
        for item in items:
            lines = wrap(item, size, TW - 16)
            for i, line in enumerate(lines):
                self.ensure(lead)
                self.y -= lead
                if i == 0:
                    self.text(ML + 3, self.y, "•", size, True, (0.07, 0.20, 0.42))
                self.text(ML + 16, self.y, line, size)
            self.y -= 2
        self.y -= 4

    def table(self, header: list[str], rows: list[list[str]], ratios: list[float] | None = None) -> None:
        n = len(header)
        ratios = ratios or [1 / n] * n
        widths = [TW * r for r in ratios]
        size, lead, pad = 9.4, 12.4, 5

        def draw_row(cells: list[str], bold: bool, fill) -> None:
            wrapped = [wrap(c, size, w - 2 * pad, bold) for c, w in zip(cells, widths)]
            rh = max(len(w) for w in wrapped) * lead + 2 * pad - 2
            if self.y - rh < MB:
                self.new_page()
                draw_row(header, True, (0.91, 0.93, 0.96))
            top = self.y
            x = ML
            for cell_lines, w in zip(wrapped, widths):
                self.rect(x, top - rh, w, rh, fill=fill, stroke=(0.78, 0.80, 0.84), lw=0.5)
                y = top - pad - 8
                for line in cell_lines:
                    self.text(x + pad, y, line, size, bold)
                    y -= lead
                x += w
            self.y = top - rh

        # The heading, the column titles and at least the first two rows together.
        first = rows[:2]
        need = sum(max(len(wrap(c, size, w - 2 * pad)) for c, w in zip(r, widths)) * lead + 2 * pad for r in [header, *first])
        self._heading(need)
        self.ensure(need)
        draw_row(header, True, (0.91, 0.93, 0.96))
        for row in rows:
            draw_row(row, False, None)
        self.y -= 14

    def qa(self, pairs: list[tuple[str, str]], q_prefix: str = "Q:", a_prefix: str = "A:") -> None:
        if pairs:
            q0, a0 = pairs[0]
            self._heading(14.4 * (len(wrap(f"{q_prefix} {q0}", 10.4, TW, True)) + len(wrap(f"{a_prefix} {a0}", 10.4, TW))))
        for q, a in pairs:
            q_lines = wrap(f"{q_prefix} {q}", 10.4, TW, True)
            a_lines = wrap(f"{a_prefix} {a}", 10.4, TW)
            self.ensure(14.4 * (len(q_lines) + min(2, len(a_lines))) + 4)
            for line in q_lines:
                self.y -= 14.4
                self.text(ML, self.y, line, 10.4, True)
            for line in a_lines:
                self.ensure(14.4)
                self.y -= 14.4
                self.text(ML, self.y, line, 10.4)
            self.y -= 8

    # --- output --------------------------------------------------------------
    def save(self, path: Path, page_label: str = "Page {n} of {total}") -> None:
        self._heading(0)
        total = len(self.pages)
        for i, page in enumerate(self.pages, 1):
            self.pages[i - 1] = page
            label = page_label.format(n=i, total=total)
            footer_ops = []
            footer_ops.append(f"0.82 0.84 0.87 RG 0.5 w {ML:.2f} {MB - 22:.2f} m {W - MR:.2f} {MB - 22:.2f} l S".encode())
            font_rgb = "0.420 0.440 0.480 rg"
            footer_ops.append(f"BT /F1 7.8 Tf {font_rgb} {ML:.2f} {MB - 34:.2f} Td ".encode() + _pdf_string(self.footer) + b" Tj ET")
            lw = width(label, 7.8)
            footer_ops.append(f"BT /F1 7.8 Tf {font_rgb} {W - MR - lw:.2f} {MB - 34:.2f} Td ".encode() + _pdf_string(label) + b" Tj ET")
            page.extend(footer_ops)

        objects: list[bytes] = []

        def add(obj: bytes) -> int:
            objects.append(obj)
            return len(objects)

        catalog = add(b"")  # filled below
        pages_obj = add(b"")
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        info = add(b"<< /Title " + _pdf_string(self.title) + b" /Subject " + _pdf_string(self.subject)
                   + b" /Producer (Rahmqvist Assistant test content) >>")
        kids = []
        for page in self.pages:
            stream = b"\n".join(page)
            content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
            page_obj = add(
                f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {W:.2f} {H:.2f}] "
                f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> /Contents {content} 0 R >>".encode()
            )
            kids.append(page_obj)
        objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
        objects[pages_obj - 1] = (
            f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
        )

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, obj in enumerate(objects, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        path.write_bytes(bytes(out))
