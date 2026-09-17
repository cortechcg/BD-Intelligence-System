"""Golden-set scoring harness — offline, no network.

Constructs analysis dicts from human labels (plus recorded fixtures where
needed) and runs ``bid_scorer.compute_bid_intelligence``.
"""

from __future__ import annotations

from intelligence.bid_scorer import apply_bid_intelligence, compute_bid_intelligence
from tests.golden.loader import (
    analysis_from_labels,
    load_golden_set,
)
from tests.golden.metrics import coerce_number, format_rate, mae, null_agreement


def compute_scoring_metrics(items: list[dict]) -> dict:
    rec_pairs = []
    budget_pairs = []
    budget_null_pairs = []
    ev_insufficient = 0
    official_differs_from_llm = 0
    skipped = []
    crashed = []

    for item in items:
        labels = item["labels"]
        try:
            result = compute_bid_intelligence(analysis_from_labels(item))
        except Exception as exc:  # noqa: BLE001 — harness must record crashes
            crashed.append({"id": item["id"], "error": type(exc).__name__})
            continue

        expected = item.get("expected_recommendation")
        got = result.get("recommendation")
        if expected is None:
            skipped.append({"id": item["id"], "reason": "expected_recommendation is null"})
        else:
            rec_pairs.append((got, expected))

        pred_budget = result.get("commercial_value", {}).get("contract_value_usd")
        gold_budget = coerce_number(labels.get("budget_usd"))
        budget_null_pairs.append((pred_budget, gold_budget))
        if pred_budget is not None and gold_budget is not None:
            budget_pairs.append((float(pred_budget), gold_budget))

        if result.get("expected_value", {}).get("value") == "INSUFFICIENT DATA":
            ev_insufficient += 1

        llm_fit = result.get("llm_audit", {}).get("cortech_fit_score")
        official = result.get("fit", {}).get("score")
        if official is not None and llm_fit is not None and official != llm_fit:
            official_differs_from_llm += 1

    scored = len(rec_pairs)
    rec_acc = (
        sum(p == y for p, y in rec_pairs) / scored if scored else None
    )
    n_run = len(items) - len(crashed)
    return {
        "n_items": len(items),
        "n_recommendation_scored": scored,
        "n_skipped_recommendation": len(skipped),
        "n_crashed": len(crashed),
        "crashed": crashed,
        "recommendation_accuracy": rec_acc,
        "budget_mae": mae(budget_pairs),
        "budget_numeric_n": len(budget_pairs),
        "budget_null_agreement": null_agreement(budget_null_pairs),
        "ev_insufficient_rate": (ev_insufficient / n_run) if n_run else None,
        "official_fit_differs_from_llm": official_differs_from_llm,
        "what_this_measures": (
            "Deterministic scorer on analysis dicts built from golden labels. "
            "Anonymized client names do not overlap the profile list, so "
            "known_client is systematically 'not on list'. Vacancies skip "
            "recommendation checks because the boolean gate — not FIT — "
            "is what should stop them. Live win/loss calibration is not measured."
        ),
    }


def test_scoring_metrics_against_labeled_expected(capsys):
    items = load_golden_set()
    metrics = compute_scoring_metrics(items)
    print("\nGOLDEN SCORING (offline, labels → bid_scorer)")
    print(
        f"  n={metrics['n_items']} rec_scored={metrics['n_recommendation_scored']} "
        f"rec_skipped={metrics['n_skipped_recommendation']} crashed={metrics['n_crashed']}"
    )
    print(f"  recommendation accuracy={format_rate(metrics['recommendation_accuracy'])}")
    print(
        f"  commercial MAE={format_rate(metrics['budget_mae'])} "
        f"(n_numeric={metrics['budget_numeric_n']}) "
        f"null_agree={format_rate(metrics['budget_null_agreement'])}"
    )
    print(f"  EV INSUFFICIENT DATA rate={format_rate(metrics['ev_insufficient_rate'])}")
    print(
        f"  official FIT differs from adversarial LLM audit "
        f"on {metrics['official_fit_differs_from_llm']} items"
    )
    print(f"  note: {metrics['what_this_measures']}")
    captured = capsys.readouterr()
    assert "GOLDEN SCORING" in captured.out

    assert metrics["n_crashed"] == 0
    assert metrics["recommendation_accuracy"] == 1.0
    assert metrics["budget_mae"] == 0.0
    assert metrics["budget_null_agreement"] == 1.0
    assert metrics["ev_insufficient_rate"] == 1.0
    assert metrics["official_fit_differs_from_llm"] >= 1


def test_vacancy_items_do_not_assert_recommendation():
    vacancies = [
        item for item in load_golden_set() if item["source_kind"] == "synthetic_vacancy"
    ]
    assert vacancies
    assert all(item.get("expected_recommendation") is None for item in vacancies)
    for item in vacancies:
        result = compute_bid_intelligence(analysis_from_labels(item))
        assert result["recommendation"] in {"BID", "WATCH", "NO-BID"}


def test_apply_preserves_false_consultancy_flag():
    items = {item["id"]: item for item in load_golden_set()}
    analysis = apply_bid_intelligence(analysis_from_labels(items["g-030"]))
    assert analysis["bid_analysis"]["is_consultancy_contract"] is False
    assert analysis["bid_analysis"]["llm_bid_recommendation"] == "NO-BID"


def test_empty_set_scoring_metrics_do_not_crash():
    metrics = compute_scoring_metrics([])
    assert metrics["n_items"] == 0
    assert metrics["n_crashed"] == 0
    assert metrics["recommendation_accuracy"] is None


def test_garbage_analysis_does_not_blow_up_scorer():
    for garbage in (None, "not a dict", [1, 2], 42, object(), {"opportunity": "x"}):
        result = compute_bid_intelligence(garbage)  # type: ignore[arg-type]
        assert result["expected_value"]["value"] == "INSUFFICIENT DATA"
        assert result["recommendation"] in {"BID", "WATCH", "NO-BID"}
        assert result["fit"]["score"] is None or isinstance(result["fit"]["score"], float)

    applied = apply_bid_intelligence("garbage")  # type: ignore[arg-type]
    assert applied["bid_analysis"]["bid_recommendation"] in {"BID", "WATCH", "NO-BID"}


def test_missing_budget_stays_unknown_not_zero():
    items = {item["id"]: item for item in load_golden_set()}
    result = compute_bid_intelligence(analysis_from_labels(items["g-001"]))
    assert result["commercial_value"]["contract_value_usd"] is None
    assert result["commercial_value"]["status"] == "UNKNOWN"


def test_internal_audit_item_is_watch_not_forced_bid():
    items = {item["id"]: item for item in load_golden_set()}
    result = compute_bid_intelligence(analysis_from_labels(items["g-025"]))
    assert result["recommendation"] == "WATCH"
    assert result["fit"]["score"] is not None
    assert 45 <= result["fit"]["score"] < 70


def test_wrong_geography_item_stays_no_bid_even_with_stated_budget():
    items = {item["id"]: item for item in load_golden_set()}
    analysis = analysis_from_labels(items["g-028"])
    assert analysis["opportunity"]["estimated_budget_usd"] == 80000
    result = compute_bid_intelligence(analysis)
    assert result["recommendation"] == "NO-BID"
    assert result["commercial_value"]["contract_value_usd"] == 80000
