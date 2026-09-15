"""Deterministic bid scoring. LLM extracts signals; this module calculates scores.

The analyzer may still return cortech_fit_score / win_probability /
bid_recommendation. Those are stored as llm_* audit fields. Final numbers
and BID/WATCH/NO-BID come from this file + scoring_model.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from utils.dates import days_until, parse_deadline

_MODEL_PATH = Path(__file__).with_name("scoring_model.json")
_MODEL_CACHE: dict | None = None

STATUS_VERIFIED = "VERIFIED"
STATUS_INFERRED = "INFERRED"
STATUS_UNKNOWN = "UNKNOWN"
INSUFFICIENT = "INSUFFICIENT DATA"


def load_scoring_model() -> dict:
    global _MODEL_CACHE
    if _MODEL_CACHE is None:
        _MODEL_CACHE = json.loads(_MODEL_PATH.read_text(encoding="utf-8"))
    return _MODEL_CACHE


def scoring_model_version() -> str:
    return str(load_scoring_model().get("score_version") or "unknown")


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _norm_list(values) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    out = []
    for item in values:
        text = str(item or "").strip().lower()
        if text:
            out.append(text)
    return out


def _overlap_score(needles: list[str], haystack: str) -> tuple[float, list[str]]:
    if not needles or not haystack:
        return 0.0, []
    hits = [n for n in needles if n and n in haystack]
    if not hits:
        return 0.0, []
    return _clamp(100.0 * len(hits) / max(len(needles), 1)), hits


def _factor(name: str, value: Optional[float], status: str, evidence: str, **extra) -> dict:
    payload = {
        "name": name,
        "value": None if value is None else round(float(value), 2),
        "status": status,
        "evidence": evidence or "",
    }
    payload.update(extra)
    return payload


def _weighted_mean(factors: list[dict], weights: dict[str, float]) -> tuple[Optional[float], float]:
    """Average of factors that are not UNKNOWN. Returns (score, coverage 0-1)."""
    usable = []
    total_w = 0.0
    for factor in factors:
        name = factor["name"]
        if name not in weights:
            continue
        if factor["status"] == STATUS_UNKNOWN or factor["value"] is None:
            continue
        w = float(weights[name])
        usable.append((factor["value"], w))
        total_w += w
    if not usable or total_w <= 0:
        return None, 0.0
    score = sum(v * w for v, w in usable) / total_w
    coverage = total_w / sum(weights.values()) if weights else 0.0
    return _clamp(score), coverage


def _geography_factor(locations: list[str], model: dict) -> dict:
    hay = " ".join(_norm_list(locations))
    if not hay:
        return _factor("geography", None, STATUS_UNKNOWN, "No project location extracted")

    primary = model["primary_geographies"]
    secondary = model["secondary_geographies"]
    regional = model["regional_geographies"]
    p_hits = [g for g in primary if g in hay]
    s_hits = [g for g in secondary if g in hay]
    r_hits = [g for g in regional if g in hay]

    if p_hits:
        return _factor(
            "geography", 100.0, STATUS_VERIFIED,
            f"Primary geography match: {', '.join(p_hits)}",
            hits=p_hits,
        )
    if s_hits:
        return _factor(
            "geography", 70.0, STATUS_VERIFIED,
            f"Secondary geography match: {', '.join(s_hits)}",
            hits=s_hits,
        )
    if r_hits:
        # "africa" alone is a weak signal
        score = 55.0 if any(h != "africa" for h in r_hits) else 35.0
        return _factor(
            "geography", score, STATUS_VERIFIED,
            f"Regional geography match only: {', '.join(r_hits)}",
            hits=r_hits,
        )
    return _factor(
        "geography", 10.0, STATUS_VERIFIED,
        f"Locations extracted but none in Cortech focus: {hay[:160]}",
        hits=[],
    )


def _thematic_factor(themes: list[str], model: dict) -> dict:
    hay = " ".join(_norm_list(themes))
    if not hay:
        return _factor("thematic", None, STATUS_UNKNOWN, "No thematic areas extracted")
    score, hits = _overlap_score(model["core_thematics"], hay)
    if not hits:
        return _factor(
            "thematic", 15.0, STATUS_VERIFIED,
            f"Themes extracted but none match Cortech core list: {hay[:160]}",
            hits=[],
        )
    # Overlap-over-all-core-thematics is too harsh (long list). Score by
    # whether extracted themes hit the core list.
    extracted = _norm_list(themes)
    matched = [t for t in extracted if any(c in t or t in c for c in model["core_thematics"])]
    ratio = len(matched) / max(len(extracted), 1)
    return _factor(
        "thematic", _clamp(100.0 * ratio), STATUS_VERIFIED,
        f"Matched themes: {', '.join(matched) or 'none'}",
        hits=matched,
    )


def _language_factor(required: list[str], model: dict) -> dict:
    req = _norm_list(required)
    if not req:
        return _factor(
            "language", None, STATUS_UNKNOWN,
            "No language requirements extracted — not assumed",
        )
    have = model["languages"]
    missing = [r for r in req if not any(h in r or r in h for h in have)]
    if not missing:
        return _factor(
            "language", 100.0, STATUS_VERIFIED,
            f"All required languages covered: {', '.join(req)}",
        )
    covered = [r for r in req if r not in missing]
    score = 100.0 * len(covered) / len(req)
    return _factor(
        "language", score, STATUS_VERIFIED,
        f"Covered {', '.join(covered) or 'none'}; missing {', '.join(missing)}",
        gaps=missing,
    )


def _eligibility_factor(certs: list[str], locations: list[str]) -> dict:
    """Do not invent missing credentials. Unknown certs ⇒ UNKNOWN, not a fail."""
    req = _norm_list(certs)
    if not req:
        return _factor(
            "eligibility", None, STATUS_UNKNOWN,
            "No certifications/eligibility extracted",
        )
    known_ok = ("iso", "psea", "child safeguarding", "safeguarding")
    loc_blob = " ".join(_norm_list(locations))
    known_regs = ("kenya", "somalia", "united kingdom", "uk")
    evidence = []
    unknown = []
    for item in req:
        if any(k in item for k in known_ok):
            evidence.append(f"profile lists {item}")
        elif "regist" in item and any(r in item or r in loc_blob for r in known_regs):
            evidence.append(f"registration language: {item}")
        else:
            unknown.append(item)
    if unknown and not evidence:
        return _factor(
            "eligibility", None, STATUS_UNKNOWN,
            "Required credentials not in Cortech profile — not inferred: "
            + ", ".join(unknown),
            gaps=unknown,
        )
    if unknown:
        return _factor(
            "eligibility", 60.0, STATUS_INFERRED,
            "Some stated requirements match profile; others unverified: "
            + ", ".join(unknown),
            gaps=unknown,
        )
    return _factor(
        "eligibility", 90.0, STATUS_VERIFIED,
        "; ".join(evidence) or "stated requirements match known profile items",
    )


def _team_capacity_factor(matched_team_result: Optional[dict]) -> dict:
    if not matched_team_result:
        return _factor(
            "team_capacity", None, STATUS_UNKNOWN,
            "CV matching has not run yet",
        )
    coverage = matched_team_result.get("coverage_percent")
    gaps = matched_team_result.get("gaps") or []
    if coverage is None:
        return _factor("team_capacity", None, STATUS_UNKNOWN, "No coverage_percent on match result")
    evidence = f"Role coverage {coverage}%"
    if gaps:
        evidence += f"; unmatched roles: {', '.join(str(g) for g in gaps)}"
    return _factor("team_capacity", float(coverage), STATUS_VERIFIED, evidence, gaps=list(gaps))


def _deadline_factors(deadline, urgent_days: int, soon_days: int) -> tuple[dict, dict]:
    parsed = parse_deadline(deadline)
    remaining = days_until(deadline)
    if remaining is None:
        unknown = _factor(
            "deadline_feasibility", None, STATUS_UNKNOWN,
            "Submission deadline missing or unparseable",
        )
        pressure = _factor(
            "deadline_pressure", None, STATUS_UNKNOWN,
            "Submission deadline missing or unparseable",
        )
        return unknown, pressure

    if remaining < 0:
        feas = _factor("deadline_feasibility", 0.0, STATUS_VERIFIED, f"Deadline {parsed} is in the past")
        press = _factor("deadline_pressure", 100.0, STATUS_VERIFIED, f"Deadline {parsed} is in the past")
        return feas, press
    if remaining <= urgent_days:
        feas = _factor("deadline_feasibility", 25.0, STATUS_VERIFIED, f"{remaining} days left (urgent)")
        press = _factor("deadline_pressure", 90.0, STATUS_VERIFIED, f"{remaining} days left")
        return feas, press
    if remaining <= soon_days:
        feas = _factor("deadline_feasibility", 55.0, STATUS_VERIFIED, f"{remaining} days left")
        press = _factor("deadline_pressure", 60.0, STATUS_VERIFIED, f"{remaining} days left")
        return feas, press
    feas = _factor("deadline_feasibility", 90.0, STATUS_VERIFIED, f"{remaining} days left")
    press = _factor("deadline_pressure", 20.0, STATUS_VERIFIED, f"{remaining} days left")
    return feas, press


def _known_client_factor(client: str, donor: str, model: dict) -> dict:
    blob = f"{client} {donor}".strip().lower()
    if not blob.strip():
        return _factor("known_client", None, STATUS_UNKNOWN, "No client/donor extracted")
    hits = [c for c in model["known_clients"] if c in blob]
    if hits:
        return _factor(
            "known_client", 80.0, STATUS_VERIFIED,
            f"Name appears on Cortech profile client list: {', '.join(hits)}. "
            "This is name overlap, not a verified current relationship.",
            hits=hits,
        )
    return _factor(
        "known_client", 20.0, STATUS_VERIFIED,
        "Client/donor not on the profile list — no relationship inferred",
        hits=[],
    )


def _contract_value(opportunity: dict) -> tuple[Optional[float], str]:
    raw = opportunity.get("estimated_budget_usd")
    if raw is None or raw == "" or raw == 0 or raw == "0":
        return None, STATUS_UNKNOWN
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, STATUS_UNKNOWN
    if value <= 0:
        return None, STATUS_UNKNOWN
    return value, STATUS_VERIFIED


def _recommend(fit: Optional[float], eligibility: dict, model: dict) -> str:
    rules = model["recommendation"]
    if rules.get("no_bid_if_ineligible") and eligibility.get("gaps") and eligibility.get("status") == STATUS_VERIFIED and (eligibility.get("value") or 0) < 40:
        return "NO-BID"
    if fit is None:
        return "WATCH"
    if fit >= rules["bid_min_fit"]:
        return "BID"
    if fit >= rules["watch_min_fit"]:
        return "WATCH"
    return "NO-BID"


def _confidence(factors: list[dict]) -> dict:
    n = len(factors) or 1
    counts = {STATUS_VERIFIED: 0, STATUS_INFERRED: 0, STATUS_UNKNOWN: 0}
    for f in factors:
        counts[f.get("status", STATUS_UNKNOWN)] = counts.get(f.get("status"), 0) + 1
    # 0-1: verified weighted 1, inferred 0.4, unknown 0
    score = (counts[STATUS_VERIFIED] + 0.4 * counts[STATUS_INFERRED]) / n
    return {
        "score": round(score, 3),
        "verified_factors": counts[STATUS_VERIFIED],
        "inferred_factors": counts[STATUS_INFERRED],
        "unknown_factors": counts[STATUS_UNKNOWN],
        "status": STATUS_VERIFIED if score >= 0.7 else STATUS_INFERRED if score >= 0.3 else STATUS_UNKNOWN,
    }


def compute_bid_intelligence(
    analysis: dict,
    matched_team_result: Optional[dict] = None,
) -> dict:
    """Pure function. Does not call an LLM. Missing inputs → UNKNOWN / INSUFFICIENT DATA."""
    from config import SOON_DEADLINE_DAYS, URGENT_DEADLINE_DAYS

    if not isinstance(analysis, dict):
        analysis = {}
    if matched_team_result is not None and not isinstance(matched_team_result, dict):
        matched_team_result = None

    model = load_scoring_model()
    opportunity = analysis.get("opportunity") if isinstance(analysis.get("opportunity"), dict) else {}
    requirements = analysis.get("requirements") if isinstance(analysis.get("requirements"), dict) else {}
    bid_analysis = analysis.get("bid_analysis") if isinstance(analysis.get("bid_analysis"), dict) else {}

    locations = (
        opportunity.get("project_location")
        or requirements.get("geographic_experience")
        or []
    )
    themes = requirements.get("thematic_areas") or []
    languages = requirements.get("language_requirements") or []
    certs = requirements.get("certifications") or []
    client = opportunity.get("client") or ""
    donor = opportunity.get("donor") or ""
    deadline = opportunity.get("submission_deadline")

    geo = _geography_factor(locations if isinstance(locations, list) else [locations], model)
    thematic = _thematic_factor(themes if isinstance(themes, list) else [themes], model)
    language = _language_factor(languages if isinstance(languages, list) else [languages], model)
    eligibility = _eligibility_factor(certs if isinstance(certs, list) else [certs], locations if isinstance(locations, list) else [locations])
    team = _team_capacity_factor(matched_team_result)
    deadline_feas, deadline_pressure = _deadline_factors(deadline, URGENT_DEADLINE_DAYS, SOON_DEADLINE_DAYS)
    known_client = _known_client_factor(client, donor, model)

    fit_factors = [geo, thematic, language, eligibility, team]
    fit_score, fit_cov = _weighted_mean(fit_factors, model["weights"]["fit"])

    win_factors = [
        _factor("fit", fit_score, STATUS_VERIFIED if fit_score is not None else STATUS_UNKNOWN, "Deterministic fit dimension"),
        deadline_feas,
        known_client,
        eligibility,
    ]
    win_score, win_cov = _weighted_mean(win_factors, model["weights"]["win_probability"])

    # Strategic pieces reuse geography/thematic/client with dedicated names
    core_thematic = _factor(
        "core_thematic",
        thematic["value"],
        thematic["status"],
        thematic["evidence"],
    )
    priority_geo = _factor(
        "priority_geography",
        geo["value"],
        geo["status"],
        geo["evidence"],
    )
    strategic_factors = [core_thematic, priority_geo, known_client]
    strategic_score, _ = _weighted_mean(strategic_factors, model["weights"]["strategic_value"])

    team_gap_score = None
    team_gap_status = STATUS_UNKNOWN
    team_gap_evidence = "Team matching not available"
    if team["value"] is not None:
        team_gap_score = _clamp(100.0 - float(team["value"]))
        team_gap_status = STATUS_VERIFIED
        team_gap_evidence = team["evidence"]
    team_gaps_factor = _factor("team_gaps", team_gap_score, team_gap_status, team_gap_evidence)

    elig_gap_score = None
    elig_gap_status = STATUS_UNKNOWN
    if eligibility["value"] is not None:
        elig_gap_score = _clamp(100.0 - float(eligibility["value"]))
        elig_gap_status = eligibility["status"]
    elig_gaps_factor = _factor(
        "eligibility_gaps", elig_gap_score, elig_gap_status, eligibility["evidence"],
        gaps=eligibility.get("gaps") or [],
    )

    risk_factors = [deadline_pressure, team_gaps_factor, elig_gaps_factor]
    risk_score, _ = _weighted_mean(risk_factors, model["weights"]["risk"])

    contract_value, cv_status = _contract_value(opportunity)
    commercial = {
        "contract_value_usd": contract_value,
        "status": cv_status if contract_value is not None else STATUS_UNKNOWN,
        "evidence": (
            f"extracted estimated_budget_usd={contract_value}"
            if contract_value is not None
            else "No usable contract value extracted — not guessed"
        ),
        "within_typical_range": None,
    }
    if contract_value is not None:
        lo = model["typical_budget_usd"]["min"]
        hi = model["typical_budget_usd"]["max"]
        commercial["within_typical_range"] = lo <= contract_value <= hi

    # Pursuit cost is not known at analysis time. Do not invent it.
    expected_value = INSUFFICIENT
    ev_status = STATUS_UNKNOWN
    if contract_value is not None and win_score is not None:
        expected_value = INSUFFICIENT
        ev_status = STATUS_UNKNOWN
        ev_reason = "P(win)×value known but pursuit_cost and risk_adjustment USD are not — EV not guessed"
    else:
        ev_reason = "Need P(win) and contract_value and pursuit_cost — at least one is missing"

    recommendation = _recommend(fit_score, eligibility, model)
    llm_rec = bid_analysis.get("bid_recommendation")
    llm_fit = bid_analysis.get("cortech_fit_score")
    llm_win = bid_analysis.get("win_probability")

    all_factors = fit_factors + [deadline_feas, deadline_pressure, known_client, team_gaps_factor]
    confidence = _confidence(all_factors)

    why = []
    if fit_score is not None:
        why.append(f"Fit {round(fit_score)}/100 (weights {model['score_version']}).")
    else:
        why.append("Fit INSUFFICIENT DATA — too many UNKNOWN factors.")
    why.append(geo["evidence"])
    why.append(thematic["evidence"])
    if known_client["status"] == STATUS_VERIFIED:
        why.append(known_client["evidence"])

    risks = [f["evidence"] for f in risk_factors if f["value"] is not None and f["value"] >= 50]
    gaps = []
    for f in (language, eligibility, team):
        for g in f.get("gaps") or []:
            gaps.append(str(g))
    gaps.extend(bid_analysis.get("key_gaps") or [])

    result = {
        "score_version": model["score_version"],
        "weights": model["weights"],
        "factor_values": {f["name"]: f["value"] for f in all_factors},
        "factor_evidence": all_factors,
        "fit": {
            "score": None if fit_score is None else round(fit_score, 2),
            "coverage": round(fit_cov, 3),
            "status": STATUS_VERIFIED if fit_score is not None else STATUS_UNKNOWN,
        },
        "win_probability": {
            "score": None if win_score is None else round(win_score, 2),
            "coverage": round(win_cov, 3),
            "status": STATUS_VERIFIED if win_score is not None else STATUS_UNKNOWN,
            "unit": "percent",
        },
        "commercial_value": commercial,
        "strategic_value": {
            "score": None if strategic_score is None else round(strategic_score, 2),
            "status": STATUS_VERIFIED if strategic_score is not None else STATUS_UNKNOWN,
        },
        "risk": {
            "score": None if risk_score is None else round(risk_score, 2),
            "status": STATUS_VERIFIED if risk_score is not None else STATUS_UNKNOWN,
            "unit": "0-100 higher=riskier",
        },
        "expected_value": {
            "value": expected_value,
            "status": ev_status,
            "reason": ev_reason,
            "formula": "P(win)×contract_value − pursuit_cost − risk_adjustment",
        },
        "recommendation": recommendation,
        "why": " ".join(w for w in why if w),
        "risks": risks or ["No quantified risk factors above threshold — remaining risks may still be UNKNOWN."],
        "gaps": gaps,
        "confidence": confidence,
        "llm_audit": {
            "cortech_fit_score": llm_fit,
            "win_probability": llm_win,
            "bid_recommendation": llm_rec,
            "note": "LLM numbers are audit-only. They are not the final score.",
        },
    }
    # Audit field only. Never replaces heuristic win_probability / recommendation.
    try:
        from intelligence.win_calibration import attach_calibrated_win_probability

        attach_calibrated_win_probability(result)
    except Exception:
        result["calibrated_win_probability"] = {
            "value": None,
            "status": INSUFFICIENT,
            "official": False,
            "official_win_probability_source": "bid_scorer_heuristic",
            "note": (
                "Calibration harness failed open. "
                "Heuristic WIN PROBABILITY remains official."
            ),
        }
    return result



def apply_bid_intelligence(analysis: dict, matched_team_result: Optional[dict] = None) -> dict:
    """Mutate analysis so downstream gates read deterministic scores.

    Preserves is_consultancy_contract and submission_type.
    Stashes LLM numbers under bid_analysis.llm_*.
    """
    if not isinstance(analysis, dict):
        analysis = {}
    bid = dict(analysis.get("bid_analysis") if isinstance(analysis.get("bid_analysis"), dict) else {})
    intelligence = compute_bid_intelligence(analysis, matched_team_result)

    bid["llm_cortech_fit_score"] = bid.get("cortech_fit_score")
    bid["llm_win_probability"] = bid.get("win_probability")
    bid["llm_bid_recommendation"] = bid.get("bid_recommendation")

    fit = intelligence["fit"]["score"]
    win = intelligence["win_probability"]["score"]
    bid["cortech_fit_score"] = 0 if fit is None else round(fit)
    bid["win_probability"] = 0 if win is None else round(win)
    bid["bid_recommendation"] = intelligence["recommendation"]
    bid["score_version"] = intelligence["score_version"]
    bid["bid_intelligence"] = intelligence
    # Default consultancy TRUE is the caller's job when the key is missing;
    # we never overwrite a provided boolean here.
    analysis["bid_analysis"] = bid
    analysis["bid_intelligence"] = intelligence
    return analysis
