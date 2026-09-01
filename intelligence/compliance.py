"""Compliance matrix for SUBMISSION requirements — not assignment evaluation criteria.

Statuses: SATISFIED / PARTIAL / MISSING / UNKNOWN
Never infers that a draft 'probably' meets a scored criterion.
"""

from __future__ import annotations

STATUSES = ("SATISFIED", "PARTIAL", "MISSING", "UNKNOWN")


def _row(requirement: str, status: str, evidence: str, source: str) -> dict:
    if status not in STATUSES:
        status = "UNKNOWN"
    return {
        "requirement": requirement,
        "status": status,
        "evidence": evidence,
        "source": source,
    }


def build_compliance_matrix(
    analysis: dict,
    matched_team_result: dict | None = None,
    proposal_sections: dict | None = None,
) -> list[dict]:
    analysis = analysis or {}
    sub = analysis.get("submission_requirements") or {}
    award = analysis.get("evaluation_criteria") or []
    # Explicitly unused: assignment_evaluation_framework is how the consultant
    # will evaluate a project, not how the buyer scores this proposal.
    _ = analysis.get("assignment_evaluation_framework")

    rows: list[dict] = []
    sections = proposal_sections or {}
    drafted = [k for k, v in sections.items() if isinstance(v, str) and v.strip()]

    cvs_required = sub.get("cvs_required")
    if cvs_required is True:
        team = (matched_team_result or {}).get("matched_team") or {}
        gaps = (matched_team_result or {}).get("gaps") or []
        named = [
            m.get("consultant_name")
            for m in team.values()
            if isinstance(m, dict)
            and m.get("consultant_name")
            and m.get("consultant_name") != "EXTERNAL RECRUITMENT NEEDED"
        ]
        if named and not gaps:
            rows.append(_row(
                "CVs required",
                "PARTIAL",
                f"Team matched ({', '.join(named)}); CVs not attached to this draft",
                "submission_requirements.cvs_required",
            ))
        elif named:
            rows.append(_row(
                "CVs required",
                "PARTIAL",
                f"Some roles matched ({', '.join(named)}); gaps: {', '.join(map(str, gaps))}",
                "submission_requirements.cvs_required",
            ))
        else:
            rows.append(_row(
                "CVs required",
                "MISSING",
                "ToR requires CVs; no named consultants matched",
                "submission_requirements.cvs_required",
            ))
    elif cvs_required is False:
        rows.append(_row("CVs required", "SATISFIED", "ToR does not require CVs", "submission_requirements.cvs_required"))
    else:
        rows.append(_row("CVs required", "UNKNOWN", "cvs_required not extracted", "submission_requirements.cvs_required"))

    if sub.get("financial_proposal_required") is True:
        rows.append(_row(
            "Financial proposal",
            "MISSING",
            "This pipeline drafts the technical proposal only — financial is a separate artefact",
            "submission_requirements.financial_proposal_required",
        ))
    elif sub.get("financial_proposal_required") is False:
        rows.append(_row(
            "Financial proposal",
            "SATISFIED",
            "ToR does not require a financial proposal",
            "submission_requirements.financial_proposal_required",
        ))
    else:
        rows.append(_row(
            "Financial proposal",
            "UNKNOWN",
            "financial_proposal_required not extracted",
            "submission_requirements.financial_proposal_required",
        ))

    page_limit = sub.get("technical_proposal_page_limit")
    if page_limit:
        rows.append(_row(
            f"Technical proposal page limit ({page_limit})",
            "UNKNOWN",
            "Page count is not measured in this pipeline",
            "submission_requirements.technical_proposal_page_limit",
        ))

    refs = sub.get("references_required")
    if refs:
        rows.append(_row(
            f"References required ({refs})",
            "UNKNOWN",
            "References are not generated or verified here",
            "submission_requirements.references_required",
        ))

    samples = sub.get("past_work_samples_required")
    if samples:
        rows.append(_row(
            f"Past work samples required ({samples})",
            "UNKNOWN",
            "Past-work retrieval cites corpus chunks; sample annexes are not attached",
            "submission_requirements.past_work_samples_required",
        ))

    for criterion in award:
        if not isinstance(criterion, dict):
            continue
        name = criterion.get("criterion") or "unnamed criterion"
        weight = criterion.get("weight_percent")
        label = f"Award criterion: {name}" + (f" ({weight}%)" if weight is not None else "")
        if drafted:
            rows.append(_row(
                label,
                "UNKNOWN",
                "A draft exists but this matrix does not claim the criterion is satisfied",
                "evaluation_criteria",
            ))
        else:
            rows.append(_row(
                label,
                "UNKNOWN",
                "No draft yet — not evaluated",
                "evaluation_criteria",
            ))

    return rows
