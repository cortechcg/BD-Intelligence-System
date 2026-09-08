from intelligence import budget_calculator


def _analysis(requirements):
    return {
        "opportunity": {"estimated_budget_usd": 100000},
        "team_requirements": requirements,
    }


def test_budget_uses_only_explicit_effort_and_rate_card(monkeypatch):
    monkeypatch.setattr(
        budget_calculator,
        "get_rate_card",
        lambda: [
            {"role_level": "Senior", "location": "Somalia", "day_rate_usd": 900},
            {"role_level": "Mid", "location": "Somalia", "day_rate_usd": 500},
        ],
    )

    budget = budget_calculator.calculate_budget(
        _analysis([
            {"role": "Team Lead", "level": "Senior", "estimated_days_of_effort": 12},
            {"role": "Analyst", "level": "Mid", "estimated_days_of_effort": 8},
        ]),
        matched_team={},
        primary_location="Somalia",
    )

    assert budget["status"] == budget_calculator.BUDGET_PARTIAL
    assert budget["summary"]["personnel_subtotal_usd"] == 14800
    assert budget["summary"]["known_personnel_subtotal_usd"] == 14800
    assert budget["summary"]["grand_total_usd"] is None
    assert budget["personnel_breakdown"]["Team Lead"]["personnel_cost_usd"] == 10800
    assert budget["personnel_breakdown"]["Analyst"]["personnel_cost_usd"] == 4000
    assert "travel/logistics assumptions or quotations" in budget["missing_inputs"]


def test_budget_does_not_invent_effort_or_default_day_rates(monkeypatch):
    monkeypatch.setattr(budget_calculator, "get_rate_card", lambda: [])

    budget = budget_calculator.calculate_budget(
        _analysis([
            {"role": "Team Lead", "level": "Senior", "estimated_days_of_effort": None},
        ]),
        matched_team={},
        primary_location="Nairobi",
    )

    line = budget["personnel_breakdown"]["Team Lead"]
    assert budget["status"] == budget_calculator.BUDGET_INSUFFICIENT
    assert line["day_rate_usd"] is None
    assert line["personnel_cost_usd"] is None
    assert budget["summary"]["grand_total_usd"] is None
    assert any("estimated_days_of_effort" in item for item in budget["missing_inputs"])
    assert any("rate-card day_rate_usd" in item for item in budget["missing_inputs"])


def test_workshop_compatibility_api_refuses_to_guess_costs():
    result = budget_calculator.estimate_workshop_costs([], "Nairobi")
    assert result["status"] == budget_calculator.BUDGET_INSUFFICIENT
    assert result["total"] is None


def test_budget_survives_missing_analysis_fields_and_rate_card_errors(monkeypatch):
    monkeypatch.setattr(
        budget_calculator,
        "get_rate_card",
        lambda: (_ for _ in ()).throw(RuntimeError("429")),
    )

    empty = budget_calculator.calculate_budget(None, None, "Nairobi")
    assert empty["status"] == budget_calculator.BUDGET_INSUFFICIENT
    assert empty["summary"]["grand_total_usd"] is None

    partial = budget_calculator.calculate_budget(
        {"team_requirements": [{"role": "Lead"}]},
        {},
        "Nairobi",
    )
    assert partial["status"] == budget_calculator.BUDGET_INSUFFICIENT
    assert partial["personnel_breakdown"]["Lead"]["day_rate_usd"] is None
    assert partial["summary"]["grand_total_usd"] is None
    assert partial["missing_inputs"]
