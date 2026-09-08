from types import SimpleNamespace

from intelligence.analyzer import analyze_rfp


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
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=payload)],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    monkeypatch.setattr("intelligence.analyzer.complete", lambda **kwargs: response)
    monkeypatch.setattr("intelligence.analyzer.usage_totals", lambda _r: (1, 1))
    monkeypatch.setattr("intelligence.analyzer.log_agent_action", lambda **kwargs: None)

    analysis = analyze_rfp(
        "Terms of Reference for an endline evaluation of a livelihoods "
        "programme in Somalia with defined deliverables and a scope of work."
    )

    assert analysis["bid_analysis"]["is_consultancy_contract"] is True
    assert analysis["opportunity"]["title"] == "Somalia MEL Evaluation"
