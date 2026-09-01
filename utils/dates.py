"""Deterministic deadline parsing. No LLM. UNKNOWN if it cannot be parsed."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

_UNKNOWN = {"", "unknown", "tbd", "n/a", "na", "none", "null"}

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_deadline(value) -> Optional[str]:
    """Return YYYY-MM-DD or None. Does not guess a year or timezone."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    if text.lower() in _UNKNOWN:
        return None

    # Already ISO date or datetime
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T\s].*)?$", text)
    if iso:
        try:
            datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
            return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
        except ValueError:
            return None

    # DD/MM/YYYY or DD-MM-YYYY (development-sector default: day first)
    dmy = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", text)
    if dmy:
        day, month, year = int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3))
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None

    # YYYY/MM/DD
    ymd = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", text)
    if ymd:
        year, month, day = int(ymd.group(1)), int(ymd.group(2)), int(ymd.group(3))
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None

    # 15 March 2026 / March 15, 2026 / 15th March 2026
    named = re.match(
        r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})$",
        text,
    )
    if named:
        month = _MONTHS.get(named.group(2).lower())
        if month:
            try:
                return datetime(int(named.group(3)), month, int(named.group(1))).date().isoformat()
            except ValueError:
                return None

    named_us = re.match(
        r"^([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$",
        text,
    )
    if named_us:
        month = _MONTHS.get(named_us.group(1).lower())
        if month:
            try:
                return datetime(int(named_us.group(3)), month, int(named_us.group(2))).date().isoformat()
            except ValueError:
                return None

    return None


def days_until(deadline: Optional[str], today: Optional[datetime] = None) -> Optional[int]:
    """Whole days from today (UTC date) to deadline. None if unparseable or past-unknown."""
    iso = parse_deadline(deadline)
    if not iso:
        return None
    now = today or datetime.now(timezone.utc)
    target = datetime.strptime(iso, "%Y-%m-%d").date()
    return (target - now.date()).days
