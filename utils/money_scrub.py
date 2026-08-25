# utils/money_scrub.py
"""
Deterministic guarantee that no monetary figure reaches a drafted proposal.

Cortech submits the technical proposal and the financial proposal as
separate envelopes, and many procurement rules disqualify a bid outright
if price appears inside the technical envelope. Prompt instructions alone
were not enough — an earlier emailed draft shipped the sentence "The
financial proposal totals $139,090 USD" and experience tables with a
"Value" column — so every generated section is also scrubbed here before
it can reach a .docx or an email.

Scope: figures only. A number needs a currency marker next to it to be
treated as money, so sample sizes ("1,200 households"), percentages,
years, and page counts survive untouched. Amounts spelled out entirely
in words ("one hundred thousand dollars") are not detected by regex —
the prompt-level rule in intelligence/proposal_writer.py covers those.
"""
import re

# Currency codes and names. Matched case-insensitively, and always
# anchored to an adjacent number so bare words never trigger.
_CODES = (
    r"USD|US\$|EUR|GBP|KES|KSH|TZS|UGX|ETB|SOS|SLSH|SSP|ZAR|RWF|BIF|DJF|"
    r"AED|SAR|QAR|CHF|SEK|NOK|DKK|CAD|AUD|JPY|CNY|INR|XOF|XAF|NGN|GHS|"
    r"EGP|MWK|ZMW|MZN|BWP"
)
_WORDS = r"dollars?|shillings?|euros?|pounds? sterling|pounds?|birr|francs?"
_SYMBOLS = r"US\$|\$|€|£|¥|₦|₹|KSh\.?|Ksh\.?|Sh\.?"
_SCALE = r"(?:\s*(?:million|billion|trillion|thousand|mn|bn|m|k))?"
_NUM = r"\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"

# "$139,090", "USD 139,090", "KES 3.5M", "€1 200", "$139,090 USD" — the
# optional trailing code matters: without it a table cell reading
# "$139,090 USD" would be blanked down to a stray "USD".
_PREFIXED = rf"(?:{_CODES}|{_SYMBOLS})\s*(?:{_NUM}){_SCALE}(?:\s*(?:{_CODES}))?"
# "139,090 USD", "3.5 million shillings", "45,000 EUR"
_SUFFIXED = rf"(?:{_NUM}){_SCALE}\s*(?:{_CODES}|{_WORDS})\b"

_AMOUNT_RE = re.compile(rf"(?<![\w.]){_PREFIXED}|(?<![\w.]){_SUFFIXED}", re.IGNORECASE)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def find_monetary_amounts(text: str) -> list[str]:
    """Every currency figure in `text`, in order of appearance."""
    if not text:
        return []
    return [m.group(0).strip() for m in _AMOUNT_RE.finditer(text)]


def contains_monetary_amount(text: str) -> bool:
    return bool(text) and _AMOUNT_RE.search(text) is not None


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
