"""Evidence-bound internal budget preparation.

This module deliberately does not ask an LLM to invent effort, day rates,
workshops, overhead, contingency, or travel. A budget can be useful only when
its numbers are traceable to an extracted ToR field or Cortech's maintained
rate card. Missing inputs are returned explicitly for a human estimator.
"""

from __future__ import annotations

import math
from typing import Any

from loguru import logger

from database.airtable_client import get_rate_card


BUDGET_COMPLETE = "COMPLETE"
BUDGET_PARTIAL = "PARTIAL"
BUDGET_INSUFFICIENT = "INSUFFICIENT DATA"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _key(value: Any) -> str:
    return " ".join(_text(value).casefold().split())


def _positive_number(value: Any) -> float | None:
    """Accept a finite positive number; zero/unknown/invalid are not estimates."""
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _empty_budget(primary_location: str, missing_inputs: list[str], reason: str) -> dict:
    return {
        "status": BUDGET_INSUFFICIENT,
        "currency": "USD",
        "primary_location": primary_location,
        "reason": reason,
        "missing_inputs": missing_inputs,
        "excluded_costs": [
            "travel and logistics",
            "data collection tools and materials",
            "workshops and events",
            "management overhead",
            "contingency",
            "taxes",
        ],
        "summary": {
            "personnel_subtotal_usd": None,
            "known_personnel_subtotal_usd": 0,
            "grand_total_usd": None,
        },
        "personnel_breakdown": {},
    }


def _rate_lookup(rate_card: list[dict]) -> dict[tuple[str, str], dict]:
    """Index only explicit, usable rate-card rows by level and location."""
    rates: dict[tuple[str, str], dict] = {}
    for row in rate_card or []:
        if not isinstance(row, dict):
            continue
        level = _key(row.get("role_level"))
        location = _key(row.get("location"))
        day_rate = _positive_number(row.get("day_rate_usd"))
        if not level or not location or day_rate is None:
            continue
        # The first row is retained deterministically. Conflicting rate-card
        # rows must be cleaned by the owner instead of being averaged here.
        rates.setdefault((level, location), {
            "day_rate_usd": day_rate,
            "per_diem_usd": _positive_number(row.get("per_diem_usd")),
        })
    return rates


def calculate_budget(
    analysis: dict,
    matched_team: dict,
    primary_location: str = "Nairobi",
) -> dict:
    """Calculate only a traceable personnel subtotal.

    ``estimated_days_of_effort`` must have been explicitly extracted from the
    tender for each role, and a matching Cortech rate-card row must exist for
    its level and project location. ``matched_team`` remains part of the
    public interface but does not supply a fabricated rate or effort value.

    A full financial proposal is never claimed ready: the current data model
    has no evidence-backed inputs for non-personnel costs. The returned
    ``status`` and ``missing_inputs`` make that limitation operational.
    """
    del matched_team  # Rates and effort must not be inferred from a CV match.

    analysis = analysis or {}
    opportunity = analysis.get("opportunity") or {}
    if not isinstance(opportunity, dict):
        opportunity = {}
    requirements = analysis.get("team_requirements") or []
    if not isinstance(requirements, list) or not requirements:
        return _empty_budget(
            primary_location,
            ["team_requirements with explicit estimated_days_of_effort"],
            "No team effort requirements were extracted from the tender.",
        )

    try:
        rate_card = get_rate_card()
    except Exception as exc:
        # The Airtable client normally fails open, but protect this critical
        # boundary so an unavailable rate card can never create default prices.
        logger.warning(f"Rate-card lookup failed; budget remains incomplete: {exc}")
        rate_card = []
    rates = _rate_lookup(rate_card)
    location_key = _key(primary_location)

    personnel: dict[str, dict] = {}
    missing: list[str] = []
    known_total = 0.0
    complete_personnel = True

    for index, requirement in enumerate(requirements, start=1):
        if not isinstance(requirement, dict):
            complete_personnel = False
            missing.append(f"valid team requirement #{index}")
            continue

        role = _text(requirement.get("role")) or f"Unspecified role #{index}"
        level = _text(requirement.get("level"))
        effort_days = _positive_number(requirement.get("estimated_days_of_effort"))
        rate = rates.get((_key(level), location_key)) if level else None

        line: dict[str, Any] = {
            "role": role,
            "level": level or None,
            "estimated_days_of_effort": effort_days,
            "day_rate_usd": rate["day_rate_usd"] if rate else None,
            "personnel_cost_usd": None,
            "status": "UNKNOWN",
            "evidence": [],
        }
        if effort_days is None:
            complete_personnel = False
            missing.append(f"explicit estimated_days_of_effort for {role}")
        else:
            line["evidence"].append(
                "team_requirements.estimated_days_of_effort"
            )

        if not level:
            complete_personnel = False
            missing.append(f"role level for {role}")
        elif rate is None:
            complete_personnel = False
            missing.append(
                f"rate-card day_rate_usd for {level} at {primary_location} ({role})"
            )
        else:
            line["evidence"].append("Airtable RATE_CARDS.day_rate_usd")

        if effort_days is not None and rate is not None:
            cost = effort_days * rate["day_rate_usd"]
            line["personnel_cost_usd"] = round(cost, 2)
            line["status"] = "VERIFIED"
            known_total += cost
        elif effort_days is not None or rate is not None:
            line["status"] = "PARTIAL"

        # Duplicate role labels are valid in poor-quality extractions. Keep
        # both lines rather than silently overwrite a cost.
        key = role
        suffix = 2
        while key in personnel:
            key = f"{role} ({suffix})"
            suffix += 1
        personnel[key] = line

    # The supplied model does not contain verified inputs for non-personnel
    # costs, so do not turn an accurate subtotal into a fake total.
    non_personnel_missing = [
        "travel/logistics assumptions or quotations",
        "data-collection/tools assumptions or quotations",
        "workshop/event assumptions or quotations",
        "approved overhead, contingency, and tax treatment",
    ]
    missing.extend(non_personnel_missing)
    status = BUDGET_PARTIAL if known_total > 0 else BUDGET_INSUFFICIENT
    summary = {
        "personnel_subtotal_usd": round(known_total, 2) if complete_personnel else None,
        "known_personnel_subtotal_usd": round(known_total, 2),
        "grand_total_usd": None,
    }

    budget = {
        "status": status,
        "currency": "USD",
        "primary_location": primary_location,
        "reason": (
            "Personnel costed from explicit ToR effort and rate-card inputs; "
            "full financial proposal still needs non-personnel cost evidence."
            if known_total > 0
            else "No personnel amount could be calculated from verified effort and rate-card inputs."
        ),
        "missing_inputs": missing,
        "excluded_costs": non_personnel_missing,
        "summary": summary,
        "personnel_breakdown": personnel,
        "opportunity_budget_cap_usd": _positive_number(
            opportunity.get("estimated_budget_usd")
        ),
    }
    logger.info(
        f"Budget {status}: known personnel subtotal ${known_total:,.2f}; "
        f"{len(missing)} required input(s) unresolved"
    )
    return budget


def estimate_workshop_costs(deliverables: list, location: str) -> dict:
    """Compatibility shim: workshop cost is UNKNOWN without actual inputs."""
    del deliverables, location
    return {
        "status": BUDGET_INSUFFICIENT,
        "total": None,
        "reason": "No evidence-backed workshop quantities or unit costs are available.",
    }
