# intelligence/cv_matcher.py
import json
from loguru import logger
from database.supabase_client import search_consultants
from database.airtable_client import get_all_consultants, log_agent_action

def _as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


def _overlap(required: list[str], have: list[str]) -> tuple[list[str], list[str]]:
    have_l = [h.lower() for h in have]
    hits, misses = [], []
    for item in required:
        needle = str(item).lower()
        if any(needle in h or h in needle for h in have_l if h):
            hits.append(item)
        else:
            misses.append(item)
    return hits, misses


def score_capability_match(requirement: dict, match: dict) -> dict:
    """Semantic similarity plus explicit geography/language/years. Never infers education or certifications that are not on the CV metadata."""
    requirement = requirement or {}
    meta = match.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}

    semantic = match.get("similarity")
    if semantic is None:
        semantic = (match.get("similarity_score") or 0) / 100.0
    try:
        semantic = float(semantic)
    except (TypeError, ValueError):
        semantic = 0.0
    if semantic > 1:
        semantic = semantic / 100.0

    evidence = [f"semantic similarity {round(semantic * 100)}%"]
    gaps = []
    unknown = []
    parts = {"semantic": round(semantic * 100, 2)}

    geo_req = _as_list(requirement.get("required_geographic_experience"))
    geo_have = _as_list(meta.get("geographic_experience"))
    if geo_req:
        if geo_have:
            hits, misses = _overlap(geo_req, geo_have)
            parts["geography"] = round(100.0 * len(hits) / len(geo_req), 2) if geo_req else None
            if hits:
                evidence.append("geography: " + ", ".join(map(str, hits)))
            if misses:
                gaps.extend(f"geography:{m}" for m in misses)
        else:
            parts["geography"] = None
            unknown.append("geographic_experience not on CV metadata")
    else:
        parts["geography"] = None

    lang_req = _as_list(
        requirement.get("required_languages")
        or requirement.get("language_requirements")
    )
    lang_have = _as_list(meta.get("languages"))
    if lang_req:
        if lang_have:
            hits, misses = _overlap(lang_req, lang_have)
            parts["language"] = round(100.0 * len(hits) / len(lang_req), 2)
            if hits:
                evidence.append("languages: " + ", ".join(map(str, hits)))
            if misses:
                gaps.extend(f"language:{m}" for m in misses)
        else:
            parts["language"] = None
            unknown.append("languages not on CV metadata")

    years_need = requirement.get("years_experience_minimum")
    years_have = meta.get("years_experience")
    if years_need not in (None, "", 0):
        try:
            need_n = float(years_need)
            if years_have in (None, ""):
                parts["years"] = None
                unknown.append("years_experience not on CV metadata")
            else:
                have_n = float(years_have)
                parts["years"] = 100.0 if have_n >= need_n else round(100.0 * have_n / need_n, 2)
                evidence.append(f"years_experience={have_n} (need {need_n})")
                if have_n < need_n:
                    gaps.append(f"years_experience:{have_n}<{need_n}")
        except (TypeError, ValueError):
            parts["years"] = None
            unknown.append("years_experience unparseable")

    education = requirement.get("required_education")
    if education:
        # Education is not stored on cv_embeddings.metadata — do not infer it.
        parts["education"] = None
        unknown.append("education not in CV metadata — not inferred")
        gaps.append("education:UNKNOWN")

    numeric = [v for v in (parts.get("semantic"), parts.get("geography"), parts.get("language"), parts.get("years")) if v is not None]
    match_score = round(sum(numeric) / len(numeric), 2) if numeric else 0.0
    if unknown and not gaps:
        confidence = "INFERRED"
    elif unknown:
        confidence = "INFERRED"
    else:
        confidence = "VERIFIED"

    why_bits = [evidence[0]]
    if gaps:
        why_bits.append("gaps: " + ", ".join(gaps))
    if unknown:
        why_bits.append("unknown: " + ", ".join(unknown))

    return {
        "match_score": match_score,
        "why": "; ".join(why_bits),
        "evidence": evidence,
        "gaps": gaps,
        "unknown": unknown,
        "factors": parts,
        "confidence": confidence,
        "status": "PARTIAL" if gaps or unknown else "SATISFIED",
    }


# Human prerequisite — CONSULTANTS table must include and maintain:
#   current_projects, available_from, availability_percentage, booked_until
# This feature is only as good as those fields staying current.


def filter_by_availability(matches: list[dict]) -> list[dict]:
    """Annotates matches with live availability from Airtable. Never
    drops a match for missing data — flags it and lets a human decide.
    One table.all() — not a get() per consultant — so a 429 cannot
    serialize the pipeline for tens of minutes."""
    consultants = get_all_consultants()
    by_id = {c.get("id"): c for c in consultants}
    for match in matches:
        airtable_id = match.get("airtable_consultant_id") or match.get("airtable_id")
        consultant = by_id.get(airtable_id) if airtable_id else None
        if not consultant:
            match["availability_flag"] = "Unknown"
            continue
        pct = consultant.get("availability_percentage")
        if pct is None:
            pct = 100
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = 100
        match["availability_percent"] = pct
        match["current_project_count"] = consultant.get("current_projects") or 0
        match["availability_flag"] = (
            "Available" if pct >= 50 else
            "Partially" if pct >= 20 else
            "Busy"
        )
    return sorted(matches, key=lambda m: m.get("availability_percent", 100), reverse=True)


def match_team_to_requirements(
    team_requirements: list[dict],
    opportunity_id: str = None,
    opportunity_title: str = ""
) -> dict:
    """
    For each required role in the ToR, find the best matching
    Cortech consultant using semantic search.
    """
    matched_team = {}
    gaps = []

    logger.info(f"Matching team for {len(team_requirements)} roles...")

    for req in team_requirements or []:
        if not isinstance(req, dict):
            continue
        role = req.get("role") or "Unknown"
        level = req.get("level") or "Senior"
        skills = req.get("required_skills") or []
        geo = req.get("required_geographic_experience") or []
        if isinstance(skills, str):
            skills = [skills]
        if isinstance(geo, str):
            geo = [geo]

        # Build semantic search query from requirements
        search_query = f"""
        {level} {role}
        Skills required: {', '.join(str(s) for s in skills)}
        Geographic experience needed: {', '.join(str(g) for g in geo)}
        Years experience: {req.get('years_experience_minimum') or 0}+
        Education: {req.get('required_education') or ''}
        """

        # Search CV database
        matches = search_consultants(
            query_text=search_query,
            match_threshold=0.60,
            match_count=3
        )

        if matches:
            best_match = matches[0] if isinstance(matches[0], dict) else {}
            capability = score_capability_match(req, best_match)
            try:
                similarity = float(best_match.get("similarity") or 0)
            except (TypeError, ValueError):
                similarity = 0.0
            if similarity > 1:
                similarity = similarity / 100.0
            name = best_match.get("consultant_name") or "Unknown"
            matched_team[role] = {
                "consultant_name": name,
                "airtable_id": best_match.get("airtable_consultant_id"),
                "role_title": best_match.get("role_title") or "",
                "similarity_score": round(similarity * 100),
                "metadata": best_match.get("metadata") or {},
                "all_matches": matches,
                "requirement": req,
                "capability": capability,
            }
            logger.success(
                f"  {role} → {name} "
                f"(score: {round(similarity * 100)}%)"
            )
        else:
            gaps.append(role)
            matched_team[role] = {
                "consultant_name": "EXTERNAL RECRUITMENT NEEDED",
                "airtable_id": None,
                "similarity_score": 0,
                "requirement": req,
                "capability": {
                    "match_score": 0,
                    "why": "No CV in the corpus passed the semantic threshold",
                    "evidence": [],
                    "gaps": [role],
                    "confidence": "VERIFIED",
                    "status": "MISSING",
                },
            }
            logger.warning(f"  {role} → NO MATCH FOUND — External recruitment needed")

    try:
        log_agent_action(
            action_type="Analysis",
            description=f"CV matching: {len(matched_team)} roles, {len(gaps)} gaps",
            opportunity_id=opportunity_id,
            tokens_used=0,
            status="Success",
        )
    except Exception:
        pass

    return {
        "matched_team": matched_team,
        "gaps": gaps,
        "coverage_percent": round(
            (len(matched_team) - len(gaps)) / len(matched_team) * 100
            if matched_team else 0
        )
    }
