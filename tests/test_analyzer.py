from types import SimpleNamespace

import json
import pytest

from config import CLAUDE_MODEL
from intelligence.analysis_schema import AnalysisSchemaError
from intelligence.analyzer import analyze_rfp, parse_analysis_payload
from intelligence.extraction_provenance import (
    STATUS_INSUFFICIENT,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    attach_extraction_provenance,
    sanitize_provenance_record,
)


_TOR = (
    "Terms of Reference for an endline evaluation of a livelihoods "
    "programme in Somalia with defined deliverables and a scope of work. "
    "Client UNICEF. Submission deadline 2099-12-01."
)


def _response(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def _patch_llm(monkeypatch, handler):
    monkeypatch.setattr("intelligence.analyzer.complete", handler)
    monkeypatch.setattr("intelligence.analyzer.usage_totals", lambda _r: (1, 1))
    monkeypatch.setattr("intelligence.analyzer.log_agent_action", lambda **kwargs: None)


def test_analyze_rfp_rejects_empty_document_without_calling_claude(monkeypatch):
    called = []
    monkeypatch.setattr(
        "intelligence.analyzer.complete",
        lambda **kwargs: called.append(kwargs) or SimpleNamespace(),
    )

    assert analyze_rfp("") == {}
    assert analyze_rfp("   \n") == {}
    assert analyze_rfp(None) == {}  # type: ignore[arg-type]
    assert called == []


def test_analyze_rfp_null_bid_analysis_defaults_consultancy_true(monkeypatch):
    payload = (
        '{"opportunity": {"title": "Somalia MEL Evaluation", "client": "UNICEF"},'
        ' "bid_analysis": null}'
    )
    _patch_llm(monkeypatch, lambda **kwargs: _response(payload))

    analysis = analyze_rfp(_TOR)

    assert analysis["bid_analysis"]["is_consultancy_contract"] is True
    assert analysis["opportunity"]["title"] == "Somalia MEL Evaluation"
    provenance = analysis["extraction_provenance"]["opportunity.client"]
    assert provenance["status"] == STATUS_VERIFIED
    assert provenance["page"] is None
    assert "UNICEF" in (provenance.get("excerpt") or "")


def test_parse_malformed_json_raises():
    with pytest.raises(json.JSONDecodeError):
        parse_analysis_payload("not-json {", "Title")


def test_parse_non_object_json_is_empty_not_a_record():
    assert parse_analysis_payload("[1, 2, 3]", "Title") == {}


def test_parse_schema_violating_json_raises_not_a_silent_record():
    payload = json.dumps({
        "opportunity": {"title": "X", "client": "Y", "estimated_budget_usd": {"n": 1}},
        "bid_analysis": {"is_consultancy_contract": True},
    })
    with pytest.raises(AnalysisSchemaError, match="estimated_budget_usd"):
        parse_analysis_payload(payload, "Title")


def test_parse_garbage_consultancy_flag_is_schema_failure_not_true():
    payload = json.dumps({
        "opportunity": {"title": "X", "client": "Y"},
        "bid_analysis": {"is_consultancy_contract": "yes"},
    })
    with pytest.raises(AnalysisSchemaError, match="is_consultancy_contract"):
        parse_analysis_payload(payload, "Title")


def test_analyze_rfp_malformed_json_returns_empty_without_retry(monkeypatch):
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        return _response("not-json {")

    _patch_llm(monkeypatch, complete)
    assert analyze_rfp(_TOR) == {}
    assert len(calls) == 1
    assert calls[0]["model"] == CLAUDE_MODEL


def test_schema_violation_retries_once_then_fails(monkeypatch):
    calls = []
    bad = json.dumps({
        "opportunity": {
            "title": "X",
            "client": "Y",
            "estimated_budget_usd": {"n": 1},
        },
        "bid_analysis": {"is_consultancy_contract": True},
    })

    def complete(**kwargs):
        calls.append(kwargs)
        return _response(bad)

    _patch_llm(monkeypatch, complete)
    assert analyze_rfp(_TOR) == {}
    assert len(calls) == 2
    assert all(call["model"] == CLAUDE_MODEL for call in calls)
    repair = calls[1]["messages"][-1]["content"]
    assert "didn't match the schema because" in repair
    assert "estimated_budget_usd" in repair
    # Tender stays wrapped on the original user turn, not in the repair note.
    assert "UNTRUSTED" in calls[0]["messages"][0]["content"]
    assert calls[1]["stage"] == "analyze_rfp_schema_retry"


def test_schema_violation_retry_succeeds_with_valid_json(monkeypatch):
    calls = []
    bad = json.dumps({
        "opportunity": {"title": "X", "client": "Y", "estimated_budget_usd": {"n": 1}},
        "bid_analysis": {"is_consultancy_contract": True},
    })
    good = json.dumps({
        "opportunity": {
            "title": "Somalia MEL Evaluation",
            "client": "UNICEF",
            "estimated_budget_usd": None,
        },
        "bid_analysis": {"is_consultancy_contract": True},
    })
    texts = [bad, good]

    def complete(**kwargs):
        calls.append(kwargs)
        return _response(texts.pop(0))

    _patch_llm(monkeypatch, complete)
    analysis = analyze_rfp(_TOR)
    assert analysis["opportunity"]["client"] == "UNICEF"
    assert analysis["bid_analysis"]["is_consultancy_contract"] is True
    assert len(calls) == 2


def test_missing_source_provenance_is_insufficient_not_a_fake_page():
    parsed = parse_analysis_payload(
        json.dumps({
            "opportunity": {"title": "Endline", "client": "UNICEF"},
            "bid_analysis": {"is_consultancy_contract": True},
        }),
        "Endline",
    )
    client = parsed["extraction_provenance"]["opportunity.client"]
    assert client["status"] == STATUS_INSUFFICIENT
    assert client["page"] is None
    assert client["excerpt"] is None
    assert client["source_file"] is None


def test_extracted_value_not_in_source_is_unknown_not_a_citation():
    parsed = parse_analysis_payload(
        json.dumps({
            "opportunity": {"title": "Invented Title", "client": "Not In Document"},
            "bid_analysis": {"is_consultancy_contract": True},
        }),
        "Invented Title",
        source_text="Terms of Reference for a WASH survey in Dadaab, Kenya.",
    )
    client = parsed["extraction_provenance"]["opportunity.client"]
    assert client["status"] == STATUS_UNKNOWN
    assert client["page"] is None
    assert client["excerpt"] is None


def test_page_is_recorded_only_when_marker_exists():
    source = (
        "===== SOURCE FILE: annex-ii.pdf =====\n\n"
        "----- PAGE 1 -----\nCover page only.\n\n"
        "----- PAGE 2 -----\nClient UNICEF hires a firm for an endline evaluation in Somalia."
    )
    analysis = {
        "opportunity": {"client": "UNICEF", "title": "endline evaluation"},
        "requirements": {},
        "bid_analysis": {"is_consultancy_contract": True},
    }
    attach_extraction_provenance(analysis, source)
    client = analysis["extraction_provenance"]["opportunity.client"]
    assert client["status"] == STATUS_VERIFIED
    assert client["page"] == 2
    assert client["source_file"] == "annex-ii.pdf"
    assert "UNICEF" in client["excerpt"]


def test_malformed_provenance_does_not_keep_invented_page():
    cleaned = sanitize_provenance_record(
        {
            "field": "opportunity.client",
            "value": "UNICEF",
            "status": STATUS_VERIFIED,
            "page": 99,
            "excerpt": "not actually in the source",
            "source_file": "invented.pdf",
        },
        "Terms of Reference. Client is named elsewhere.",
    )
    assert cleaned["page"] is None
    assert cleaned["status"] != STATUS_VERIFIED
    assert cleaned["excerpt"] is None
