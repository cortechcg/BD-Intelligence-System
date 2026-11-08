"""Deterministic release review built from stored tender, draft and CV evidence.

The quality score is diagnostic. ``release_ready`` is a separate human gate
which also requires hard checks to pass and the reviewer to attest to external
submission items and the final rendered file.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

from intelligence import quality_gate
from intelligence.requirement_alignment import align_draft_to_requirements
from intelligence.tender_reader import build_format_compliance

_META = {
    "submission_type", "lightweight", "lightweight_reason", "quality_score",
    "claim_grounding", "tender_brief", "win_strategy", "document_lock",
    "section_order", "omitted_financial", "submission_outline",
    "required_attachments", "required_forms", "format_compliance",
    "requirement_alignment", "claim_sheet", "draft_editor", "committee_grade",
    "quality_gate", "proposal_versions", "release_approval", "review_pack",
}
_GENERIC = re.compile(
    r"\b(?:world[- ]class|best[- ]in[- ]class|cutting[- ]edge|uniquely positioned|"
    r"robust and innovative|in today's (?:fast[- ]changing|complex) environment)\b",
    re.I,
)


def _drafted(sections: dict) -> dict[str, str]:
    return {
        str(key): value for key, value in (sections or {}).items()
        if key not in _META and isinstance(value, str) and value.strip()
    }


def _person_key(match: dict) -> str:
    # Prefer external identity keys, then the normalized name. CV versions of
    # one person cannot count as multiple people when checking role coverage.
    for key in ("airtable_consultant_id", "airtable_id", "external_id", "consultant_id"):
        value = str(match.get(key) or "").strip()
        if value:
            return "id:" + value.lower()
    name = re.sub(r"[^a-z0-9]+", " ", str(match.get("consultant_name") or "").lower()).strip()
    return "name:" + name if name else ""


def _team_review(analysis: dict, matched: dict) -> tuple[list[dict], dict]:
    requirements = analysis.get("team_requirements") or []
    slots = matched.get("matched_team") if isinstance(matched.get("matched_team"), dict) else {}
    by_person: dict[str, list[str]] = defaultdict(list)
    failures: list[dict] = []
    covered = 0
    unknown_search = bool(matched.get("search_unavailable"))
    for req in requirements:
        if not isinstance(req, dict) or not req.get("role"):
            continue
        role = str(req["role"])
        match = slots.get(role) if isinstance(slots.get(role), dict) else {}
        name = str(match.get("consultant_name") or "")
        capability = match.get("capability") if isinstance(match.get("capability"), dict) else {}
        if name in ("CV SEARCH UNAVAILABLE", "EXTERNAL RECRUITMENT NEEDED", "", "Unknown"):
            unknown = name == "CV SEARCH UNAVAILABLE" or unknown_search
            failures.append({
                "kind": "team_search_unavailable" if unknown else "team_role_unfilled",
                "role": role,
                "message": capability.get("why") or ("CV retrieval did not complete; qualification is unknown." if unknown else "No qualified consultant is evidenced for this mandatory role."),
            })
            continue
        identity = _person_key(match)
        if identity:
            by_person[identity].append(role)
        status = str(capability.get("status") or "UNKNOWN").upper()
        if status != "SATISFIED":
            failures.append({
                "kind": "team_qualification_gap",
                "role": role,
                "message": "Mandatory role requirements are not fully evidenced in the selected CV.",
                "evidence": capability.get("evidence") or [],
                "gaps": capability.get("gaps") or [],
                "unknown": capability.get("unknown") or [],
            })
        else:
            covered += 1
    for identity, roles in by_person.items():
        if len(roles) > 1:
            failures.append({
                "kind": "concurrent_role_conflict",
                "roles": roles,
                "message": "One consultant is assigned to multiple concurrent roles; record a permitted exception before release.",
            })
    required = sum(1 for x in requirements if isinstance(x, dict) and x.get("role"))
    return failures, {
        "required_headcount": required,
        "qualified_role_coverage": covered,
        "alternatives_available": sum(
            1 for role in slots.values()
            if isinstance(role, dict) and len({
                _person_key(candidate) for candidate in (role.get("all_matches") or [])
                if isinstance(candidate, dict) and _person_key(candidate)
            }) > 1
        ),
        "availability_is_separate": True,
        "search_unavailable": unknown_search,
    }


def _committee_assessment(qg: dict, alignment: dict, team_failures: list[dict]) -> dict:
    """Build an explicit internal rubric simulation without inventing weights.

    Awarded points are a deterministic coverage estimate from the separate
    compliance pass, not a model's prose-quality grade or an official score.
    """
    source = str(qg.get("criteria_source") or "unknown")
    tender_based = source in {quality_gate.SOURCE_TOR, quality_gate.SOURCE_TOR_UNWEIGHTED}
    alignment_rows = {
        re.sub(r"[^a-z0-9]+", " ", str(row.get("label") or row.get("text") or "").lower()).strip(): row
        for row in (alignment.get("rows") or [])
        if row.get("kind") == "evaluation_criterion"
    }
    evidence_blockers = [
        item for item in (qg.get("blockers") or [])
        if item.get("kind") in {"unsupported_claim", "unverified_claim"}
    ]
    assessed = []
    weighted = source == quality_gate.SOURCE_TOR and bool(qg.get("rows")) and all(
        isinstance(row.get("weight"), (int, float)) and row.get("weight") > 0
        for row in qg.get("rows") or []
    )
    awarded_total = 0.0
    maximum_total = 0.0
    for item in qg.get("rows") or []:
        criterion = str(item.get("criterion") or "")
        key = re.sub(r"[^a-z0-9]+", " ", criterion.lower()).strip()
        match = alignment_rows.get(key) or {}
        fraction = float(item.get("earned") or 0.0)
        lower = criterion.lower()
        critical_team = any(word in lower for word in ("team", "staff", "expert", "qualification", "personnel"))
        critical_experience = any(word in lower for word in ("experience", "track record", "past performance", "references"))
        if critical_team and team_failures:
            fraction = 0.0
        if critical_experience and evidence_blockers:
            fraction = 0.0
        fraction = max(0.0, min(1.0, fraction))
        maximum = float(item["weight"]) if weighted else None
        awarded = round(maximum * fraction, 2) if maximum is not None else None
        if maximum is not None and awarded is not None:
            maximum_total += maximum
            awarded_total += awarded
        status = str(match.get("status") or item.get("status") or "unknown")
        gaps = []
        if status != "addressed":
            gaps.append(str(match.get("evidence") or item.get("reason") or "Requirement coverage is not verified."))
        if critical_team and team_failures:
            gaps.extend(str(failure.get("message") or "Team evidence gap") for failure in team_failures)
        if critical_experience and evidence_blockers:
            gaps.extend(str(blocker.get("text") or blocker.get("kind")) for blocker in evidence_blockers)
        rationale = str(item.get("reason") or "Deterministic requirement-coverage check; content quality remains for human review.")
        assessed.append({
            "criterion": criterion,
            "maximum_score": maximum,
            "awarded_score": awarded,
            "coverage_fraction": fraction,
            "rationale": rationale,
            "supporting_evidence": {
                "draft_quote": match.get("quote") or "",
                "section": match.get("section") or item.get("section") or "",
                "tender_source": match.get("source_evidence") or {},
            },
            "gaps": list(dict.fromkeys(gaps)),
            "confidence": "medium" if status == "addressed" and not gaps else "low",
            "coverage_status": status,
        })
    return {
        "label": "Simulated internal assessment — not an official procurement committee score or win probability.",
        "method": "Deterministic tender-criterion coverage proxy; a person must assess substantive quality.",
        "source": "tender_rubric" if tender_based else "provisional_fallback",
        "criteria_source": source,
        "weights_complete": weighted,
        "missing_weights": [row["criterion"] for row in assessed if row["maximum_score"] is None],
        "criteria": assessed,
        "weighted_total": {
            "awarded": round(awarded_total, 2),
            "maximum": round(maximum_total, 2),
            "percent": round(100 * awarded_total / maximum_total, 1) if maximum_total else None,
        } if weighted else None,
        "provisional_coverage_percent": qg.get("score") if not weighted else None,
        "provisional_note": quality_gate.SOURCE_NOTES.get(source, "Scoring basis not verified."),
    }


def build_review_pack(
    analysis: dict | None,
    sections: dict | None,
    matched_team_result: dict | None = None,
    *,
    acknowledgements: dict | None = None,
) -> dict:
    analysis = analysis if isinstance(analysis, dict) else {}
    sections = sections if isinstance(sections, dict) else {}
    acknowledgements = acknowledgements if isinstance(acknowledgements, dict) else {}
    qg = quality_gate.score_draft(sections, analysis) if _drafted(sections) else {
        "status": quality_gate.BLOCKED, "score": None, "blockers": [],
        "headline": "No draft is available for review.",
    }
    alignment = align_draft_to_requirements(
        analysis, sections, outline=sections.get("submission_outline") or {}
    )
    provenance = analysis.get("extraction_provenance") if isinstance(analysis.get("extraction_provenance"), dict) else {}
    for row in alignment.get("rows") or []:
        source_path = str(row.get("source") or "")
        # Rows sourced from the reviewer-edited outline still point to the
        # tender's original extracted list when that provenance is available.
        candidates = [source_path]
        if source_path.startswith("submission_outline.sections["):
            candidates.append(source_path.replace("submission_outline.sections[", "submission_requirements.prescribed_proposal_sections["))
        evidence = next((provenance[key] for key in candidates if isinstance(provenance.get(key), dict)), None)
        if evidence is None and source_path.startswith("evaluation_criteria["):
            evidence = provenance.get(source_path + ".criterion") or provenance.get(source_path + ".description")
        row["source_evidence"] = {
            "status": evidence.get("status", "UNKNOWN") if isinstance(evidence, dict) else "UNKNOWN",
            "source_file": evidence.get("source_file") if isinstance(evidence, dict) else None,
            "page": evidence.get("page") if isinstance(evidence, dict) else None,
            "chunk_index": evidence.get("chunk_index") if isinstance(evidence, dict) else None,
            "passage": evidence.get("excerpt") if isinstance(evidence, dict) else None,
        }
    format_report = build_format_compliance(sections, sections.get("submission_outline"))
    hard: list[dict] = []
    evidence_gaps: list[dict] = []
    suggestions: list[dict] = []

    if qg.get("status") != quality_gate.READY:
        for blocker in qg.get("blockers") or []:
            hard.append({"kind": blocker.get("kind", "quality_gate"), "message": blocker.get("text") or blocker.get("kind"), "section": blocker.get("section", "")})
        if not qg.get("blockers"):
            hard.append({"kind": "quality_gate", "message": "Diagnostic quality gate is blocked or unavailable."})

    for row in alignment.get("rows") or []:
        kind = str(row.get("kind") or "")
        status = str(row.get("status") or "")
        if status == "not_addressed" and kind in {
            "prescribed_section", "disqualifying_condition", "financial_proposal", "cvs",
        }:
            hard.append({
                "kind": "requirement_uncovered",
                "message": f"Tender requirement is not covered: {row.get('label') or row.get('text')}",
                "source": row.get("source", ""), "section": row.get("section", ""),
            })
        elif status in {"unknown", "not_addressed"}:
            evidence_gaps.append({
                "kind": "requirement_evidence_gap",
                "requirement": row.get("label") or row.get("text"),
                "source": row.get("source", ""),
                "evidence": row.get("evidence") or "Not traced; not treated as satisfied.",
            })

    for row in format_report.get("rows") or []:
        if row.get("status") in ("over", "gantt_missing"):
            hard.append({"kind": "format_limit", "message": row.get("note") or row.get("heading"), "section": row.get("heading")})
        elif row.get("status") == "under":
            suggestions.append({"kind": "section_length", "message": row.get("note") or "Section is shorter than its target.", "section": row.get("heading")})

    team_failures, team_coverage = _team_review(analysis, matched_team_result or {})
    hard.extend(team_failures)
    committee_assessment = _committee_assessment(qg, alignment, team_failures)

    paragraphs = []
    for section, content in _drafted(sections).items():
        for paragraph in re.split(r"\n\s*\n", content):
            normalized = " ".join(paragraph.lower().split())
            if len(normalized.split()) >= 35:
                paragraphs.append((section, normalized))
        if _GENERIC.search(content):
            suggestions.append({"kind": "generic_filler", "section": section, "message": "Generic promotional wording needs assignment-specific evidence or replacement."})
    seen: dict[str, str] = {}
    for section, para in paragraphs:
        digest = hashlib.sha256(para.encode("utf-8")).hexdigest()
        other = seen.get(digest)
        if other and other != section:
            suggestions.append({"kind": "repeated_text", "section": section, "message": f"A paragraph repeats text from {other}."})
        seen[digest] = section

    sub = analysis.get("submission_requirements") if isinstance(analysis.get("submission_requirements"), dict) else {}
    outline = sections.get("submission_outline") if isinstance(sections.get("submission_outline"), dict) else {}
    external_items = list(sub.get("required_annexes") or []) + list(outline.get("required_attachments") or []) + list(outline.get("required_forms") or [])
    for key, label in (
        ("cvs_required", "CVs"), ("past_work_samples_required", "past-work samples"),
        ("references_required", "references"), ("financial_proposal_required", "separate financial proposal"),
    ):
        value = sub.get(key)
        if value is True or (isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0):
            external_items.append(label)
    needs_attachment_ack = bool(external_items)
    needs_final_check = True  # word estimates do not equal the rendered Word/PDF layout
    needs_submission_check = not acknowledgements.get("submission_checked")
    if needs_attachment_ack and not acknowledgements.get("attachments_verified"):
        evidence_gaps.append({"kind": "external_submission_items", "items": sorted(set(map(str, external_items))), "message": "A reviewer must confirm all required attachments/forms and separate envelopes are ready."})
    if needs_final_check and not acknowledgements.get("final_document_checked"):
        evidence_gaps.append({"kind": "rendered_document_review", "message": "Final page count, pagination, signatures and rendered Word/PDF presentation need human inspection."})
    if acknowledgements.get("attachments_verified") and needs_attachment_ack:
        external_items = []
    if acknowledgements.get("final_document_checked"):
        needs_final_check = False
    if needs_submission_check:
        evidence_gaps.append({"kind": "submission_instructions_review", "message": "A human must confirm eligibility, tender instructions, deadlines and any unknown requirements against the original tender."})

    release_eligible = qg.get("status") == quality_gate.READY and not hard
    release_ready = (
        release_eligible
        and not (needs_attachment_ack and not acknowledgements.get("attachments_verified"))
        and not needs_final_check
        and not needs_submission_check
    )
    return {
        "diagnostic_score": qg.get("score"),
        "diagnostic_status": qg.get("status", quality_gate.BLOCKED),
        "diagnostic_note": "A quality score is a drafting diagnostic, not a win probability or proof of procurement compliance.",
        "hard_compliance_failures": hard,
        "evidence_gaps": evidence_gaps,
        "editorial_suggestions": suggestions,
        "requirement_matrix": alignment.get("rows") or [],
        "team_coverage": team_coverage,
        "committee_assessment": committee_assessment,
        "format_review": format_report,
        "external_items": sorted(set(map(str, external_items))) if needs_attachment_ack else [],
        "needs_attachment_ack": needs_attachment_ack,
        "needs_final_document_check": needs_final_check,
        "needs_submission_check": needs_submission_check,
        "release_eligible": release_eligible,
        "release_ready": release_ready,
    }


def draft_fingerprint(analysis: dict, sections: dict, matched_team_result: dict | None, evidence_hash: str = "") -> str:
    """Hash the exact draft, extracted tender and selected team used at approval."""
    material = {
        "analysis": analysis or {}, "sections": sections or {},
        "team": matched_team_result or {}, "evidence_hash": evidence_hash or "",
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evidence_fingerprint(content_hash: str = "", tender_text: str = "") -> str:
    """Fingerprint the tender bytes and extracted text used for this draft."""
    text_hash = hashlib.sha256(str(tender_text or "").encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{content_hash or ''}:{text_hash}".encode("ascii")).hexdigest()
