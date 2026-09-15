"""Calibrated P(win) harness (Phase 6).

Official bid WIN PROBABILITY remains the heuristic in bid_scorer.py until a
model outperforms it on held-out labeled WON/LOST data. This module refuses
to emit a numeric calibrated probability when the labeled sample is too small
or one-class.

See docs/adr/010-calibrated-win-probability.md.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Optional

from intelligence.organizations import (
    STATUS_VERIFIED,
    normalize_outcome,
)

_ARTIFACT_PATH = Path(__file__).with_name("win_calibration_artifact.json")
_ARTIFACT_CACHE: dict | None = None

INSUFFICIENT = "INSUFFICIENT DATA"
SAMPLE_TOO_SMALL = "sample too small to trust"
OFFICIAL_SOURCE = "bid_scorer_heuristic"

# Conservative floors. Logistic on 8 rows is not calibration. A one-class
# WON pile (n_lost=0) is also not calibration — Phase 2 won=False is UNKNOWN.
MIN_LABELED_N = 30
MIN_PER_CLASS = 10

FEATURE_NAMES = (
    "intercept",
    "fit",
    "deadline_feasibility",
    "known_client",
    "eligibility",
    "org_won_count",
    "org_lost_count",
)

def load_calibration_artifact(path: Path | None = None) -> dict:
    """Load the versioned artifact. Malformed/missing → fail-open insufficient."""
    global _ARTIFACT_CACHE
    target = Path(path) if path is not None else _ARTIFACT_PATH
    if path is None and _ARTIFACT_CACHE is not None:
        return _ARTIFACT_CACHE
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    payload = dict(raw)
    payload.setdefault("artifact_version", "unknown")
    payload.setdefault("fitted", False)
    payload.setdefault("coefficients", None)
    payload.setdefault("min_labeled_n", MIN_LABELED_N)
    payload.setdefault("min_per_class", MIN_PER_CLASS)
    payload.setdefault("status", INSUFFICIENT)
    payload.setdefault("official_win_probability_source", OFFICIAL_SOURCE)
    if path is None:
        _ARTIFACT_CACHE = payload
    return payload


def calibration_artifact_version() -> str:
    return str(load_calibration_artifact().get("artifact_version") or "unknown")


def reset_calibration_artifact_cache() -> None:
    """Test helper."""
    global _ARTIFACT_CACHE
    _ARTIFACT_CACHE = None


def labeled_outcome_from_fields(
    *,
    outcome_raw: Any = None,
    won_flag: Any = None,
) -> tuple[str, str]:
    """Apply Phase 2 rules: won=False / missing → UNKNOWN, not Lost."""
    return normalize_outcome(outcome_raw, won_flag=True if won_flag is True else None)


def census_labeled_outcomes(rows: Iterable[dict] | None) -> dict:
    """Count distinct labeled WON/LOST from already-normalized or raw rows.

    Each row may include:
      - ``source_id`` (dedupe key; fallback is a synthetic index)
      - ``outcome`` / ``outcome_raw``
      - ``won`` / ``won_flag`` (True is WON; False/missing is UNKNOWN)
    Golden UNKNOWN and unchecked checkboxes do not become Lost.
    """
    won_ids: set[str] = set()
    lost_ids: set[str] = set()
    unknown_ids: set[str] = set()
    skipped_malformed = 0
    for index, row in enumerate(rows or ()):
        if not isinstance(row, dict):
            skipped_malformed += 1
            continue
        source_id = str(row.get("source_id") or row.get("id") or f"row-{index}")
        outcome_raw = row.get("outcome")
        if outcome_raw is None:
            outcome_raw = row.get("outcome_raw")
        won_flag = row.get("won")
        if won_flag is None:
            won_flag = row.get("won_flag")
        label, status = labeled_outcome_from_fields(
            outcome_raw=outcome_raw,
            won_flag=won_flag,
        )
        if label == "WON" and status == STATUS_VERIFIED:
            won_ids.add(source_id)
            lost_ids.discard(source_id)
            unknown_ids.discard(source_id)
        elif label == "LOST" and status == STATUS_VERIFIED:
            if source_id not in won_ids:
                lost_ids.add(source_id)
                unknown_ids.discard(source_id)
        else:
            if source_id not in won_ids and source_id not in lost_ids:
                unknown_ids.add(source_id)
    n_won = len(won_ids)
    n_lost = len(lost_ids)
    n_unknown = len(unknown_ids)
    n_labeled = n_won + n_lost
    return {
        "n_won": n_won,
        "n_lost": n_lost,
        "n_unknown": n_unknown,
        "n_labeled": n_labeled,
        "malformed_skipped": skipped_malformed,
        "meets_bar": meets_calibration_bar(n_won=n_won, n_lost=n_lost),
    }


def census_from_artifact(artifact: dict | None = None) -> dict:
    artifact = artifact if isinstance(artifact, dict) else load_calibration_artifact()
    block = artifact.get("census") if isinstance(artifact.get("census"), dict) else {}
    n_won = int(block.get("n_won") or 0)
    n_lost = int(block.get("n_lost") or 0)
    n_labeled = int(block.get("n_labeled") or (n_won + n_lost))
    n_unknown = int(
        block.get("n_unknown")
        or (
            (block.get("golden") or {}).get("unknown") or 0
        )
    )
    return {
        "n_won": n_won,
        "n_lost": n_lost,
        "n_unknown": n_unknown,
        "n_labeled": n_labeled,
        "malformed_skipped": 0,
        "meets_bar": meets_calibration_bar(n_won=n_won, n_lost=n_lost),
        "as_of": block.get("as_of"),
        "source": "artifact",
    }


def meets_calibration_bar(*, n_won: int, n_lost: int) -> bool:
    n_labeled = int(n_won) + int(n_lost)
    return (
        n_labeled >= MIN_LABELED_N
        and int(n_won) >= MIN_PER_CLASS
        and int(n_lost) >= MIN_PER_CLASS
    )


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, float(z)))
    return 1.0 / (1.0 + math.exp(-z))


def _as_unit_score(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number > 1.0:
        number = number / 100.0
    return _clip01(number)


def _as_count(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return min(number, 20.0) / 10.0


def extract_model_features(
    bid_intelligence: Any,
    org_history: Any = None,
) -> Optional[list[float]]:
    """Build the designed feature vector, or None if inputs are unusable.

    Missing org history or malformed factors → None (fail-open to a null
    calibrated field, not a crash and not imputed zeros presented as data).
    """
    if not isinstance(bid_intelligence, dict):
        return None
    if org_history is None:
        return None
    if not isinstance(org_history, dict):
        return None

    fit_block = bid_intelligence.get("fit")
    fit_score = None
    if isinstance(fit_block, dict):
        fit_score = _as_unit_score(fit_block.get("score"))
    fv = bid_intelligence.get("factor_values")
    if not isinstance(fv, dict):
        fv = {}
    if fit_score is None:
        fit_score = _as_unit_score(fv.get("fit"))
    deadline = _as_unit_score(fv.get("deadline_feasibility"))
    known_client = _as_unit_score(fv.get("known_client"))
    eligibility = _as_unit_score(fv.get("eligibility"))
    if None in (fit_score, deadline, known_client, eligibility):
        return None

    org_won = _as_count(org_history.get("won"))
    org_lost = _as_count(org_history.get("lost"))
    if org_won is None or org_lost is None:
        return None

    return [
        1.0,
        fit_score,
        deadline,
        known_client,
        eligibility,
        org_won,
        org_lost,
    ]


def _features_from_labeled_row(row: dict) -> Optional[list[float]]:
    intel = row.get("bid_intelligence")
    if not isinstance(intel, dict):
        intel = {
            "fit": {"score": row.get("fit")},
            "factor_values": {
                "deadline_feasibility": row.get("deadline_feasibility"),
                "known_client": row.get("known_client"),
                "eligibility": row.get("eligibility"),
            },
        }
    org = row.get("org_history")
    if org is None and ("org_won" in row or "org_lost" in row or "won_count" in row):
        org = {
            "won": row.get("org_won", row.get("won_count")),
            "lost": row.get("org_lost", row.get("lost_count")),
        }
    return extract_model_features(intel, org)


def _binary_label(row: dict) -> Optional[int]:
    label, status = labeled_outcome_from_fields(
        outcome_raw=row.get("outcome", row.get("outcome_raw")),
        won_flag=row.get("won", row.get("won_flag")),
    )
    if status != STATUS_VERIFIED:
        return None
    if label == "WON":
        return 1
    if label == "LOST":
        return 0
    return None


def _heuristic_from_row(row: dict) -> Optional[float]:
    direct = _as_unit_score(row.get("heuristic_win"))
    if direct is not None:
        return direct
    direct = _as_unit_score(row.get("win_probability"))
    if direct is not None:
        return direct
    intel = row.get("bid_intelligence")
    if isinstance(intel, dict):
        win = intel.get("win_probability")
        if isinstance(win, dict):
            return _as_unit_score(win.get("score"))
        return _as_unit_score(win)
    return None


def _usable_labeled_examples(rows: Iterable[dict] | None) -> list[tuple[str, list[float], int, Optional[float]]]:
    usable = []
    for index, row in enumerate(rows or ()):
        if not isinstance(row, dict):
            continue
        y = _binary_label(row)
        if y is None:
            continue
        features = _features_from_labeled_row(row)
        if features is None:
            continue
        source_id = str(row.get("source_id") or row.get("id") or f"row-{index}")
        usable.append((source_id, features, y, _heuristic_from_row(row)))
    # Stable dedupe: last WON/LOST wins for an id.
    by_id: dict[str, tuple[str, list[float], int, Optional[float]]] = {}
    for item in usable:
        by_id[item[0]] = item
    return [by_id[k] for k in sorted(by_id)]


def _held_out_split(
    examples: list[tuple[str, list[float], int, Optional[float]]],
) -> tuple[list, list]:
    """Deterministic ~20% holdout by source_id. Not a random shuffle."""
    train, test = [], []
    for item in examples:
        source_id = item[0]
        bucket = sum(ord(ch) for ch in source_id) % 5
        if bucket == 0:
            test.append(item)
        else:
            train.append(item)
    if not test and examples:
        test = examples[-max(1, len(examples) // 5) :]
        train_ids = {t[0] for t in test}
        train = [e for e in examples if e[0] not in train_ids]
    return train, test


def _fit_logistic(
    X: list[list[float]],
    y: list[int],
    *,
    l2: float = 1.0,
    lr: float = 0.4,
    steps: int = 250,
) -> list[float]:
    """Tiny L2 logistic. Stdlib only. Not used unless the labeled bar is met."""
    n = len(X)
    d = len(X[0]) if X else 0
    w = [0.0] * d
    if n == 0 or d == 0:
        return w
    for _ in range(steps):
        grad = [0.0] * d
        for i in range(n):
            z = sum(w[j] * X[i][j] for j in range(d))
            err = _sigmoid(z) - y[i]
            for j in range(d):
                grad[j] += err * X[i][j]
        for j in range(d):
            penalty = 0.0 if j == 0 else l2 * w[j]
            w[j] -= lr * (grad[j] / n + penalty)
    return w


def _predict_proba(features: list[float], weights: list[float]) -> float:
    z = sum(weights[j] * features[j] for j in range(min(len(weights), len(features))))
    return _sigmoid(z)


def brier_score(pairs: list[tuple[float, int]]) -> Optional[float]:
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def log_loss(pairs: list[tuple[float, int]], eps: float = 1e-15) -> Optional[float]:
    if not pairs:
        return None
    total = 0.0
    for p, y in pairs:
        p = min(1.0 - eps, max(eps, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1.0 - p))
    return total / len(pairs)


def binary_precision_recall(
    pairs: list[tuple[float, int]],
    *,
    threshold: float = 0.5,
) -> dict:
    tp = fp = tn = fn = 0
    for p, y in pairs:
        pred = 1 if p >= threshold else 0
        if y == 1 and pred == 1:
            tp += 1
        elif y == 0 and pred == 1:
            fp += 1
        elif y == 0 and pred == 0:
            tn += 1
        else:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
    }


def evaluate_held_out(labeled_rows: Iterable[dict] | None) -> dict:
    """Offline held-out evaluation. Empty/thin samples are not a fake Brier."""
    examples = _usable_labeled_examples(labeled_rows)
    n_won = sum(1 for e in examples if e[2] == 1)
    n_lost = sum(1 for e in examples if e[2] == 0)
    census = {
        "n_won": n_won,
        "n_lost": n_lost,
        "n_labeled": n_won + n_lost,
        "n_unknown": 0,
        "meets_bar": meets_calibration_bar(n_won=n_won, n_lost=n_lost),
    }
    if not census["meets_bar"]:
        return {
            "status": INSUFFICIENT,
            "brier": None,
            "log_loss": None,
            "precision": None,
            "recall": None,
            "heuristic_brier": None,
            "outperforms_heuristic": False,
            "n_train": 0,
            "n_test": 0,
            "n_won": n_won,
            "n_lost": n_lost,
            "note": SAMPLE_TOO_SMALL,
            "official_win_probability_source": OFFICIAL_SOURCE,
        }

    train, test = _held_out_split(examples)
    train_won = sum(1 for e in train if e[2] == 1)
    train_lost = sum(1 for e in train if e[2] == 0)
    test_won = sum(1 for e in test if e[2] == 1)
    test_lost = sum(1 for e in test if e[2] == 0)
    if not train or not test or train_won < 1 or train_lost < 1 or test_won < 1 or test_lost < 1:
        # Full set met the bar; this particular split cannot support a metric.
        return {
            "status": INSUFFICIENT,
            "brier": None,
            "log_loss": None,
            "precision": None,
            "recall": None,
            "heuristic_brier": None,
            "outperforms_heuristic": False,
            "n_train": len(train),
            "n_test": len(test),
            "n_won": n_won,
            "n_lost": n_lost,
            "note": SAMPLE_TOO_SMALL,
            "official_win_probability_source": OFFICIAL_SOURCE,
        }

    weights = _fit_logistic([e[1] for e in train], [e[2] for e in train])
    model_pairs = [(_predict_proba(e[1], weights), e[2]) for e in test]
    heuristic_pairs = [
        (e[3] if e[3] is not None else 0.5, e[2]) for e in test
    ]
    model_brier = brier_score(model_pairs)
    heuristic_brier = brier_score(heuristic_pairs)
    pr = binary_precision_recall(model_pairs)
    outperforms = (
        model_brier is not None
        and heuristic_brier is not None
        and model_brier < heuristic_brier
    )
    return {
        "status": STATUS_VERIFIED,
        "brier": model_brier,
        "log_loss": log_loss(model_pairs),
        "precision": pr["precision"],
        "recall": pr["recall"],
        "heuristic_brier": heuristic_brier,
        "outperforms_heuristic": bool(outperforms),
        "n_train": len(train),
        "n_test": len(test),
        "n_won": n_won,
        "n_lost": n_lost,
        "coefficients": weights,
        "note": (
            "Held-out split on injected labeled rows. "
            "Does not replace bid_scorer WIN PROBABILITY unless an operator "
            "promotes a later artifact after it beats the heuristic."
        ),
        "official_win_probability_source": OFFICIAL_SOURCE,
    }


def _insufficient_payload(
    census: dict,
    *,
    artifact: dict | None = None,
    extra_reason: str = "",
) -> dict:
    artifact = artifact if isinstance(artifact, dict) else {}
    held = artifact.get("held_out") if isinstance(artifact.get("held_out"), dict) else {}
    n_won = int(census.get("n_won") or 0)
    n_lost = int(census.get("n_lost") or 0)
    n_labeled = int(census.get("n_labeled") or (n_won + n_lost))
    reason = extra_reason or (
        f"{INSUFFICIENT}: n_labeled={n_labeled} "
        f"(WON={n_won}, LOST={n_lost}); "
        f"need n_labeled>={MIN_LABELED_N} and "
        f"n_won>={MIN_PER_CLASS} and n_lost>={MIN_PER_CLASS}. "
        f"{SAMPLE_TOO_SMALL}."
    )
    return {
        "value": None,
        "status": INSUFFICIENT,
        "unit": "probability",
        "reason": reason,
        "n_labeled": n_labeled,
        "n_won": n_won,
        "n_lost": n_lost,
        "min_labeled_n": MIN_LABELED_N,
        "min_per_class": MIN_PER_CLASS,
        "held_out_brier": held.get("brier"),
        "held_out_note": held.get("note") or SAMPLE_TOO_SMALL,
        "artifact_version": str(artifact.get("artifact_version") or "unknown"),
        "fitted": False,
        "official": False,
        "official_win_probability_source": OFFICIAL_SOURCE,
        "note": (
            "Heuristic WIN PROBABILITY in bid_scorer.py remains official. "
            "This field is not a frequentist P(win)."
        ),
    }


def predict_calibrated_win_probability(
    *,
    bid_intelligence: Any = None,
    org_history: Any = None,
    labeled_rows: Iterable[dict] | None = None,
    artifact: dict | None = None,
) -> dict:
    """Return the audit field. Never raises. Never invents a confident P(win)."""
    try:
        artifact_doc = artifact if isinstance(artifact, dict) else load_calibration_artifact()
        if labeled_rows is not None:
            census = census_labeled_outcomes(labeled_rows)
        else:
            census = census_from_artifact(artifact_doc)

        if not census.get("meets_bar"):
            return _insufficient_payload(census, artifact=artifact_doc)

        eval_result = evaluate_held_out(labeled_rows if labeled_rows is not None else [])
        if eval_result.get("status") != STATUS_VERIFIED or not eval_result.get("coefficients"):
            payload = _insufficient_payload(
                census,
                artifact=artifact_doc,
                extra_reason=(
                    f"{INSUFFICIENT}: labeled bar met but held-out split "
                    f"is {SAMPLE_TOO_SMALL}."
                ),
            )
            payload["held_out_brier"] = eval_result.get("brier")
            payload["held_out_note"] = eval_result.get("note") or SAMPLE_TOO_SMALL
            return payload

        features = extract_model_features(bid_intelligence, org_history)
        if features is None:
            payload = _insufficient_payload(
                census,
                artifact=artifact_doc,
                extra_reason=(
                    f"{INSUFFICIENT}: malformed factors or missing org history "
                    "— calibrated field left null (fail-open)."
                ),
            )
            payload["held_out_brier"] = eval_result.get("brier")
            payload["held_out_note"] = eval_result.get("note")
            payload["fitted"] = True
            return payload

        value = _predict_proba(features, eval_result["coefficients"])
        return {
            "value": round(float(value), 4),
            "status": STATUS_VERIFIED,
            "unit": "probability",
            "reason": (
                "Code logistic on deterministic bid_scorer factors plus Phase 2 "
                "org won/lost counts. Not the official bid-decision input."
            ),
            "n_labeled": census.get("n_labeled"),
            "n_won": census.get("n_won"),
            "n_lost": census.get("n_lost"),
            "min_labeled_n": MIN_LABELED_N,
            "min_per_class": MIN_PER_CLASS,
            "held_out_brier": eval_result.get("brier"),
            "held_out_log_loss": eval_result.get("log_loss"),
            "held_out_precision": eval_result.get("precision"),
            "held_out_recall": eval_result.get("recall"),
            "heuristic_brier": eval_result.get("heuristic_brier"),
            "outperforms_heuristic": eval_result.get("outperforms_heuristic"),
            "held_out_note": eval_result.get("note"),
            "artifact_version": str(artifact_doc.get("artifact_version") or "injected"),
            "fitted": True,
            "official": False,
            "official_win_probability_source": OFFICIAL_SOURCE,
            "note": (
                "Heuristic WIN PROBABILITY in bid_scorer.py remains official "
                "until a promoted artifact beats it on held-out Brier/log loss."
            ),
        }
    except Exception:
        return _insufficient_payload(
            {"n_won": 0, "n_lost": 0, "n_labeled": 0},
            artifact=artifact if isinstance(artifact, dict) else {},
            extra_reason=f"{INSUFFICIENT}: predictor failed open.",
        )


def attach_calibrated_win_probability(
    intelligence: Any,
    org_history: Any = None,
    *,
    labeled_rows: Iterable[dict] | None = None,
    artifact: dict | None = None,
) -> dict:
    """Mutate-or-wrap bid_intelligence with the audit field. Never raises."""
    if not isinstance(intelligence, dict):
        intelligence = {}
    intelligence["calibrated_win_probability"] = predict_calibrated_win_probability(
        bid_intelligence=intelligence,
        org_history=org_history,
        labeled_rows=labeled_rows,
        artifact=artifact,
    )
    return intelligence


def golden_outcomes_are_unknown(items: Iterable[dict] | None) -> bool:
    """True when every golden label.outcome is UNKNOWN (Phase 0 contract)."""
    found = False
    for item in items or ():
        if not isinstance(item, dict):
            return False
        labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
        if labels.get("outcome") != "UNKNOWN":
            return False
        found = True
    return found
