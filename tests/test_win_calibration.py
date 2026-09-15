"""Phase 6 calibrated P(win) harness — honesty over a toy model."""

from __future__ import annotations

from intelligence.bid_scorer import apply_bid_intelligence, compute_bid_intelligence
from intelligence.win_calibration import (
    INSUFFICIENT,
    MIN_LABELED_N,
    MIN_PER_CLASS,
    OFFICIAL_SOURCE,
    SAMPLE_TOO_SMALL,
    attach_calibrated_win_probability,
    census_labeled_outcomes,
    evaluate_held_out,
    extract_model_features,
    golden_outcomes_are_unknown,
    labeled_outcome_from_fields,
    load_calibration_artifact,
    meets_calibration_bar,
    predict_calibrated_win_probability,
)
from tests.golden.loader import load_golden_set
from tests.test_bid_scorer import _analysis


def _thin_rows(n_won: int = 4, n_lost: int = 4) -> list[dict]:
    rows = []
    for i in range(n_won):
        rows.append(_labeled_row(i, won=True, prefix="thin-w"))
    for i in range(n_lost):
        rows.append(_labeled_row(i, won=False, prefix="thin-l"))
    return rows


def _labeled_row(i: int, *, won: bool, prefix: str = "row") -> dict:
    if won:
        return {
            "source_id": f"{prefix}-{i:02d}",
            "outcome": "WON",
            "fit": 88,
            "deadline_feasibility": 90,
            "known_client": 80,
            "eligibility": 90,
            "org_won": 3,
            "org_lost": 0,
            "heuristic_win": 78,
        }
    return {
        "source_id": f"{prefix}-{i:02d}",
        "outcome": "LOST",
        "fit": 22,
        "deadline_feasibility": 25,
        "known_client": 20,
        "eligibility": 30,
        "org_won": 0,
        "org_lost": 3,
        "heuristic_win": 24,
    }


def _enough_rows() -> list[dict]:
    rows = []
    for i in range(24):
        rows.append(_labeled_row(i, won=True, prefix="ok-w"))
    for i in range(24):
        rows.append(_labeled_row(i, won=False, prefix="ok-l"))
    return rows


def test_thresholds_are_conservative():
    assert MIN_LABELED_N == 30
    assert MIN_PER_CLASS == 10
    assert meets_calibration_bar(n_won=123, n_lost=0) is False
    assert meets_calibration_bar(n_won=8, n_lost=8) is False
    assert meets_calibration_bar(n_won=25, n_lost=5) is False
    assert meets_calibration_bar(n_won=20, n_lost=20) is True


def test_artifact_records_insufficient_and_is_not_a_fitted_model():
    artifact = load_calibration_artifact()
    assert artifact["fitted"] is False
    assert artifact["coefficients"] is None
    assert artifact["status"] == INSUFFICIENT
    assert artifact["official_win_probability_source"] == OFFICIAL_SOURCE
    census = artifact["census"]
    assert census["n_won"] == 123
    assert census["n_lost"] == 0
    assert census["n_labeled"] == 123
    assert artifact["held_out"]["brier"] is None
    assert SAMPLE_TOO_SMALL in (artifact["held_out"]["note"] or "")


def test_golden_set_outcomes_are_all_unknown_and_not_training_labels():
    items = load_golden_set()
    assert golden_outcomes_are_unknown(items)
    outcomes = [item["labels"]["outcome"] for item in items]
    assert outcomes.count("UNKNOWN") == 36
    assert outcomes.count("WON") == 0
    assert outcomes.count("LOST") == 0
    census = census_labeled_outcomes(
        [
            {
                "source_id": item["id"],
                "outcome": item["labels"]["outcome"],
            }
            for item in items
        ]
    )
    assert census["n_labeled"] == 0
    assert census["n_unknown"] == 36
    assert census["meets_bar"] is False


def test_won_false_is_unknown_not_lost():
    assert labeled_outcome_from_fields(won_flag=False) == ("UNKNOWN", "UNKNOWN")
    assert labeled_outcome_from_fields(won_flag=None) == ("UNKNOWN", "UNKNOWN")
    assert labeled_outcome_from_fields(won_flag=True)[0] == "WON"
    census = census_labeled_outcomes(
        [
            {"source_id": "a", "won": False},
            {"source_id": "b", "won": True},
            {"source_id": "c", "outcome": "Lost"},
        ]
    )
    assert census["n_won"] == 1
    assert census["n_lost"] == 1
    assert census["n_unknown"] == 1


def test_production_predict_is_insufficient_not_a_confident_probability():
    intel = compute_bid_intelligence(_analysis())
    field = intel["calibrated_win_probability"]
    assert field["value"] is None
    assert field["status"] == INSUFFICIENT
    assert field["official"] is False
    assert field["n_lost"] == 0
    assert field["n_won"] == 123
    assert SAMPLE_TOO_SMALL in field["reason"] or SAMPLE_TOO_SMALL in field["held_out_note"]
    assert field["value"] != 0.73
    # Heuristic WIN PROBABILITY is still the 0-100 score.
    assert intel["win_probability"]["unit"] == "percent"
    assert intel["win_probability"]["score"] is not None
    assert intel["win_probability"]["score"] > 1


def test_thin_n_does_not_emit_numeric_p_win():
    field = predict_calibrated_win_probability(
        bid_intelligence=compute_bid_intelligence(_analysis()),
        org_history={"won": 2, "lost": 1},
        labeled_rows=_thin_rows(),
    )
    assert field["value"] is None
    assert field["status"] == INSUFFICIENT
    assert field["n_labeled"] == 8
    assert field["official_win_probability_source"] == OFFICIAL_SOURCE


def test_held_out_evaluation_runs_when_labels_meet_the_bar():
    metrics = evaluate_held_out(_enough_rows())
    assert metrics["status"] == "VERIFIED"
    assert metrics["brier"] is not None
    assert 0.0 <= metrics["brier"] <= 1.0
    assert metrics["n_test"] >= 1
    assert metrics["n_won"] == 24
    assert metrics["n_lost"] == 24
    assert metrics["official_win_probability_source"] == OFFICIAL_SOURCE
    # A separable synthetic set should not be promoted over the heuristic.
    # Official source stays the bid_scorer heuristic regardless of Brier.
    assert metrics["outperforms_heuristic"] in {True, False}


def test_numeric_calibrated_value_is_not_the_official_win_score():
    intel = compute_bid_intelligence(_analysis())
    heuristic = intel["win_probability"]["score"]
    org = {"won": 2, "lost": 1}
    field = predict_calibrated_win_probability(
        bid_intelligence=intel,
        org_history=org,
        labeled_rows=_enough_rows(),
    )
    assert field["value"] is not None
    assert 0.0 <= field["value"] <= 1.0
    assert field["official"] is False
    assert field["official_win_probability_source"] == OFFICIAL_SOURCE
    applied = apply_bid_intelligence(_analysis())
    assert applied["bid_analysis"]["win_probability"] == round(heuristic)
    assert applied["bid_analysis"]["win_probability"] != field["value"]
    assert applied["bid_intelligence"]["win_probability"]["score"] == heuristic


def test_malformed_factors_fail_open_to_null():
    field = predict_calibrated_win_probability(
        bid_intelligence="not a dict",
        org_history={"won": 1, "lost": 1},
        labeled_rows=_enough_rows(),
    )
    assert field["value"] is None
    assert field["status"] == INSUFFICIENT


def test_missing_org_history_fail_open_to_null_not_crash():
    intel = compute_bid_intelligence(_analysis())
    field = predict_calibrated_win_probability(
        bid_intelligence=intel,
        org_history=None,
        labeled_rows=_enough_rows(),
    )
    assert field["value"] is None
    assert field["status"] == INSUFFICIENT
    assert extract_model_features(intel, None) is None
    assert extract_model_features(intel, "bad") is None
    wrapped = attach_calibrated_win_probability("garbage")  # type: ignore[arg-type]
    assert wrapped["calibrated_win_probability"]["value"] is None


def test_apply_does_not_replace_heuristic_win_probability():
    analysis = apply_bid_intelligence(_analysis())
    bid = analysis["bid_analysis"]
    intel = analysis["bid_intelligence"]
    assert bid["win_probability"] == round(intel["win_probability"]["score"])
    assert intel["win_probability"]["unit"] == "percent"
    assert intel["calibrated_win_probability"]["value"] is None
    assert "calibrated_win_probability" not in bid or bid.get("calibrated_win_probability") in {
        None,
        intel["calibrated_win_probability"],
    }


def test_won_only_rows_do_not_meet_the_bar():
    rows = [_labeled_row(i, won=True, prefix="only-w") for i in range(40)]
    census = census_labeled_outcomes(rows)
    assert census["n_won"] == 40
    assert census["n_lost"] == 0
    assert census["meets_bar"] is False
    metrics = evaluate_held_out(rows)
    assert metrics["brier"] is None
    assert metrics["status"] == INSUFFICIENT
    assert SAMPLE_TOO_SMALL in metrics["note"]
