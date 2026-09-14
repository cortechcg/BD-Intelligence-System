"""Offline metrics for the golden set. No network. Missing → skip, not a guess."""

from __future__ import annotations

from typing import Any, Optional


UNKNOWN = "UNKNOWN"


def _norm_terms(values: Any) -> set[str]:
    if not values:
        return set()
    if isinstance(values, str):
        values = [values]
    return {str(item).strip().lower() for item in values if str(item).strip()}


def jaccard(predicted: Any, labeled: Any) -> Optional[float]:
    pred = _norm_terms(predicted)
    gold = _norm_terms(labeled)
    if not pred and not gold:
        return 1.0
    if not pred or not gold:
        return 0.0
    union = pred | gold
    if not union:
        return 1.0
    return len(pred & gold) / len(union)


def boolean_confusion(pairs: list[tuple[bool, bool]]) -> dict:
    tp = fp = tn = fn = 0
    for predicted, labeled in pairs:
        if labeled and predicted:
            tp += 1
        elif labeled and not predicted:
            fn += 1
        elif (not labeled) and predicted:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    accuracy = (tp + tn) / len(pairs) if pairs else None
    return {
        "n": len(pairs),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "accuracy": accuracy,
    }


def mae(pairs: list[tuple[float, float]]) -> Optional[float]:
    if not pairs:
        return None
    return sum(abs(p - y) for p, y in pairs) / len(pairs)


def null_agreement(pairs: list[tuple[Any, Any]]) -> Optional[float]:
    if not pairs:
        return None
    agree = 0
    for predicted, labeled in pairs:
        pred_null = predicted is None
        gold_null = labeled is None
        if pred_null == gold_null:
            agree += 1
    return agree / len(pairs)


def coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def coerce_number(value: Any) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def format_rate(value: Optional[float]) -> str:
    if value is None:
        return UNKNOWN
    return f"{value:.3f}"
