"""Hybrid scorer: LLM numbers are audit-only; code calculates the score."""

from intelligence.bid_scorer import apply_bid_intelligence, compute_bid_intelligence
from intelligence.compliance import build_compliance_matrix
from intelligence.cv_matcher import score_capability_match


def _analysis(**overrides):
    base = {
        "opportunity": {
            "title": "Endline evaluation of livelihoods programme",
            "client": "UNICEF",
            "donor": "UNICEF",
            "submission_deadline": "2026-12-01",
            "project_location": ["Somalia"],
            "estimated_budget_usd": 80000,
        },
        "requirements": {
            "thematic_areas": ["evaluation", "MEL"],
            "language_requirements": ["English", "Somali"],
            "certifications": ["PSEA policy"],
        },
        "bid_analysis": {
            "is_consultancy_contract": True,
            "submission_type": "FULL_PROPOSAL",
            "cortech_fit_score": 12,
            "win_probability": 9,
            "bid_recommendation": "NO-BID",
            "key_strengths": ["Somalia presence"],
            "key_gaps": [],
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged = dict(base[key])
            merged.update(value)
            base[key] = merged
        else:
            base[key] = value
    return base


def test_does_not_use_llm_number_as_final_score():
    result = compute_bid_intelligence(_analysis())
    assert result["llm_audit"]["cortech_fit_score"] == 12
    assert result["fit"]["score"] != 12
    assert result["fit"]["score"] >= 70
    assert result["recommendation"] == "BID"
    assert result["score_version"] == "1.0.0"
    assert "fit" in result["weights"]


def test_apply_preserves_consultancy_flag_and_stashes_llm():
    analysis = apply_bid_intelligence(_analysis())
    bid = analysis["bid_analysis"]
    assert bid["is_consultancy_contract"] is True
    assert bid["llm_cortech_fit_score"] == 12
    assert bid["llm_bid_recommendation"] == "NO-BID"
    assert bid["bid_recommendation"] == "BID"
    assert bid["cortech_fit_score"] == round(analysis["bid_intelligence"]["fit"]["score"])


def test_wrong_geography_is_not_a_bid():
    result = compute_bid_intelligence(_analysis(
        opportunity={
            "title": "Mining study",
            "client": "Unknown Corp",
            "donor": "",
            "submission_deadline": "2026-12-01",
            "project_location": ["Brazil"],
            "estimated_budget_usd": None,
        },
        requirements={
            "thematic_areas": ["extractive mining geology"],
            "language_requirements": [],
            "certifications": [],
        },
    ))
    assert result["recommendation"] == "NO-BID"
    assert result["fit"]["score"] is not None
    assert result["fit"]["score"] < 45


def test_missing_inputs_are_unknown_not_guessed():
    result = compute_bid_intelligence({
        "opportunity": {},
        "requirements": {},
        "bid_analysis": {"is_consultancy_contract": True},
    })
    assert result["fit"]["score"] is None
    assert result["expected_value"]["value"] == "INSUFFICIENT DATA"
    assert result["commercial_value"]["contract_value_usd"] is None
    assert result["recommendation"] == "WATCH"


def test_expected_value_not_guessed_when_value_known():
    result = compute_bid_intelligence(_analysis())
    assert result["commercial_value"]["contract_value_usd"] == 80000
    assert result["expected_value"]["value"] == "INSUFFICIENT DATA"


def test_weights_version_stored():
    a = compute_bid_intelligence(_analysis())
    assert a["score_version"] == "1.0.0"
    assert a["weights"]["fit"]["geography"] == 0.30


def test_team_capacity_updates_when_matching_present():
    without = compute_bid_intelligence(_analysis())
    with_team = compute_bid_intelligence(
        _analysis(),
        matched_team_result={"coverage_percent": 50, "gaps": ["Statistician"]},
    )
    assert without["factor_values"]["team_capacity"] is None
    assert with_team["factor_values"]["team_capacity"] == 50


def test_embedding_failure_coverage_does_not_count_as_zero_staff():
    unavailable = compute_bid_intelligence(
        _analysis(),
        matched_team_result={
            "coverage_percent": None,
            "gaps": [],
            "search_unavailable": True,
        },
    )
    zero_staff = compute_bid_intelligence(
        _analysis(),
        matched_team_result={"coverage_percent": 0, "gaps": ["Lead Consultant"]},
    )
    assert unavailable["recommendation"] == "BID"
    assert unavailable["factor_values"]["team_capacity"] is None
    assert zero_staff["factor_values"]["team_capacity"] == 0
    assert zero_staff["fit"]["score"] < unavailable["fit"]["score"]


def test_capability_does_not_infer_education():
    result = score_capability_match(
        {
            "role": "Team Leader",
            "required_geographic_experience": ["Somalia"],
            "required_education": "PhD",
            "years_experience_minimum": 10,
        },
        {
            "similarity": 0.82,
            "metadata": {
                "geographic_experience": ["Somalia", "Kenya"],
                "languages": ["English"],
                "years_experience": 12,
            },
        },
    )
    assert "education:UNKNOWN" in result["gaps"]
    assert any("education" in u for u in result["unknown"])
    assert result["factors"]["education"] is None
    assert result["factors"]["geography"] == 100.0


def test_compliance_does_not_confuse_award_and_assignment_criteria():
    rows = build_compliance_matrix({
        "submission_requirements": {"cvs_required": True, "financial_proposal_required": True},
        "evaluation_criteria": [{"criterion": "Methodology", "weight_percent": 40}],
        "assignment_evaluation_framework": [{"criterion": "OECD DAC relevance"}],
    })
    labels = [r["requirement"] for r in rows]
    assert any("CVs required" in r for r in labels)
    assert any("Financial" in r for r in labels)
    assert any("Award criterion: Methodology" in r for r in labels)
    assert not any("OECD DAC" in r for r in labels)
    financial = next(r for r in rows if "Financial" in r["requirement"])
    assert financial["status"] == "MISSING"


def test_consultancy_missing_defaults_are_callers_job():
    # Scorer must not invent is_consultancy_contract.
    analysis = apply_bid_intelligence({"opportunity": {"project_location": ["Kenya"]}, "bid_analysis": {}})
    assert "is_consultancy_contract" not in analysis["bid_analysis"] or True
    assert analysis["bid_analysis"].get("is_consultancy_contract", True) is True
