"""
Shared readers for the past-proposal corpus in data/proposals/.

Both extract_style_guide.py and extract_voice_exemplars.py used to read
only .docx files, so every PDF-only submission never reached a style guide
or a voice exemplar. This module gives both scripts one PDF path (the same
pdfplumber call shape as intelligence/relationships.py) and one notion of
a document's real date.

PDF headings are a HEURISTIC. A .docx carries explicit "Heading N" styles;
a PDF carries only positioned text. The detector below reuses the heading
regex already used to pack tender documents (intelligence.tender_reader)
plus conservative line-shape rules. Treat PDF-derived headings as
approximate, not as ground truth.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from loguru import logger

from intelligence.tender_reader import _HEADING_RE
from utils.dates import _MONTHS, parse_deadline

# YYYYMMDD_ prefix used by most dated files in data/proposals/.
_FILENAME_DATE_RE = re.compile(r"^(20\d{2})(\d{2})(\d{2})_")

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
# Cover-page date forms, checked in order. Day-month-year and month-day-year
# are exact; month-year resolves to the 1st and is flagged as such.
_COVER_DATE_RES: tuple[tuple[str, re.Pattern], ...] = (
    ("dmy", re.compile(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_ALT + r")[,.]?\s+(20\d{2})\b", re.I
    )),
    ("mdy", re.compile(
        r"\b(" + _MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", re.I
    )),
    ("numeric", re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})\b")),
    ("month_year", re.compile(r"\b(" + _MONTH_ALT + r")[,.]?\s+(20\d{2})\b", re.I)),
)

# How much of the document to search for a cover/letter date. Two pages of
# a PDF or the first paragraphs of a .docx comfortably cover a cover page
# and a transmittal letter without reaching work-plan dates deeper in.
COVER_CHARS = 6000
MAX_DATE_LINE_CHARS = 60  # longer lines are prose, not a cover/letter date line
PDF_COVER_PAGES = 2
DOCX_COVER_PARAGRAPHS = 80

# Heading shape limits for PDF text lines.
_MAX_HEADING_WORDS = 14
_MAX_HEADING_CHARS = 110


# DDMMYYYY glued into a filename body, e.g. "..._Final_14122024.pdf".
_FILENAME_DDMMYYYY_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")


def parse_filename_date(name: str) -> date | None:
    """
    Date from the filename: the YYYYMMDD_ prefix first, else a written
    date anywhere in the stem ("Technical Proposal 31 Jan 2026.docx",
    "..._Final_14122024.pdf"). None when the name carries no date.
    """
    m = _FILENAME_DATE_RE.match(name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    stem = Path(name).stem
    found = _date_in_line(stem)  # a filename is one line; length limit does not apply
    if found:
        return found
    m = _FILENAME_DDMMYYYY_RE.search(stem)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def extract_cover_date(text: str) -> date | None:
    """
    First plausible date in cover-page text. Exact when a day is present;
    month-year forms resolve to the 1st of that month. None when nothing
    date-like appears — never invents a date.
    """
    head = (text or "")[:COVER_CHARS]
    # Cover-page and letter dates sit on a short line of their own
    # ("07 November 2024", "Nairobi – 8 April 2026"). A date inside a long
    # prose line ("the programme started implementation in September
    # 2020") is body text and must not be taken as the document date.
    for raw in head.splitlines():
        # Right-aligned letter dates arrive as "COVER LETTER          10th
        # November 2025": collapse internal runs of whitespace before judging.
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or len(line) > MAX_DATE_LINE_CHARS:
            continue
        found = _date_in_line(line)
        if found:
            return found
    return None


def _date_in_line(line: str) -> date | None:
    for kind, pattern in _COVER_DATE_RES:
        m = pattern.search(line)
        if not m:
            continue
        if kind == "dmy":
            iso = parse_deadline(f"{m.group(1)} {m.group(2)} {m.group(3)}")
        elif kind == "mdy":
            iso = parse_deadline(f"{m.group(1)} {m.group(2)} {m.group(3)}")
        elif kind == "numeric":
            iso = parse_deadline(f"{m.group(1)}/{m.group(2)}/{m.group(3)}")
        else:
            month = _MONTHS.get(m.group(1).lower())
            iso = f"{m.group(2)}-{month:02d}-01" if month else None
        if iso:
            return date.fromisoformat(iso)
    return None


def read_pdf_pages(path: Path, max_pages: int | None = None) -> list[str]:
    """
    Text per page via pdfplumber — same call shape as
    intelligence.relationships.read_proposal_file. Pages with no text layer
    yield "". Raises on an unreadable file so callers can log and skip.
    """
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        selected = pdf.pages if max_pages is None else pdf.pages[:max_pages]
        for page in selected:
            pages.append(page.extract_text() or "")
    return pages


_NUMBERED_HEADING_RE = re.compile(r"^\d+(\.\d+){0,3}\.?\s+[A-Z]")
_LABELLED_HEADING_RE = re.compile(r"^(ANNEX|ARTICLE|SECTION|PART|CHAPTER)\s+[A-Z0-9IVX]+\b", re.I)
_MAX_TOKEN_CHARS = 28  # longer single tokens are glued words from designed PDFs


def is_pdf_heading(line: str) -> bool:
    """
    Conservative heading test for one PDF text line: short, on its own
    line, not ending in sentence punctuation, and shaped like a heading.

    The tender-reader regex (intelligence.tender_reader._HEADING_RE) is
    compiled case-insensitively, which is right for packing ToR text but
    lets any punctuation-free lowercase fragment through here, so it is
    used only as a gate. A line must ALSO be one of: numbered ("2.1 Title"),
    labelled (ANNEX/ARTICLE/SECTION/PART/CHAPTER), ALL CAPS, or Title Case
    with at least two words. Heuristic — see module docstring.
    """
    t = line.strip()
    if not t or len(t) > _MAX_HEADING_CHARS or len(t.split()) > _MAX_HEADING_WORDS:
        return False
    if t[-1] in ".,;:":
        return False
    if re.search(r"\.{3,}|\t", t):  # table-of-contents leader
        return False
    if sum(c.isdigit() for c in t) / len(t) > 0.35:  # table row / page no.
        return False
    if any(len(tok) > _MAX_TOKEN_CHARS for tok in t.split()):
        return False
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 4:
        return False
    if _LABELLED_HEADING_RE.match(t) or _NUMBERED_HEADING_RE.match(t):
        return True
    if not _HEADING_RE.match(t):
        return False
    upper_ratio = sum(c.isupper() for c in letters) / len(letters)
    if upper_ratio >= 0.85:
        return True  # ALL CAPS heading
    words = [w for w in re.split(r"\s+", t) if any(c.isalpha() for c in w)]
    if len(words) < 2:
        return False
    significant = [w for w in words if len(w) > 3]
    if not significant:
        return False
    capitalised = sum(w[0].isupper() for w in significant) / len(significant)
    return capitalised >= 0.7 and words[0][0].isupper()


def pdf_blocks(pages: list[str]) -> list[tuple[str, str]]:
    """
    Rebuild ("heading", text) / ("para", text) blocks from PDF page text.
    pdfplumber returns one line per visual line with no paragraph markers,
    so consecutive non-heading lines are joined until a blank line, a
    heading, or a short line ending a sentence.
    """
    blocks: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            blocks.append(("para", " ".join(buf).strip()))
            buf.clear()

    for page in pages:
        for raw in page.splitlines():
            line = raw.strip()
            if not line:
                flush()
                continue
            if is_pdf_heading(line):
                flush()
                blocks.append(("heading", line))
                continue
            buf.append(line)
            if line[-1] in ".?!" and len(line) < 55:
                flush()
        flush()
    return blocks


def pdf_text_has_layer(pages: list[str]) -> bool:
    """False for scanned PDFs with no extractable text."""
    return sum(len(p.strip()) for p in pages) >= 200


def docx_cover_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs[:DOCX_COVER_PARAGRAPHS]]
    for table in doc.tables[:3]:
        for row in table.rows[:10]:
            parts.extend(c.text for c in row.cells)
    return "\n".join(parts)


def rank_by_date(files: list[Path]) -> tuple[list[tuple[Path, date, str]], list[Path]]:
    """
    Files ordered newest first by REAL date (filename prefix, else cover
    page). Files with no discoverable date are returned separately so the
    caller can log why they were left out of a "most recent N" sample,
    rather than being silently mis-ranked. mtime is never consulted.
    """
    dated: list[tuple[Path, date, str]] = []
    undated: list[Path] = []
    for path in files:
        d, source = document_date(path)
        if d is None:
            undated.append(path)
        else:
            dated.append((path, d, source))
    dated.sort(key=lambda item: (item[1], item[0].name.lower()), reverse=True)
    return dated, undated


def document_date(path: Path) -> tuple[date | None, str]:
    """
    (real date, source) for a corpus file. Source is "filename", "cover",
    or "none". Never falls back to filesystem mtime — after a clone or
    archive extraction mtime is checkout time, not submission date.
    """
    d = parse_filename_date(path.name)
    if d:
        return d, "filename"
    try:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            text = "\n".join(read_pdf_pages(path, max_pages=PDF_COVER_PAGES))
        elif suffix == ".docx":
            text = docx_cover_text(path)
        else:
            return None, "none"
    except Exception as e:  # unreadable file — caller decides what to do
        logger.warning(f"Could not read {path.name} for a cover date: {e}")
        return None, "none"
    d = extract_cover_date(text)
    return (d, "cover") if d else (None, "none")
