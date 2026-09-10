# utils/money_scrub.py
"""
Deterministic guard that no identifiable monetary amount reaches a drafted proposal.

Cortech submits the technical proposal and the financial proposal as
separate envelopes, and many procurement rules disqualify a bid outright
if price appears inside the technical envelope. Prompt instructions alone
were not enough — an earlier emailed draft shipped the sentence "The
financial proposal totals $139,090 USD" and experience tables with a
"Value" column — so every generated section is also scrubbed here before
it can reach a .docx or an email.

Amounts need a currency marker next to them to be treated as money, so sample
sizes ("1,200 households"), percentages, years, and page counts survive
untouched. Both numeric and bounded natural-language number phrases are
supported; a phrase such as "one hundred participants" remains untouched
because it has no currency marker.

Writer *input* (the ToR pack, donor notes, style guides) is redacted in place
so the model never sees a figure it could copy. Writer *output* drops the
whole sentence, because deleting the figure alone leaves broken prose.
"""
import re

_REDACTION = "[REDACTED: financial proposal only]"

# Currency codes and names. Matched case-insensitively, and always
# anchored to an adjacent number so bare words never trigger.
_CODES = (
    r"USD|US\$|EUR|GBP|KES|KSH|TZS|UGX|ETB|SOS|SLSH|SSP|ZAR|RWF|BIF|DJF|"
    r"AED|SAR|QAR|CHF|SEK|NOK|DKK|CAD|AUD|JPY|CNY|INR|XOF|XAF|NGN|GHS|"
    r"EGP|MWK|ZMW|MZN|BWP"
)
_WORDS = (
    r"(?:US\s+)?dollars?|(?:Kenyan|Kenya|Tanzanian|Ugandan|Somali|Somalia)\s+shillings?|"
    r"shillings?|euros?|pounds? sterling|pounds?|birr|francs?"
)
_SYMBOLS = r"US\$|\$|€|£|¥|₦|₹|KSh\.?|Ksh\.?|Sh\.?"
_SCALE = r"(?:\s*(?:million|billion|trillion|thousand|mn|bn|m|k))?"
_NUM = r"\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"

# The longest accepted phrase is deliberately bounded. It recognizes ordinary
# English tender/proposal amounts without treating arbitrary prose containing a
# number word as a monetary expression.
_NUMBER_WORD = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|thousand|million|billion|trillion"
)
_WORD_NUMBER = rf"(?:{_NUMBER_WORD})(?:[\s-]+(?:and[\s-]+)?(?:{_NUMBER_WORD})){{0,11}}"

# "$139,090", "USD 139,090", "KES 3.5M", "€1 200", "$139,090 USD" — the
# optional trailing code matters: without it a table cell reading
# "$139,090 USD" would be blanked down to a stray "USD".
_PREFIXED = rf"(?:{_CODES}|{_SYMBOLS})\s*(?:{_NUM}){_SCALE}(?:\s*(?:{_CODES}))?"
# "139,090 USD", "3.5 million shillings", "45,000 EUR"
_SUFFIXED = rf"(?:{_NUM}){_SCALE}\s*(?:{_CODES}|{_WORDS})\b"
_WORD_PREFIXED = rf"(?:{_CODES}|{_SYMBOLS})\s*(?:{_WORD_NUMBER})\b"
_WORD_SUFFIXED = rf"(?:{_WORD_NUMBER})\s*(?:{_CODES}|{_WORDS})\b"

_AMOUNT_RE = re.compile(
    rf"(?<![\w.])(?:{_PREFIXED}|{_SUFFIXED}|{_WORD_PREFIXED}|{_WORD_SUFFIXED})",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Experience-table columns that would disclose price even if the cells are empty.
_FINANCIAL_HEADER_CELL = re.compile(
    r"^(?:est\.?\s+|estimated\s+|total\s+|contract\s+)*"
    r"(?:value|budget|fee|fees|price|prices|rate|rates|amount|amounts|"
    r"lump[\s-]?sum|cost|usd|eur|gbp|kes)"
    r"(?:\s*\([^)]+\))?$",
    re.IGNORECASE,
)


def find_monetary_amounts(text: str) -> list[str]:
    """Every currency figure in `text`, in order of appearance."""
    if not text:
        return []
    return [m.group(0).strip() for m in _AMOUNT_RE.finditer(text)]


def contains_monetary_amount(text: str) -> bool:
    return bool(text) and _AMOUNT_RE.search(text) is not None


def redact_monetary_amounts(text: str) -> tuple[str, list[str]]:
    """Replace currency figures with a marker; keep the surrounding sentence.

    Used on writer *input* (tender pack, extra context) so the model can see
    that a ceiling existed without being able to quote it.
    """
    removed = find_monetary_amounts(text)
    if not removed:
        return text, []
    cleaned = _AMOUNT_RE.sub(_REDACTION, text)
    cleaned = re.sub(r"[^\S\n]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, removed


def _is_separator_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and set(s.replace(" ", "")) <= set("|:-")


def _header_cell_is_financial(cell: str) -> bool:
    text = re.sub(r"[*_`]", "", cell or "")
    text = re.sub(r"\s+", " ", text).strip()
    return bool(text) and bool(_FINANCIAL_HEADER_CELL.match(text))


def find_financial_table_headers(text: str) -> list[str]:
    """Markdown table header cells that are themselves a price/budget column."""
    if not text:
        return []
    found: list[str] = []
    lines = (text or "").split("\n")
    for i, line in enumerate(lines):
        if not _is_table_row(line):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        prev = lines[i - 1] if i > 0 else ""
        if not (_is_separator_row(nxt) or not _is_table_row(prev)):
            continue
        for cell in line.strip().strip("|").split("|"):
            if _header_cell_is_financial(cell):
                found.append(cell.strip())
    return found


def strip_financial_table_headers(text: str) -> tuple[str, list[str]]:
    """Rewrite price/budget/fee header cells to duration-or-scope."""
    removed = find_financial_table_headers(text)
    if not removed:
        return text, []
    lines = (text or "").split("\n")
    out = []
    for i, line in enumerate(lines):
        if not _is_table_row(line):
            out.append(line)
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        prev = lines[i - 1] if i > 0 else ""
        if not (_is_separator_row(nxt) or not _is_table_row(prev)):
            out.append(line)
            continue
        leading = line[: len(line) - len(line.lstrip())]
        body = line.strip()
        outer_pipes = body.endswith("|")
        cells = body.strip("|").split("|")
        rewritten = []
        for cell in cells:
            if _header_cell_is_financial(cell):
                rewritten.append(" Duration or scope ")
            else:
                rewritten.append(cell)
        rebuilt = "|" + "|".join(rewritten) + ("|" if outer_pipes else "")
        out.append(leading + rebuilt)
    return "\n".join(out), removed


def contains_financial_disclosure(text: str) -> bool:
    """True when a client-facing draft still carries price or a price column."""
    return contains_monetary_amount(text) or bool(find_financial_table_headers(text))


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _tidy(fragment: str) -> str:
    """Clean up the punctuation and spacing a removed amount leaves behind."""
    fragment = re.sub(r"\(\s*[,;:]?\s*\)", "", fragment)
    fragment = re.sub(r"\s+([,.;:%)])", r"\1", fragment)
    fragment = re.sub(r"([,;:])\s*([,.;:])", r"\2", fragment)
    fragment = re.sub(r"\s{2,}", " ", fragment)
    return fragment.strip()


def _scrub_table_row(line: str) -> str:
    """
    Blank only the offending cells so the row — and the column count the
    .docx renderer depends on — stays intact.
    """
    leading = line[: len(line) - len(line.lstrip())]
    body = line.strip()
    outer_pipes = body.endswith("|")
    cells = body.strip("|").split("|")
    cleaned = []
    for cell in cells:
        if contains_monetary_amount(cell):
            stripped = _tidy(_AMOUNT_RE.sub("", cell))
            cleaned.append(f" {stripped} " if stripped else " — ")
        else:
            cleaned.append(cell)
    rebuilt = "|" + "|".join(cleaned) + ("|" if outer_pipes else "")
    return leading + rebuilt


def strip_monetary_amounts(text: str) -> tuple[str, list[str]]:
    """
    Remove every currency figure from `text`.

    Returns (cleaned_text, amounts_removed). Table rows keep their shape
    with the offending cell blanked; in prose, the whole sentence carrying
    the figure is dropped, because deleting the figure alone leaves broken
    sentences like "The financial proposal totals , structured against".
    """
    removed = find_monetary_amounts(text)
    if not removed:
        return text, []

    out_lines = []
    for line in (text or "").split("\n"):
        if not contains_monetary_amount(line):
            out_lines.append(line)
            continue
        if _is_table_row(line):
            out_lines.append(_scrub_table_row(line))
            continue
        kept = [
            s for s in _SENTENCE_SPLIT_RE.split(line.strip())
            if s.strip() and not contains_monetary_amount(s)
        ]
        if kept:
            leading = line[: len(line) - len(line.lstrip())]
            out_lines.append(leading + " ".join(kept))
        # Every sentence on the line carried a figure — drop the line.

    cleaned = "\n".join(out_lines)
    # Collapse any blank-line runs the dropped lines opened up.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, removed
