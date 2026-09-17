"""Label-to-document grounding for the golden set.

Recorded analyzer JSON that copies labels can still report P/R 1.000 even if
the reconstructed ToR never stated those facts. These helpers require labeled
values to appear in ``document_text`` (or stay null because they are not there).
"""

from __future__ import annotations

import re
from typing import Any

VACANCY_SIGNALS = (
    "vacancy",
    "job opening",
    "job posting",
    "staff",
    "salary",
    "salaried",
    "full time",
    "recruitment",
    "position",
    "employee",
    "employment",
    "intern",
    "internship",
    "hiring",
    "trainee",
    "we are hiring",
    "is recruiting",
    "is seeking a",
)

THEME_ALIASES = {
    "endline": ("endline", "end line"),
    "capacity building": ("capacity building",),
    "livelihoods": ("livelihoods", "livelihood"),
    "mel": ("mel", "meal", "m&e", "monitoring evaluation and learning"),
    "economic inclusion": ("economic inclusion",),
    "extractive mining geology": ("extractive mining geology", "mining geology"),
    "assessment": ("assessment",),
}


def normalize_text(value: str) -> str:
    text = (value or "").casefold().replace("—", "-").replace("–", "-")
    text = text.replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def theme_appears(theme: str, document_normalized: str) -> bool:
    key = normalize_text(theme)
    aliases = THEME_ALIASES.get(key, (key,))
    return any(alias in document_normalized for alias in aliases)


def budget_appears(budget: Any, document_text: str) -> bool:
    number = float(budget)
    as_int = str(int(number)) if number.is_integer() else str(budget)
    collapsed = document_text.replace(",", "")
    return as_int in collapsed or as_int in document_text


def ungrounded_fields(item: dict) -> list[str]:
    """Return human-readable issues. Empty list means the item is grounded."""
    text = item.get("document_text") or ""
    normalized = normalize_text(text)
    labels = item.get("labels") or {}
    issues: list[str] = []

    client = labels.get("client")
    if client:
        if normalize_text(str(client)) not in normalized:
            issues.append(f"client {client!r} is not in document_text")
    deadline = labels.get("deadline")
    if deadline and str(deadline) not in text:
        issues.append(f"deadline {deadline!r} is not in document_text")
    budget = labels.get("budget_usd")
    if budget is not None and not budget_appears(budget, text):
        issues.append(f"budget {budget!r} is not in document_text")
    for geo in labels.get("geography") or []:
        if normalize_text(str(geo)) not in normalized:
            issues.append(f"geography {geo!r} is not in document_text")
    for theme in labels.get("thematic_areas") or []:
        if not theme_appears(str(theme), normalized):
            issues.append(f"thematic {theme!r} is not in document_text")

    if labels.get("is_consultancy_contract") is False:
        if not any(signal in normalized for signal in VACANCY_SIGNALS):
            issues.append("vacancy item has no HR/staff language in document_text")

    return issues
