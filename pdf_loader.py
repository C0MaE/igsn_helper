"""PDF text extraction that keeps the structure the model needs."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Optional

import pdfplumber
from pdfminer.pdftypes import resolve1
from pdfminer.psparser import PSLiteral
from pdfminer.utils import decode_text

from documents import LoadedDocument, _identifiers, _norm, markdown_table

MIN_CHARS_PER_PAGE = 20

SCRIPT_SIZE_RATIO = 0.85
SCRIPT_SHIFT_RATIO = 0.15

GUTTER_ZONE = (0.35, 0.65)
MIN_GUTTER = 6.0
MIN_COLUMN_LINES = 3
MIN_COLUMN_WIDTH = 0.25
MIN_COLUMN_FILL = 0.6
MIN_WORDS_PER_LINE = 3
MAX_INNER_GAP = 3.0

UNREADABLE_RATIO = 0.3
CID_RE = re.compile(r"\(cid:\d+\)")
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})

PUSHBUTTON_FLAG = 1 << 16

logging.getLogger("pdfminer").setLevel(logging.ERROR)


@dataclass
class _Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    kind: str = "text"

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 0.1)


@dataclass
class _Line:
    words: list[_Word] = field(default_factory=list)
    block: Optional[str] = None
    block_top: float = 0.0

    @property
    def top(self) -> float:
        return self.block_top if self.block is not None else min(w.top for w in self.words)

    @property
    def bottom(self) -> float:
        return self.block_top if self.block is not None else max(w.bottom for w in self.words)

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 0.1)


def _inside(word: dict, bbox: tuple) -> bool:
    x0, top, x1, bottom = bbox
    cx, cy = (word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


def _text_words(page, exclude: list[tuple]) -> list[_Word]:
    words = []
    for w in page.extract_words(extra_attrs=["size"], keep_blank_chars=False):
        if any(_inside(w, bbox) for bbox in exclude):
            continue
        text = CID_RE.sub("", w["text"]).translate(LIGATURES)
        if text:
            words.append(_Word(text, w["x0"], w["x1"], w["top"], w["bottom"], w["size"]))
    return words


def _pdf_value(obj) -> str:
    obj = resolve1(obj)
    if obj is None:
        return ""
    if isinstance(obj, PSLiteral):
        return obj.name if isinstance(obj.name, str) else obj.name.decode("latin-1")
    if isinstance(obj, bytes):
        return decode_text(obj)
    if isinstance(obj, list):
        return ", ".join(v for v in (_pdf_value(o) for o in obj) if v)
    return str(obj)


def _form_words(page, size: float) -> list[_Word]:
    """Filled-in form fields as pseudo-words at the position of their widget."""
    words = []
    for annot in page.annots:
        data = annot.get("data") or {}
        if _pdf_value(data.get("Subtype")) != "Widget":
            continue
        parent = resolve1(data.get("Parent")) if data.get("Parent") is not None else {}

        def get(key):
            value = data.get(key)
            return value if value is not None else (parent or {}).get(key)

        field_type = _pdf_value(get("FT"))
        if field_type in ("Tx", "Ch"):
            value = " ".join(_pdf_value(get("V")).split())
        elif field_type == "Btn":
            if (resolve1(get("Ff")) or 0) & PUSHBUTTON_FLAG:
                continue
            state = _pdf_value(data.get("AS"))
            value = "[x]" if state and state != "Off" else "[ ]"
        else:
            continue
        if value:
            words.append(_Word(value, annot["x0"], annot["x1"], annot["top"], annot["bottom"], size, "field"))
    return words


def _xfa_form(pdf) -> bool:
    try:
        acro = resolve1(pdf.doc.catalog.get("AcroForm"))
        return bool(acro) and "XFA" in acro
    except Exception:
        return False


def _group_lines(words: list[_Word]) -> list[_Line]:
    """Words whose vertical extents overlap by half form a line."""
    lines: list[_Line] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        for line in reversed(lines[-6:]):
            overlap = min(line.bottom, w.bottom) - max(line.top, w.top)
            if overlap >= 0.5 * min(line.height, w.height):
                line.words.append(w)
                break
        else:
            lines.append(_Line([w]))
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
    return lines


def _mark_scripts(line: _Line) -> None:
    text_words = [w for w in line.words if w.kind == "text"]
    if len(text_words) < 2:
        return
    base_size = median(w.size for w in text_words)
    normal = [w for w in text_words if w.size >= SCRIPT_SIZE_RATIO * base_size]
    if not normal:
        return
    base_bottom = median(w.bottom for w in normal)
    shift = SCRIPT_SHIFT_RATIO * base_size
    for w in text_words:
        if w.size < SCRIPT_SIZE_RATIO * base_size:
            if w.bottom < base_bottom - shift:
                w.kind = "sup"
            elif w.bottom > base_bottom + shift:
                w.kind = "sub"


def _render(words: list[_Word]) -> str:
    out, prev = "", None
    for w in words:
        text = {"sup": f"<sup>{w.text}</sup>", "sub": f"<sub>{w.text}</sub>"}.get(w.kind, w.text)
        if prev is not None:
            gap = w.x0 - prev.x1
            if gap > 0.15 * min(prev.size, w.size) or "field" in (w.kind, prev.kind):
                out += " "
        out += text
        prev = w
    return out


def _free_intervals(line: _Line, lo: float, hi: float) -> list[tuple[float, float]]:
    """Parts of [lo, hi] not covered by any word of the line."""
    free = [(lo, hi)]
    for w in line.words:
        nxt = []
        for a, b in free:
            if w.x1 <= a or w.x0 >= b:
                nxt.append((a, b))
                continue
            if w.x0 > a:
                nxt.append((a, w.x0))
            if w.x1 < b:
                nxt.append((w.x1, b))
        free = nxt
    return free


def _intersect(a: list[tuple], b: list[tuple]) -> list[tuple]:
    out = []
    for a0, a1 in a:
        for b0, b1 in b:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo >= MIN_GUTTER:
                out.append((lo, hi))
    return out


def _prose_side(segments: list[list[_Word]], col_width: float) -> bool:
    if len(segments) < MIN_COLUMN_LINES or col_width <= 0:
        return False
    fills, counts, gappy = [], [], 0
    for ws in segments:
        fills.append((ws[-1].x1 - ws[0].x0) / col_width)
        counts.append(len(ws))
        size = median(w.size for w in ws)
        if any(b.x0 - a.x1 > MAX_INNER_GAP * size for a, b in zip(ws, ws[1:])):
            gappy += 1
    return (median(fills) >= MIN_COLUMN_FILL
            and median(counts) >= MIN_WORDS_PER_LINE
            and gappy <= len(segments) // 4)


def _split_columns(block: list[_Line], gutter: float, page_width: float):
    """Return (left, right) word lists per line if the block is two-column prose."""
    left = [[w for w in l.words if w.x1 <= gutter] for l in block]
    right = [[w for w in l.words if w.x0 >= gutter] for l in block]
    left, right = [ws for ws in left if ws], [ws for ws in right if ws]
    if not left or not right:
        return None
    left_width = gutter - min(ws[0].x0 for ws in left)
    right_width = max(ws[-1].x1 for ws in right) - gutter
    if min(left_width, right_width) < MIN_COLUMN_WIDTH * page_width:
        return None
    if not (_prose_side(left, left_width) and _prose_side(right, right_width)):
        return None
    return left, right


def _reading_order(lines: list[_Line], page_width: float) -> list[str]:
    lo, hi = GUTTER_ZONE[0] * page_width, GUTTER_ZONE[1] * page_width
    out, i = [], 0
    while i < len(lines):
        if lines[i].block is not None:
            out.append(lines[i].block)
            i += 1
            continue
        free = _intersect(_free_intervals(lines[i], lo, hi), [(lo, hi)])
        j = i
        while free and j + 1 < len(lines) and lines[j + 1].block is None:
            nxt = _intersect(free, _free_intervals(lines[j + 1], lo, hi))
            if not nxt:
                break
            free, j = nxt, j + 1
        if free and j > i:
            gap = max(free, key=lambda iv: iv[1] - iv[0])
            columns = _split_columns(lines[i:j + 1], (gap[0] + gap[1]) / 2, page_width)
            if columns:
                left, right = columns
                out += [_render(ws) for ws in left] + [""] + [_render(ws) for ws in right] + [""]
                i = j + 1
                continue
        out.append(_render(lines[i].words))
        i += 1
    return out


def _page_text(page) -> tuple[str, int, int]:
    """Returns (text, unmapped glyphs, all glyphs)."""
    chars = page.chars
    unmapped = sum(1 for c in chars if CID_RE.fullmatch(c.get("text", "")))
    size = median(c["size"] for c in chars) if chars else 10.0

    tables = page.find_tables()
    words = _text_words(page, [t.bbox for t in tables]) + _form_words(page, size)
    lines = _group_lines(words)
    for line in lines:
        _mark_scripts(line)
    for t in tables:
        rendered = markdown_table([[c or "" for c in row] for row in t.extract()])
        if rendered:
            lines.append(_Line(block=rendered, block_top=t.bbox[1]))
    lines.sort(key=lambda l: l.top)

    text = "\n".join(_reading_order(lines, page.width))
    return re.sub(r"\n{3,}", "\n\n", text).strip(), unmapped, len(chars)


def _text_under_link(page, link) -> str:
    """The text a link sits on, e.g. the author name an ORCID link belongs to."""
    try:
        box = (max(link["x0"], 0), max(link["top"], 0),
               min(link["x1"], page.width), min(link["bottom"], page.height))
        return " ".join((page.crop(box).extract_text() or "").split()).strip(" ,;.")
    except (KeyError, ValueError):
        return ""


def load_pdf(path: Path) -> LoadedDocument:
    pages, links, empty, unreadable = [], [], [], []
    reference = []
    with pdfplumber.open(str(path)) as pdf:
        xfa = _xfa_form(pdf)
        for number, page in enumerate(pdf.pages, 1):
            text, unmapped, total = _page_text(page)
            if total and unmapped / total > UNREADABLE_RATIO:
                unreadable.append(number)
            elif len(text) < MIN_CHARS_PER_PAGE:
                empty.append(number)
            pages.append(text)
            reference.append(page.extract_text() or "")
            reference += [_pdf_value((a.get("data") or {}).get("V")) for a in page.annots]
            for link in page.hyperlinks:
                if link.get("uri"):
                    links.append((link["uri"], _text_under_link(page, link)))

    text = "\n\n".join(p for p in pages if p)
    loaded = LoadedDocument(path=path, format="pdf", text=text)

    extra, seen = [], set()
    for uri, label in links:
        if uri in seen or _norm(uri) in _norm(loaded.text) or uri.startswith("mailto:"):
            continue
        seen.add(uri)
        extra.append(f"- {label} → {uri}" if label else f"- {uri}")
    if extra:
        loaded.text += "\n\n## Links in the document\n" + "\n".join(extra)

    if len(empty) + len(unreadable) == len(pages):
        loaded.fatal = True
    if unreadable:
        loaded.warnings.append(
            f"pages with unreadable text (font without character mapping), OCR is needed: {unreadable}"
        )
    if len(empty) == len(pages):
        loaded.warnings.append("PDF has no text layer (scanned?). OCR is needed before extraction.")
    elif empty:
        loaded.warnings.append(f"pages without text (scanned or images only), their content is missing: {empty}")
    if xfa:
        loaded.warnings.append("XFA form: filled-in values may be missing, please check the text")

    reference_ids = _identifiers("\n".join(reference + [u for u, _ in links]))
    loaded_text = _norm(re.sub(r"</?su[bp]>", "", loaded.text))
    missing = sorted(i for i in reference_ids if _norm(i) not in loaded_text)
    if missing:
        loaded.warnings.append(
            "identifiers in the file but not in the loaded text (content lost while loading): "
            + ", ".join(missing)
        )
    return loaded
