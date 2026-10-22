"""A procurement-style reading of a draft against stored evaluation criteria.

Marks come from the traced alignment already computed for the 80% gate.
Addressed counts the full weight, partially addressed counts half, and
not addressed counts zero. Nothing here is a mark awarded by the buyer.
"""
from __future__ import annotations

from intelligence.requirement_alignment import EVALUATION_ALIGNMENT_BAR

WARNING = (
    "This is the agent's traced reading of the draft against the stored "
    "criteria. It is not a score awarded by the buyer."
)

_FRACTION = {
    "addressed": 1.0,
    "partially_addressed": 0.5,
    "partial": 0.5,
    "not_addressed": 0.0,
    "missing": 0.0,
}


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def reading_from_alignment(report: dict | None) -> dict:
    """Turn an alignment report into marks, reasons, and how to reach the bar."""
    report = report or {}
    rows_in = [row for row in (report.get("rows") or []) if isinstance(row, dict)]
    criteria = [row for row in rows_in if row.get("kind") == "evaluation_criterion"]
    scored = []
    earned = 0.0
    weight_total = 0.0
    for row in criteria:
        weight = _num(row.get("weight_percent"))
        status = str(row.get("status") or "not_addressed")
        fraction = _FRACTION.get(status, 0.0)
        label = str(row.get("label") or row.get("text") or "Criterion")
        if weight is None:
            mark = None
            reason = "This criterion has no weight in the extracted ToR, so it is not given a mark."
            improvement = "Add the weight from the ToR before treating this line as scored."
        else:
            mark = round(weight * fraction, 2)
            earned += mark
            weight_total += weight
            if fraction == 1.0:
                reason = str(row.get("reason") or row.get("evidence") or "The criterion's own words are traced in the draft.")
                improvement = ""
            elif fraction == 0.5:
                reason = str(row.get("reason") or "The criterion is named, but not answered in a full paragraph.")
                improvement = (
                    f"Answer {label} in the cited section with its own words, "
                    f"in a paragraph long enough to be a response. "
                    f"The pass mark is {EVALUATION_ALIGNMENT_BAR}% of weighted criteria."
                )
            else:
                reason = str(row.get("reason") or "This criterion is not traced in the draft.")
                improvement = (
                    f"Write a section that answers {label} in the buyer's words. "
                    f"Until it is traced, it scores 0 of {weight:g}. "
                    f"The pass mark is {EVALUATION_ALIGNMENT_BAR}%."
                )
        scored.append({
            "criterion": label,
            "weight": weight,
            "mark": mark,
            "status": status,
            "reason": reason,
            "improvement": improvement,
        })
    percent = report.get("criteria_score_percent")
    if percent is None and weight_total:
        percent = round(100.0 * earned / weight_total, 2)
    at_bar = isinstance(percent, (int, float)) and not isinstance(percent, bool) and percent >= EVALUATION_ALIGNMENT_BAR
    gap = None
    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
        gap = round(max(0.0, EVALUATION_ALIGNMENT_BAR - float(percent)), 2)
    return {
        "bar_percent": EVALUATION_ALIGNMENT_BAR,
        "score_percent": percent,
        "points_short": gap,
        "at_bar": at_bar,
        "warning": WARNING,
        "rows": scored,
    }


def improvement_lines(reading: dict, limit: int = 4) -> str:
    lines = [
        str(row.get("improvement"))
        for row in (reading.get("rows") or [])
        if row.get("improvement")
    ]
    return " ".join(lines[:limit])
