# intelligence/draft_templates.py
"""Outlines a person picks after scoring, before any draft is written.

The scorer stores one recommended outline for this ToR. The catalog holds
the Cortech house outlines (one technical, one EOI). A save writes an
accepted outline onto this opportunity only. It does not change the catalog.
"""
from __future__ import annotations

import copy

from intelligence.tender_reader import plan_draft_outline

FROM_TOR_ID = "from_tor"
HOUSE_TECHNICAL_ID = "house_technical"
HOUSE_EOI_ID = "house_eoi"
CUSTOM_ID = "custom"

_MAX_SECTIONS = 16
_SUBMISSION_TYPES = ("FULL_PROPOSAL", "EOI")

_TECHNICAL_HEADINGS = (
    "Cover letter",
    "Executive summary",
    "Organisational profile",
    "Understanding of the assignment",
    "Methodology",
    "Data analysis plan",
    "Quality assurance and ethics",
    "Risk management",
    "Proposed team",
    "Work plan",
)

_EOI_HEADINGS = (
    "Letter of interest",
    "Firm profile",
    "Understanding of the assignment",
    "Proposed approach",
    "Relevant experience",
    "Key experts",
    "Eligibility",
)

_EOI_MUST = {
    "Proposed approach": [
        "A short approach summary only. Do not write a full methodology chapter.",
    ],
}


def outline_is_accepted(outline: dict | None) -> bool:
    """True when a person (or a saved checkpoint) has chosen real chapters."""
    if not isinstance(outline, dict):
        return False
    sections = outline.get("sections")
    if not isinstance(sections, list) or not sections:
        return False
    return any(
        isinstance(item, dict) and str(item.get("heading") or "").strip()
        for item in sections
    )


def as_submission_outline(outline: dict) -> dict:
    """The shape proposal_writer already drafts from."""
    return {
        "prescribed": True,
        "sections": list(outline.get("sections") or []),
        "omitted_financial": list(outline.get("omitted_financial") or []),
        "required_attachments": list(outline.get("required_attachments") or []),
        "required_forms": list(outline.get("required_forms") or []),
    }


def _submission_type(value: str | None, default: str = "FULL_PROPOSAL") -> str:
    text = str(value or "").strip().upper().replace(" ", "_").replace("-", "_")
    if text in {"EOI", "EXPRESSION_OF_INTEREST", "REOI"}:
        return "EOI"
    if text in {"FULL_PROPOSAL", "TECHNICAL_PROPOSAL", "PROPOSAL", "RFP", "TOR"}:
        return "FULL_PROPOSAL"
    return default if default in _SUBMISSION_TYPES else "FULL_PROPOSAL"


def _from_headings(
    headings: tuple[str, ...] | list[str],
    submission_type: str,
    must_include: dict | None = None,
    page_limits: dict | None = None,
) -> dict:
    must_include = must_include or {}
    page_limits = page_limits or {}
    prescribed = []
    for heading in headings:
        prescribed.append({
            "heading": heading,
            "page_limit": str(page_limits.get(heading) or ""),
            "kind": "technical",
            "must_include": list(must_include.get(heading) or []),
        })
    return plan_draft_outline(
        {"format_prescribed": True, "prescribed_sections": prescribed},
        {},
        submission_type,
    )


def _wrap(
    template_id: str,
    name: str,
    submission_type: str,
    source: str,
    summary: str,
    planned: dict,
) -> dict:
    return {
        "id": template_id,
        "name": name,
        "submission_type": submission_type,
        "source": source,
        "summary": summary,
        "sections": list(planned.get("sections") or []),
        "omitted_financial": list(planned.get("omitted_financial") or []),
        "required_attachments": list(planned.get("required_attachments") or []),
        "required_forms": list(planned.get("required_forms") or []),
    }


def house_technical() -> dict:
    planned = _from_headings(_TECHNICAL_HEADINGS, "FULL_PROPOSAL")
    return _wrap(
        HOUSE_TECHNICAL_ID,
        "Cortech technical proposal",
        "FULL_PROPOSAL",
        "template",
        "House chapters for a technical proposal when the ToR does not list its own.",
        planned,
    )


def house_eoi() -> dict:
    planned = _from_headings(_EOI_HEADINGS, "EOI", must_include=_EOI_MUST)
    return _wrap(
        HOUSE_EOI_ID,
        "Cortech EOI",
        "EOI",
        "template",
        "House chapters for an expression of interest. The approach stays a summary.",
        planned,
    )


def catalog() -> list[dict]:
    """Templates a person can pick instead of the recommendation."""
    return [house_technical(), house_eoi()]


def recommend_outline(analysis: dict | None) -> dict:
    """The outline to offer after scoring. Built from the analysis, no extra model call."""
    analysis = analysis if isinstance(analysis, dict) else {}
    bid = analysis.get("bid_analysis") if isinstance(analysis.get("bid_analysis"), dict) else {}
    submission_type = _submission_type(bid.get("submission_type"))
    planned = plan_draft_outline({}, analysis, submission_type)
    sections = planned.get("sections") or []
    if planned.get("prescribed") and sections:
        headings = [str(item.get("heading") or "") for item in sections if item.get("heading")]
        listed = "; ".join(headings)
        return _wrap(
            FROM_TOR_ID,
            "From this ToR",
            submission_type,
            "tor",
            f"These are the chapters this ToR asks for: {listed}.",
            planned,
        )
    house = house_eoi() if submission_type == "EOI" else house_technical()
    recommended = copy.deepcopy(house)
    recommended["id"] = FROM_TOR_ID
    kind = "an EOI" if submission_type == "EOI" else "a technical proposal"
    recommended["summary"] = (
        "This ToR does not list required chapters. "
        f"The recommendation is the Cortech house outline for {kind}."
    )
    return recommended


def customize_outline(
    submission_type: str,
    sections: list,
    *,
    name: str = "Custom outline",
) -> dict:
    """Turn edited headings into a draft outline for this opportunity."""
    submission_type = _submission_type(submission_type)
    cleaned = []
    for item in sections or []:
        if not isinstance(item, dict):
            continue
        heading = " ".join(str(item.get("heading") or "").split()).strip()
        if not heading:
            continue
        must = item.get("must_include") or []
        if isinstance(must, str):
            must = [must]
        if not isinstance(must, list):
            must = []
        must_include = []
        for entry in must:
            text = " ".join(str(entry or "").split()).strip()
            if text:
                must_include.append(text[:200])
            if len(must_include) >= 8:
                break
        cleaned.append({
            "heading": heading[:200],
            "page_limit": " ".join(str(item.get("page_limit") or "").split())[:40],
            "kind": "technical",
            "must_include": must_include,
        })
    if not cleaned:
        raise ValueError("an outline needs at least one section heading")
    if len(cleaned) > _MAX_SECTIONS:
        raise ValueError(f"an outline can have at most {_MAX_SECTIONS} sections")
    planned = plan_draft_outline(
        {"format_prescribed": True, "prescribed_sections": cleaned},
        {},
        submission_type,
    )
    if not planned.get("prescribed") or not planned.get("sections"):
        raise ValueError("those headings do not make a draft outline")
    label = " ".join(str(name or "").split()).strip()[:120] or "Custom outline"
    return _wrap(CUSTOM_ID, label, submission_type, "custom", "Edited for this opportunity.", planned)


def resolve_outline(payload: dict | None, recommended: dict | None = None) -> dict:
    """Pick the recommended outline, a catalog template, or edited sections.

    Edited sections win over a template id. The result is for this opportunity.
    """
    payload = payload if isinstance(payload, dict) else {}
    template_id = str(payload.get("template_id") or "").strip()
    name = " ".join(str(payload.get("name") or "").split()).strip()
    by_id = {item["id"]: item for item in catalog()}
    requested = str(payload.get("submission_type") or "").strip()
    if requested:
        submission_type = _submission_type(requested)
    elif template_id in by_id:
        submission_type = by_id[template_id]["submission_type"]
    elif isinstance(recommended, dict) and recommended.get("submission_type") in _SUBMISSION_TYPES:
        submission_type = recommended["submission_type"]
    else:
        submission_type = "FULL_PROPOSAL"

    raw_sections = payload.get("sections")
    if isinstance(raw_sections, list) and any(
        isinstance(item, dict) and str(item.get("heading") or "").strip()
        for item in raw_sections
    ):
        return customize_outline(
            submission_type,
            raw_sections,
            name=name or "Custom outline",
        )

    if template_id == FROM_TOR_ID:
        if not outline_is_accepted(recommended):
            raise ValueError("this opportunity has no recommended outline yet")
        chosen = copy.deepcopy(recommended)
        chosen["id"] = FROM_TOR_ID
        if name:
            chosen["name"] = name[:120]
        return chosen

    if template_id in by_id:
        chosen = copy.deepcopy(by_id[template_id])
        if name:
            chosen["name"] = name[:120]
        return chosen

    raise ValueError("select a template or send the sections to use")
