# intelligence/cv_matcher.py
import json
from loguru import logger
from database.supabase_client import EmbeddingError, search_consultants
from database.airtable_client import get_all_consultants, log_agent_action

_SCORE_KEYS = (
    "semantic",
    "geography",
    "sector",
    "language",
    "years",
    "skills",
    "availability",
)
_AVAIL_SCORE = {"Available": 100.0, "Partially": 50.0, "Busy": 20.0}
_AVAIL_RANK = {"Available": 3, "Partially": 2, "Unknown": 1, "Busy": 0}


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


def _explicit_overlay(
    required: list[str],
    have: list[str],
    *,
    factor: str,
    missing_meta: str,
) -> tuple[float | None, list[str], list[str], list[str]]:
    """Score a required list against CV metadata. Missing metadata is UNKNOWN, not a fail."""
    if not required:
        return None, [], [], []
    if not have:
        return None, [], [], [missing_meta]
    hits, misses = _overlap(required, have)
    value = round(100.0 * len(hits) / len(required), 2)
    evidence = [f"{factor}: " + ", ".join(map(str, hits))] if hits else []
    gaps = [f"{factor}:{m}" for m in misses]
    return value, evidence, gaps, []


def merge_requirement_context(req: dict, context: dict | None) -> dict:
    """Fill empty role fields from opportunity-level requirements. Never overwrite role facts."""
    merged = dict(req or {})
    ctx = context or {}
    if not _as_list(merged.get("required_geographic_experience")):
        fallback = ctx.get("geographic_experience") or ctx.get("project_location") or []
        if fallback:
            merged["required_geographic_experience"] = fallback
    if not _as_list(merged.get("required_languages") or merged.get("language_requirements")):
        langs = ctx.get("language_requirements") or []
        if langs:
            merged["required_languages"] = langs
    if not _as_list(
        merged.get("required_thematic_areas")
        or merged.get("thematic_areas")
        or merged.get("sector")
    ):
        themes = ctx.get("thematic_areas") or []
        if themes:
            merged["required_thematic_areas"] = themes
    return merged


def parse_availability(consultant: dict | None) -> dict:
    """Live Airtable availability only. Missing or unrecognized values stay UNKNOWN."""
    if not consultant:
        return {
            "availability_flag": "Unknown",
            "availability_percent": None,
            "availability_status": None,
            "availability_evidence": (
                "Consultant not found in Airtable — availability not inferred"
            ),
        }

    status_raw = consultant.get("availability_status")
    pct_raw = consultant.get("availability_percentage")
    flag = None
    pct = None
    evidence = []

    if pct_raw not in (None, ""):
        try:
            pct = float(pct_raw)
            evidence.append(f"availability_percentage={pct}")
            if pct >= 50:
                flag = "Available"
            elif pct >= 20:
                flag = "Partially"
            else:
                flag = "Busy"
        except (TypeError, ValueError):
            evidence.append("availability_percentage unparseable")

    if status_raw not in (None, ""):
        status = str(status_raw).strip()
        evidence.append(f"availability_status={status}")
        if flag is None:
            label = status.lower()
            if label in ("available", "free"):
                flag = "Available"
            elif label in ("partial", "partially", "partially available"):
                flag = "Partially"
            elif label in ("busy", "unavailable", "booked"):
                flag = "Busy"
            else:
                flag = "Unknown"
                evidence.append("unrecognized availability_status — not inferred")

    if flag is None:
        return {
            "availability_flag": "Unknown",
            "availability_percent": None,
            "availability_status": status_raw,
            "availability_evidence": (
                "availability_status not on Airtable record — not inferred"
            ),
        }

    return {
        "availability_flag": flag,
        "availability_percent": pct,
        "availability_status": status_raw,
        "availability_evidence": "; ".join(evidence) or f"availability: {flag}",
    }


def _finalize_capability(
    parts: dict,
    evidence: list[str],
    gaps: list[str],
    unknown: list[str],
) -> dict:
    numeric = [parts[k] for k in _SCORE_KEYS if parts.get(k) is not None]
    match_score = round(sum(numeric) / len(numeric), 2) if numeric else 0.0
    confidence = "INFERRED" if unknown else "VERIFIED"
    why_bits = [evidence[0]] if evidence else []
    if gaps:
        why_bits.append("gaps: " + ", ".join(gaps))
    if unknown:
        why_bits.append("unknown: " + ", ".join(unknown))
    if gaps or unknown:
        status = "PARTIAL"
    else:
        status = "SATISFIED"
    return {
        "match_score": match_score,
        "why": "; ".join(why_bits),
        "evidence": evidence,
        "gaps": gaps,
        "unknown": unknown,
        "factors": parts,
        "confidence": confidence,
        "status": status,
    }


def score_capability_match(requirement: dict, match: dict) -> dict:
    """Semantic similarity plus explicit geography/sector/language/years/skills/availability.

    Never infers education, certifications, sector, or availability that are
    not present on the CV metadata or a live Airtable availability field.
    """
    requirement = requirement or {}
    match = match or {}
    meta = match.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    if not isinstance(meta, dict):
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

    geo_val, geo_ev, geo_gaps, geo_unk = _explicit_overlay(
        _as_list(requirement.get("required_geographic_experience")),
        _as_list(meta.get("geographic_experience")),
        factor="geography",
        missing_meta="geographic_experience not on CV metadata",
    )
    parts["geography"] = geo_val
    evidence.extend(geo_ev)
    gaps.extend(geo_gaps)
    unknown.extend(geo_unk)

    sector_val, sector_ev, sector_gaps, sector_unk = _explicit_overlay(
        _as_list(
            requirement.get("required_thematic_areas")
            or requirement.get("thematic_areas")
            or requirement.get("sector")
        ),
        _as_list(meta.get("thematic_expertise") or meta.get("thematic_areas")),
        factor="sector",
        missing_meta="thematic_expertise not on CV metadata",
    )
    parts["sector"] = sector_val
    evidence.extend(sector_ev)
    gaps.extend(sector_gaps)
    unknown.extend(sector_unk)

    lang_val, lang_ev, lang_gaps, lang_unk = _explicit_overlay(
        _as_list(
            requirement.get("required_languages")
            or requirement.get("language_requirements")
        ),
        _as_list(meta.get("languages")),
        factor="language",
        missing_meta="languages not on CV metadata",
    )
    parts["language"] = lang_val
    evidence.extend(lang_ev)
    gaps.extend(lang_gaps)
    unknown.extend(lang_unk)

    skill_val, skill_ev, skill_gaps, skill_unk = _explicit_overlay(
        _as_list(requirement.get("required_skills")),
        _as_list(meta.get("key_skills")) + _as_list(meta.get("tools")),
        factor="skill",
        missing_meta="key_skills/tools not on CV metadata",
    )
    parts["skills"] = skill_val
    evidence.extend(skill_ev)
    gaps.extend(skill_gaps)
    unknown.extend(skill_unk)

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
    else:
        parts["years"] = None

    education = requirement.get("required_education")
    if education:
        # Education is not stored on cv_embeddings.metadata — do not infer it.
        parts["education"] = None
        unknown.append("education not in CV metadata — not inferred")
        gaps.append("education:UNKNOWN")

    flag = match.get("availability_flag")
    if flag in _AVAIL_SCORE:
        parts["availability"] = _AVAIL_SCORE[flag]
        evidence.append(match.get("availability_evidence") or f"availability: {flag}")
        if flag == "Busy":
            gaps.append("availability:Busy")
        elif flag == "Partially":
            gaps.append("availability:Partial")
    elif flag == "Unknown" or match.get("availability_evidence"):
        parts["availability"] = None
        unknown.append(
            match.get("availability_evidence")
            or "availability not on Airtable record — not inferred"
        )
    else:
        parts["availability"] = None

    return _finalize_capability(parts, evidence, gaps, unknown)


def filter_by_availability(matches: list[dict]) -> list[dict]:
    """Annotate matches with live Airtable availability. Never drop a match.

    Missing availability stays Unknown — it is not treated as 100% free.
    One table.all() — not a get() per consultant — so a 429 cannot
    serialize the pipeline for tens of minutes.
    """
    consultants = get_all_consultants()
    by_id = {c.get("id"): c for c in consultants}
    for match in matches:
        airtable_id = match.get("airtable_consultant_id") or match.get("airtable_id")
        consultant = by_id.get(airtable_id) if airtable_id else None
        parsed = parse_availability(consultant)
        match.update(parsed)
        if consultant and "current_projects" in consultant:
            match["current_project_count"] = consultant.get("current_projects")
        if (match.get("consultant_name") or "") in {
            "EXTERNAL RECRUITMENT NEEDED",
            "CV SEARCH UNAVAILABLE",
        }:
            continue
        req = match.get("requirement")
        if req is not None or match.get("capability") is not None:
            match["capability"] = score_capability_match(req or {}, match)
    return sorted(
        matches,
        key=lambda m: _AVAIL_RANK.get(m.get("availability_flag"), 1),
        reverse=True,
    )


def _team_capability_summary(matched_team: dict, gaps: list) -> dict:
    scores = []
    evidence = []
    all_gaps = [str(g) for g in gaps]
    unknown = []
    for role, match in (matched_team or {}).items():
        if not isinstance(match, dict):
            continue
        cap = match.get("capability") or {}
        name = match.get("consultant_name") or ""
        if name != "EXTERNAL RECRUITMENT NEEDED" and cap.get("match_score") is not None:
            scores.append(float(cap["match_score"]))
        evidence.extend(f"{role}: {item}" for item in (cap.get("evidence") or [])[:2])
        all_gaps.extend(str(g) for g in (cap.get("gaps") or []))
        unknown.extend(str(u) for u in (cap.get("unknown") or []))
    unavailable = any(
        isinstance(match, dict)
        and (match.get("consultant_name") or "") == "CV SEARCH UNAVAILABLE"
        for match in (matched_team or {}).values()
    )
    match_score = round(sum(scores) / len(scores), 2) if scores else 0.0
    if unavailable:
        status = "UNKNOWN"
    elif gaps and not scores:
        status = "MISSING"
    elif gaps or unknown:
        status = "PARTIAL"
    else:
        status = "SATISFIED"
    return {
        "match_score": match_score,
        "why": (
            f"{len(scores)} role(s) scored from CV evidence; "
            f"{len(gaps)} unmatched role(s)"
        ),
        "evidence": evidence,
        "gaps": all_gaps,
        "unknown": unknown,
        "status": status,
    }


def match_team_to_requirements(
    team_requirements: list[dict],
    opportunity_id: str = None,
    opportunity_title: str = "",
    opportunity_context: dict | None = None,
) -> dict:
    """
    For each required role in the ToR, find the best matching
    Cortech consultant using semantic search plus explicit overlays.
    """
    matched_team = {}
    gaps = []
    search_unavailable = False

    title_label = (opportunity_title or "")[:80]
    logger.info(
        f"Matching team for {len(team_requirements or [])} roles"
        + (f": {title_label}" if title_label else "")
    )

    def _unavailable_slot(role_name: str, requirement: dict) -> dict:
        return {
            "consultant_name": "CV SEARCH UNAVAILABLE",
            "airtable_id": None,
            "similarity_score": 0,
            "requirement": requirement,
            "capability": {
                "match_score": None,
                "why": "OpenAI embeddings failed — team match was not run",
                "evidence": [],
                "gaps": [],
                "unknown": ["cv_embeddings"],
                "confidence": "UNKNOWN",
                "status": "UNKNOWN",
            },
        }

    for req in team_requirements or []:
        if not isinstance(req, dict):
            continue
        req = merge_requirement_context(req, opportunity_context)
        role = req.get("role") or "Unknown"
        level = req.get("level") or "Senior"
        skills = req.get("required_skills") or []
        geo = req.get("required_geographic_experience") or []
        themes = (
            req.get("required_thematic_areas")
            or req.get("thematic_areas")
            or req.get("sector")
            or []
        )
        langs = req.get("required_languages") or req.get("language_requirements") or []
        if isinstance(skills, str):
            skills = [skills]
        if isinstance(geo, str):
            geo = [geo]
        if isinstance(themes, str):
            themes = [themes]
        if isinstance(langs, str):
            langs = [langs]

        search_query = f"""
        {level} {role}
        Skills required: {', '.join(str(s) for s in skills)}
        Thematic / sector: {', '.join(str(t) for t in themes)}
        Geographic experience needed: {', '.join(str(g) for g in geo)}
        Languages: {', '.join(str(lang) for lang in langs)}
        Years experience: {req.get('years_experience_minimum') or 0}+
        Education: {req.get('required_education') or ''}
        """

        if search_unavailable:
            matched_team[role] = _unavailable_slot(role, req)
            continue

        try:
            matches = search_consultants(
                query_text=search_query,
                match_threshold=0.60,
                match_count=3
            )
        except EmbeddingError as e:
            search_unavailable = True
            logger.error(
                f"  CV search unavailable for remaining roles ({e}) — "
                "not treating this as a staffing gap"
            )
            matched_team[role] = _unavailable_slot(role, req)
            continue

        if matches:
            best_match = matches[0] if isinstance(matches[0], dict) else {}
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
                "airtable_consultant_id": best_match.get("airtable_consultant_id"),
                "role_title": best_match.get("role_title") or "",
                "similarity_score": round(similarity * 100),
                "metadata": best_match.get("metadata") or {},
                "all_matches": matches,
                "requirement": req,
                "capability": score_capability_match(req, best_match),
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
                    "unknown": [],
                    "confidence": "VERIFIED",
                    "status": "MISSING",
                },
            }
            logger.warning(f"  {role} → NO MATCH FOUND — External recruitment needed")

    if matched_team:
        for role, match in matched_team.items():
            match["_sort_role"] = role
        ranked = filter_by_availability(list(matched_team.values()))
        matched_team = {item.pop("_sort_role"): item for item in ranked}

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

    if search_unavailable:
        coverage = None
    else:
        coverage = round(
            (len(matched_team) - len(gaps)) / len(matched_team) * 100
            if matched_team else 0
        )
    return {
        "matched_team": matched_team,
        "gaps": gaps,
        "coverage_percent": coverage,
        "search_unavailable": search_unavailable,
        "capability_summary": _team_capability_summary(matched_team, gaps),
    }
