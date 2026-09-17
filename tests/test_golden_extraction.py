"""Golden-set extraction harness — offline, no Anthropic key.

Live Claude is not called. Recorded analyzer JSON is fed through
``parse_analysis_payload`` (the production parse/validate path) and, for a
sample, through ``analyze_rfp`` with ``complete()`` mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from intelligence.analyzer import analyze_rfp, parse_analysis_payload
from intelligence.extraction_provenance import STATUS_VERIFIED
from tests.golden.grounding import ungrounded_fields
from tests.golden.loader import (
    GoldenSetError,
    GOLDEN_PATH,
    analysis_from_labels,
    load_golden_set,
    recorded_payload,
    validate_golden_item,
)
from tests.golden.metrics import (
    boolean_confusion,
    coerce_bool,
    coerce_number,
    format_rate,
    jaccard,
    mae,
    null_agreement,
)
from utils.untrusted import UNTRUSTED_BEGIN, UNTRUSTED_END


def _norm_client(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"unknown client", "unknown", "n/a"}:
        return None
    return text.lower()


def _extracted_fields(parsed: dict) -> dict:
    opp = parsed.get("opportunity") if isinstance(parsed.get("opportunity"), dict) else {}
    req = parsed.get("requirements") if isinstance(parsed.get("requirements"), dict) else {}
    bid = parsed.get("bid_analysis") if isinstance(parsed.get("bid_analysis"), dict) else {}
    return {
        "is_consultancy_contract": coerce_bool(bid.get("is_consultancy_contract")),
        "client": _norm_client(opp.get("client")),
        "deadline": opp.get("submission_deadline") or None,
        "budget_usd": coerce_number(opp.get("estimated_budget_usd")),
        "thematic_areas": req.get("thematic_areas") or [],
        "geography": opp.get("project_location") or req.get("geographic_experience") or [],
    }


def compute_extraction_metrics(items: list[dict]) -> dict:
    bool_pairs = []
    client_pairs = []
    deadline_pairs = []
    budget_pairs = []
    budget_null_pairs = []
    geo_scores = []
    theme_scores = []
    skipped = []

    for item in items:
        labels = item["labels"]
        try:
            parsed = parse_analysis_payload(
                json.dumps(recorded_payload(item)),
                item["title"],
                source_text=item["document_text"],
            )
        except json.JSONDecodeError:
            skipped.append({"id": item["id"], "reason": "recorded JSON did not parse"})
            continue
        if not parsed:
            skipped.append({"id": item["id"], "reason": "parse returned empty"})
            continue

        extracted = _extracted_fields(parsed)
        labeled_bool = labels["is_consultancy_contract"]
        predicted_bool = extracted["is_consultancy_contract"]
        if predicted_bool is None:
            skipped.append({"id": item["id"], "reason": "consultancy boolean still UNKNOWN after parse"})
            continue
        bool_pairs.append((predicted_bool, labeled_bool))
        client_pairs.append((_norm_client(extracted["client"]), _norm_client(labels.get("client"))))
        deadline_pairs.append((extracted["deadline"] or None, labels.get("deadline")))
        pred_budget = extracted["budget_usd"]
        gold_budget = coerce_number(labels.get("budget_usd"))
        budget_null_pairs.append((pred_budget, gold_budget))
        if pred_budget is not None and gold_budget is not None:
            budget_pairs.append((pred_budget, gold_budget))
        geo_scores.append(jaccard(extracted["geography"], labels.get("geography")))
        theme_scores.append(jaccard(extracted["thematic_areas"], labels.get("thematic_areas")))

    confusion = boolean_confusion(bool_pairs)
    geo_known = [s for s in geo_scores if s is not None]
    theme_known = [s for s in theme_scores if s is not None]
    client_exact = (
        sum(p == y for p, y in client_pairs) / len(client_pairs) if client_pairs else None
    )
    deadline_exact = (
        sum((p or None) == (y or None) for p, y in deadline_pairs) / len(deadline_pairs)
        if deadline_pairs
        else None
    )
    return {
        "n_items": len(items),
        "n_scored": len(bool_pairs),
        "n_skipped": len(skipped),
        "skipped": skipped,
        "consultancy": confusion,
        "client_exact": client_exact,
        "deadline_exact": deadline_exact,
        "budget_mae": mae(budget_pairs),
        "budget_numeric_n": len(budget_pairs),
        "budget_null_agreement": null_agreement(budget_null_pairs),
        "geography_jaccard": sum(geo_known) / len(geo_known) if geo_known else None,
        "thematic_jaccard": sum(theme_known) / len(theme_known) if theme_known else None,
        "what_this_measures": (
            "Offline parse/validate of recorded analyzer JSON against human labels. "
            "This is not live Claude extraction accuracy."
        ),
    }


def _minimal_item(**overrides):
    item = {
        "id": "g-tmp",
        "source_kind": "synthetic_consultancy",
        "title": "t",
        "document_text": "hello",
        "labels": {
            "is_consultancy_contract": True,
            "client": "Client A",
            "deadline": None,
            "budget_usd": None,
            "thematic_areas": [],
            "geography": [],
            "outcome": "UNKNOWN",
        },
    }
    label_overrides = overrides.pop("labels", {})
    item.update(overrides)
    item["labels"] = {**item["labels"], **label_overrides}
    return item


def test_golden_set_exists_and_is_balanced():
    items = load_golden_set()
    assert 20 <= len(items) <= 40
    flags = [item["labels"]["is_consultancy_contract"] for item in items]
    assert True in flags and False in flags
    outcomes = {item["labels"]["outcome"] for item in items}
    assert outcomes <= {"WON", "LOST", "UNKNOWN"}
    clients = " ".join(
        str(item["labels"].get("client") or "") + " " + item["title"] for item in items
    ).lower()
    # Fixtures must stay anonymized — no live client dump from Airtable/proposals.
    for banned in (
        "save the children",
        "welthungerhilfe",
        "peace winds",
        "lutheran world",
        "danish refugee",
        "qrcs",
        "sodma",
    ):
        assert banned not in clients


def test_extraction_metrics_on_recorded_payloads(capsys):
    items = load_golden_set()
    metrics = compute_extraction_metrics(items)
    consultancy = metrics["consultancy"]
    print("\nGOLDEN EXTRACTION (offline, recorded JSON → parse_analysis_payload)")
    print(f"  n={metrics['n_items']} scored={metrics['n_scored']} skipped={metrics['n_skipped']}")
    print(
        "  is_consultancy_contract "
        f"P={format_rate(consultancy['precision'])} "
        f"R={format_rate(consultancy['recall'])} "
        f"acc={format_rate(consultancy['accuracy'])} "
        f"tp={consultancy['tp']} fp={consultancy['fp']} "
        f"tn={consultancy['tn']} fn={consultancy['fn']}"
    )
    print(f"  client exact={format_rate(metrics['client_exact'])}")
    print(f"  deadline exact={format_rate(metrics['deadline_exact'])}")
    print(
        f"  budget MAE={format_rate(metrics['budget_mae'])} "
        f"(n_numeric={metrics['budget_numeric_n']}) "
        f"null_agree={format_rate(metrics['budget_null_agreement'])}"
    )
    print(f"  geography Jaccard={format_rate(metrics['geography_jaccard'])}")
    print(f"  thematic Jaccard={format_rate(metrics['thematic_jaccard'])}")
    print(f"  note: {metrics['what_this_measures']}")
    captured = capsys.readouterr()
    assert "GOLDEN EXTRACTION" in captured.out

    assert metrics["n_skipped"] == 0
    assert consultancy["precision"] == 1.0
    assert consultancy["recall"] == 1.0
    assert consultancy["accuracy"] == 1.0
    assert metrics["client_exact"] == 1.0
    assert metrics["deadline_exact"] == 1.0
    assert metrics["budget_mae"] == 0.0
    assert metrics["budget_numeric_n"] >= 2
    assert metrics["geography_jaccard"] == 1.0
    assert metrics["thematic_jaccard"] == 1.0


def test_labeled_non_null_fields_have_verified_provenance():
    """Phase 1: a labeled fact must cite a chunk in document_text, not a fake page."""
    missing = []
    for item in load_golden_set():
        parsed = parse_analysis_payload(
            json.dumps(recorded_payload(item)),
            item["title"],
            source_text=item["document_text"],
        )
        provenance = parsed.get("extraction_provenance") or {}
        labels = item["labels"]
        checks = []
        if labels.get("client"):
            checks.append(("opportunity.client", labels["client"]))
        if labels.get("deadline"):
            checks.append(("opportunity.submission_deadline", labels["deadline"]))
        if labels.get("budget_usd") is not None:
            checks.append(("opportunity.estimated_budget_usd", labels["budget_usd"]))
        for index, geo in enumerate(labels.get("geography") or []):
            checks.append((f"opportunity.project_location[{index}]", geo))
        for index, theme in enumerate(labels.get("thematic_areas") or []):
            checks.append((f"requirements.thematic_areas[{index}]", theme))
        for field, _value in checks:
            record = provenance.get(field) or {}
            if record.get("status") != STATUS_VERIFIED:
                missing.append(f"{item['id']} {field}={record.get('status')}")
            if record.get("page") is not None:
                missing.append(f"{item['id']} {field} invented page {record.get('page')}")
    assert missing == []


def test_ungrounded_helper_flags_invented_client_not_in_text():
    item = _minimal_item(labels={"client": "Secret Buyer"})
    issues = ungrounded_fields(item)
    assert any("Secret Buyer" in issue for issue in issues)


def test_missing_facts_item_does_not_require_a_client_in_text():
    items = {item["id"]: item for item in load_golden_set()}
    empty = items["g-029"]
    assert empty["labels"]["client"] is None
    assert empty["labels"]["budget_usd"] is None
    assert ungrounded_fields(empty) == []


def test_labeled_fields_are_grounded_in_document_text():
    """Labels that are not in the ToR text are invented facts, not a golden set."""
    problems = []
    for item in load_golden_set():
        issues = ungrounded_fields(item)
        if issues:
            problems.append(f"{item['id']}: {'; '.join(issues)}")
    assert problems == []


def test_vacancy_items_have_hr_language_and_false_consultancy_label():
    vacancies = [
        item for item in load_golden_set() if item["source_kind"] == "synthetic_vacancy"
    ]
    assert len(vacancies) >= 2
    for item in vacancies:
        assert item["labels"]["is_consultancy_contract"] is False
        assert item.get("expected_recommendation") is None
        assert ungrounded_fields(item) == []


def test_analyze_rfp_mocked_complete_uses_parse_path(monkeypatch):
    items = load_golden_set()
    sample = items[0]
    payload = json.dumps(recorded_payload(sample))
    fenced = "```json\n" + payload + "\n```"
    captured = []

    def _complete(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=fenced)],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr("intelligence.analyzer.complete", _complete)
    monkeypatch.setattr("intelligence.analyzer.usage_totals", lambda _r: (1, 1))
    monkeypatch.setattr("intelligence.analyzer.log_agent_action", lambda **kwargs: None)

    analysis = analyze_rfp(sample["document_text"], title=sample["title"])
    assert analysis["bid_analysis"]["is_consultancy_contract"] is True
    assert analysis["opportunity"]["title"]
    assert captured, "analyze_rfp must call complete()"
    user_content = captured[0]["messages"][0]["content"]
    assert UNTRUSTED_BEGIN in user_content
    assert UNTRUSTED_END in user_content
    assert sample["document_text"][:40] in user_content


def test_analyze_rfp_mocked_vacancy_keeps_false_consultancy_flag(monkeypatch):
    items = {item["id"]: item for item in load_golden_set()}
    sample = items["g-030"]
    payload = json.dumps(recorded_payload(sample))
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=payload)],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr("intelligence.analyzer.complete", lambda **kwargs: response)
    monkeypatch.setattr("intelligence.analyzer.usage_totals", lambda _r: (1, 1))
    monkeypatch.setattr("intelligence.analyzer.log_agent_action", lambda **kwargs: None)

    analysis = analyze_rfp(sample["document_text"], title=sample["title"])
    assert analysis["bid_analysis"]["is_consultancy_contract"] is False


def test_recorded_extra_keys_and_comma_budget_still_match_labels():
    items = {item["id"]: item for item in load_golden_set()}
    noisy = recorded_payload(items["g-001"])
    assert "advisory_comment" in noisy
    parsed = parse_analysis_payload(json.dumps(noisy), items["g-001"]["title"])
    assert parsed["opportunity"]["client"] == "Client A"

    budget_item = items["g-027"]
    raw_budget = recorded_payload(budget_item)["opportunity"]["estimated_budget_usd"]
    assert raw_budget == "45,000"
    parsed_budget = parse_analysis_payload(
        json.dumps(recorded_payload(budget_item)),
        budget_item["title"],
        source_text=budget_item["document_text"],
    )
    assert parsed_budget["opportunity"]["estimated_budget_usd"] == 45000.0


def test_missing_consultancy_key_defaults_true_on_parse():
    items = {item["id"]: item for item in load_golden_set()}
    sample = items["g-026"]
    recorded = recorded_payload(sample)
    assert "is_consultancy_contract" not in recorded.get("bid_analysis", {})
    parsed = parse_analysis_payload(json.dumps(recorded), sample["title"])
    assert parsed["bid_analysis"]["is_consultancy_contract"] is True


def test_malformed_recorded_json_is_empty_not_a_crash():
    with pytest.raises(json.JSONDecodeError):
        parse_analysis_payload("not-json {", "Title")
    assert parse_analysis_payload("[1, 2, 3]", "Title") == {}


def test_empty_golden_set_is_handled(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("[]\n", encoding="utf-8")
    assert load_golden_set(empty) == []
    metrics = compute_extraction_metrics([])
    assert metrics["n_items"] == 0
    assert metrics["consultancy"]["n"] == 0
    assert metrics["consultancy"]["precision"] is None


def test_malformed_golden_item_fails_explicitly_not_crash():
    with pytest.raises(GoldenSetError):
        validate_golden_item({"id": "bad"}, index=0)
    with pytest.raises(GoldenSetError):
        validate_golden_item(
            {
                "id": "bad",
                "source_kind": "x",
                "title": "t",
                "document_text": "hello",
                "labels": {
                    "is_consultancy_contract": "yes",
                    "client": "Client A",
                    "deadline": None,
                    "budget_usd": None,
                    "thematic_areas": [],
                    "geography": [],
                    "outcome": "UNKNOWN",
                },
            }
        )


def test_missing_golden_file_is_explicit_error(tmp_path):
    with pytest.raises(GoldenSetError, match="not found"):
        load_golden_set(tmp_path / "missing.json")


def test_analysis_from_labels_does_not_invent_budget():
    items = load_golden_set()
    empty = next(item for item in items if item["id"] == "g-029")
    analysis = analysis_from_labels(empty)
    assert analysis["opportunity"]["estimated_budget_usd"] is None
    assert analysis["opportunity"]["submission_deadline"] is None
    assert analysis["opportunity"]["project_location"] == []


def test_committed_golden_path_is_the_json_array():
    assert GOLDEN_PATH.name == "opportunities.json"
    assert Path(GOLDEN_PATH).is_file()


def test_duplicate_ids_fail_explicitly(tmp_path):
    item = _minimal_item()
    path = tmp_path / "dup.json"
    path.write_text(json.dumps([item, dict(item)]), encoding="utf-8")
    with pytest.raises(GoldenSetError, match="duplicate"):
        load_golden_set(path)


def test_zero_or_boolean_budget_is_rejected():
    with pytest.raises(GoldenSetError, match="budget"):
        validate_golden_item(_minimal_item(labels={"budget_usd": 0}))
    with pytest.raises(GoldenSetError, match="budget"):
        validate_golden_item(_minimal_item(labels={"budget_usd": True}))


def test_impossible_deadline_is_rejected():
    with pytest.raises(GoldenSetError, match="deadline"):
        validate_golden_item(_minimal_item(labels={"deadline": "2026-13-40"}))
    with pytest.raises(GoldenSetError, match="deadline"):
        validate_golden_item(_minimal_item(labels={"deadline": "2026/12/01"}))


def test_unknown_source_kind_is_rejected():
    with pytest.raises(GoldenSetError, match="source_kind"):
        validate_golden_item(_minimal_item(source_kind="airtable_dump"))


def test_non_array_or_invalid_json_golden_file_fails(tmp_path):
    obj = tmp_path / "obj.json"
    obj.write_text('{"id": "nope"}\n', encoding="utf-8")
    with pytest.raises(GoldenSetError, match="JSON array"):
        load_golden_set(obj)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(GoldenSetError, match="not valid JSON"):
        load_golden_set(bad)
