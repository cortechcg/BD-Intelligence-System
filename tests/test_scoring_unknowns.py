"""Scoring 1.1.0: an UNKNOWN factor is a penalty, and it cannot flip the call.

The incident: the same tender scored 59 in one run and 77 in the next. In the
1.0.0 scorer an UNKNOWN factor was dropped and the remaining weights were
renormalised, so the score depended on which facts the extractor happened to
find. These tests pin the replacement rules.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from intelligence import bid_scorer as bs

MODEL = json.loads(Path("intelligence/scoring_model.json").read_text())
FIT_W = MODEL["weights"]["fit"]
RULES = MODEL["recommendation"]
UNKNOWN_VALUE = float(MODEL["unknown_factor_value"])
NAMES = list(FIT_W)


def _factors(values: dict) -> list[dict]:
    out = []
    for name in NAMES:
        v = values.get(name)
        if v is None:
            out.append(bs._factor(name, None, bs.STATUS_UNKNOWN, "not extracted"))
        else:
            out.append(bs._factor(name, float(v), bs.STATUS_VERIFIED, "test"))
    return out


def _call(values: dict) -> tuple[str, float | None]:
    factors = _factors(values)
    fit, cov = bs._weighted_mean(factors, FIT_W, UNKNOWN_VALUE)
    bounds = bs._fit_bounds(factors, FIT_W)
    return bs._recommend(fit, {}, MODEL, bounds, cov), fit


def _analysis(certifications: list[str]) -> dict:
    return {
        "opportunity": {
            "title": "Baseline study for child protection, Dollo Ado",
            "client": "UNICEF",
            "project_location": ["Ethiopia", "Somali Region"],
            "submission_deadline": None,
        },
        "requirements": {
            "thematic_areas": ["child protection", "baseline"],
            "language_requirements": ["English", "Somali"],
            "certifications": certifications,
            "geographic_experience": ["Ethiopia"],
        },
        "bid_analysis": {"is_consultancy_contract": True},
    }


# ── Rule 1: unknown factors are penalised, not dropped ─────────────────────

def test_unknown_factor_counts_at_the_penalty_with_its_full_weight():
    known = {"geography": 100, "thematic": 100, "language": 100, "team_capacity": 100}
    fit, coverage = bs._weighted_mean(_factors(known), FIT_W, UNKNOWN_VALUE)
    expected = (100 * (1 - FIT_W["eligibility"]) + UNKNOWN_VALUE * FIT_W["eligibility"])
    assert fit == expected
    assert fit < 100, "dropping the unknown would have scored this 100"
    assert coverage == 1 - FIT_W["eligibility"]


def test_unknown_scores_lower_than_any_known_value_above_the_penalty():
    base = {"geography": 80, "thematic": 80, "language": 80, "team_capacity": 80}
    unknown_fit = bs._weighted_mean(_factors(base), FIT_W, UNKNOWN_VALUE)[0]
    known_fit = bs._weighted_mean(
        _factors({**base, "eligibility": UNKNOWN_VALUE + 1}), FIT_W, UNKNOWN_VALUE
    )[0]
    assert unknown_fit < known_fit


def test_the_59_then_77_tender_cannot_swing_between_runs():
    """Run A found no eligibility clause, run B found one Cortech meets."""
    run_a = bs.compute_bid_intelligence(_analysis([]))
    run_b = bs.compute_bid_intelligence(_analysis(["child safeguarding policy"]))
    assert "eligibility" in run_a["fit"]["unknown_factors"]
    assert "eligibility" not in run_b["fit"]["unknown_factors"]
    # Missing evidence lowers the score; it never raises it.
    assert run_a["fit"]["score"] < run_b["fit"]["score"]
    # While eligibility is unknown the call is not BID unless the worst case is.
    if run_a["recommendation"] == "BID":
        assert run_a["fit"]["worst_case"] >= RULES["bid_min_fit"]
    # Learning the fact never makes the call less favourable than before.
    order = {"NO-BID": 0, "WATCH": 1, "BID": 2}
    assert order[run_b["recommendation"]] >= order[run_a["recommendation"]]
    why = run_a["why"]
    assert "eligibility" in (why if isinstance(why, str) else " ".join(why))


# ── Rule 2: a single unknown never flips BID and WATCH ─────────────────────

GRID = (0, 25, 50, 70, 85, 100)


def test_no_single_unknown_can_flip_a_bid_to_watch_exhaustive():
    """For every combination on the grid and every factor left unknown: if the
    call is BID while it is unknown, it is BID for every value it can take."""
    checked = 0
    for values in itertools.product(GRID, repeat=len(NAMES)):
        full = dict(zip(NAMES, values))
        for hidden in NAMES:
            partial = {k: v for k, v in full.items() if k != hidden}
            call_unknown, _ = _call(partial)
            if call_unknown != "BID":
                continue
            for revealed in range(0, 101, 5):
                call_known, _ = _call({**partial, hidden: revealed})
                assert call_known == "BID", (partial, hidden, revealed)
            checked += 1
    assert checked > 0


def test_a_single_unknown_holds_a_borderline_tender_at_watch():
    strong = {"geography": 60, "thematic": 100, "language": 100, "team_capacity": 50}
    call, _fit = _call(strong)  # eligibility unknown: worst case 68
    assert call == "WATCH"
    assert _call({**strong, "eligibility": 0})[0] == "WATCH"
    assert _call({**strong, "eligibility": 100})[0] == "BID"


def test_bid_with_unknowns_only_when_the_worst_case_clears_the_line():
    call, _ = _call({"geography": 100, "thematic": 100, "language": 100, "eligibility": 100})
    worst, _ = bs._fit_bounds(
        _factors({"geography": 100, "thematic": 100, "language": 100, "eligibility": 100}),
        FIT_W,
    )
    assert worst >= RULES["bid_min_fit"]
    assert call == "BID"


# ── NO-BID rests on known evidence ──────────────────────────────────────────

def test_low_score_made_of_penalties_is_watch_not_no_bid():
    call, fit = _call({"team_capacity": 100})  # 90% of the weight unknown
    assert fit < RULES["watch_min_fit"]
    assert call == "WATCH"


def test_verified_bad_evidence_is_still_no_bid():
    call, _ = _call({"geography": 0, "thematic": 20, "language": 100})
    assert call == "NO-BID"
