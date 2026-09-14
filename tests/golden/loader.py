"""Load and validate the Phase 0 golden evaluation set."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).resolve().parent
GOLDEN_PATH = GOLDEN_DIR / "opportunities.json"

VALID_OUTCOMES = {"WON", "LOST", "UNKNOWN"}
VALID_RECOMMENDATIONS = {"BID", "WATCH", "NO-BID"}
REQUIRED_TOP = ("id", "source_kind", "title", "document_text", "labels")
REQUIRED_LABELS = (
    "is_consultancy_contract",
    "client",
    "deadline",
    "budget_usd",
    "thematic_areas",
    "geography",
    "outcome",
)


class GoldenSetError(ValueError):
    """A golden fixture is malformed or internally inconsistent."""


def _is_null_or_iso_date(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or len(value) != 10:
        return False
    parts = value.split("-")
    if len(parts) != 3:
        return False
    year, month, day = parts
    return year.isdigit() and month.isdigit() and day.isdigit()


def validate_golden_item(item: Any, *, index: int | None = None) -> None:
    where = f"item {index}" if index is not None else "item"
    if not isinstance(item, dict):
        raise GoldenSetError(f"{where} is not an object")

    missing = [key for key in REQUIRED_TOP if key not in item]
    if missing:
        raise GoldenSetError(f"{where} missing fields: {missing}")

    if not isinstance(item["id"], str) or not item["id"].strip():
        raise GoldenSetError(f"{where} id must be a non-empty string")
    if not isinstance(item["title"], str) or not item["title"].strip():
        raise GoldenSetError(f"{where} title must be a non-empty string")
    if not isinstance(item["document_text"], str) or not item["document_text"].strip():
        raise GoldenSetError(f"{where} document_text must be a non-empty string")
    if not isinstance(item["source_kind"], str) or not item["source_kind"].strip():
        raise GoldenSetError(f"{where} source_kind must be a non-empty string")

    labels = item["labels"]
    if not isinstance(labels, dict):
        raise GoldenSetError(f"{where} labels must be an object")
    missing_labels = [key for key in REQUIRED_LABELS if key not in labels]
    if missing_labels:
        raise GoldenSetError(f"{where} labels missing fields: {missing_labels}")

    flag = labels["is_consultancy_contract"]
    if not isinstance(flag, bool):
        raise GoldenSetError(
            f"{where} labels.is_consultancy_contract must be a boolean, not {type(flag).__name__}"
        )
    if labels["client"] is not None and not isinstance(labels["client"], str):
        raise GoldenSetError(f"{where} labels.client must be string or null")
    if not _is_null_or_iso_date(labels["deadline"]):
        raise GoldenSetError(
            f"{where} labels.deadline must be YYYY-MM-DD or null"
        )
    budget = labels["budget_usd"]
    if budget is not None and not isinstance(budget, (int, float)):
        raise GoldenSetError(f"{where} labels.budget_usd must be number or null")
    if isinstance(budget, bool) or (isinstance(budget, (int, float)) and budget <= 0):
        raise GoldenSetError(
            f"{where} labels.budget_usd must be a positive number or null — not invented zeros"
        )
    for list_key in ("thematic_areas", "geography"):
        value = labels[list_key]
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise GoldenSetError(f"{where} labels.{list_key} must be a list of strings")
    if labels["outcome"] not in VALID_OUTCOMES:
        raise GoldenSetError(
            f"{where} labels.outcome must be one of {sorted(VALID_OUTCOMES)}"
        )

    expected = item.get("expected_recommendation")
    if expected is not None and expected not in VALID_RECOMMENDATIONS:
        raise GoldenSetError(
            f"{where} expected_recommendation must be BID/WATCH/NO-BID or null"
        )

    recorded = item.get("recorded_analyzer_json")
    if recorded is not None and not isinstance(recorded, dict):
        raise GoldenSetError(f"{where} recorded_analyzer_json must be an object or omitted")


def load_golden_set(path: Path | None = None) -> list[dict]:
    """Load the committed golden set. Empty file → empty list, not a crash."""
    target = Path(path) if path is not None else GOLDEN_PATH
    if not target.exists():
        raise GoldenSetError(f"golden set not found: {target}")
    raw = target.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GoldenSetError(f"golden set is not valid JSON: {exc}") from exc
    if data == []:
        return []
    if not isinstance(data, list):
        raise GoldenSetError("golden set must be a JSON array")
    ids: set[str] = set()
    items: list[dict] = []
    for index, item in enumerate(data):
        validate_golden_item(item, index=index)
        item_id = item["id"]
        if item_id in ids:
            raise GoldenSetError(f"duplicate golden id: {item_id}")
        ids.add(item_id)
        items.append(item)
    return items


def analysis_from_labels(item: dict) -> dict:
    """Construct the analysis dict the scorer sees, from human labels only."""
    labels = item["labels"]
    return {
        "opportunity": {
            "title": item["title"],
            "client": labels.get("client") or "",
            "donor": labels.get("donor"),
            "submission_deadline": labels.get("deadline"),
            "project_location": list(labels.get("geography") or []),
            "estimated_budget_usd": labels.get("budget_usd"),
        },
        "requirements": {
            "thematic_areas": list(labels.get("thematic_areas") or []),
            "language_requirements": list(labels.get("language_requirements") or []),
            "certifications": list(labels.get("certifications") or []),
            "geographic_experience": list(labels.get("geography") or []),
        },
        "bid_analysis": {
            "is_consultancy_contract": labels["is_consultancy_contract"],
            "submission_type": labels.get("submission_type") or "FULL_PROPOSAL",
            # Adversarial LLM numbers: official scores must not copy these.
            "cortech_fit_score": 1,
            "win_probability": 1,
            "bid_recommendation": "NO-BID",
            "key_strengths": [],
            "key_gaps": [],
        },
    }


def recorded_payload(item: dict) -> dict:
    recorded = item.get("recorded_analyzer_json")
    if isinstance(recorded, dict):
        return recorded
    return analysis_from_labels(item)
