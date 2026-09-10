from intelligence.proposal_writer import (
    _build_past_work_context,
    _opportunity_fields,
    generate_eoi,
    generate_proposal,
)
from intelligence.tender_reader import (
    _BRIEF_PROMPT,
    _BRIEF_PROMPT_EOI,
    _STRATEGY_PROMPT,
    _STRATEGY_PROMPT_EOI,
    build_win_strategy,
)
from reporting.docx_builder import SECTION_ORDER


def test_null_title_is_safe_to_slice():
    _, title, client, donor, deadline = _opportunity_fields({
        "opportunity": {
            "title": None,
            "client": None,
            "donor": None,
            "submission_deadline": None,
        }
    })
    assert title == "Unknown Assignment"
    assert client == "Client"
    assert donor == ""
    assert deadline == "TBD"
    assert title[:60] == title


def test_eoi_brief_is_shortlisting_not_full_proposal():
    assert "Shortlisting" in _BRIEF_PROMPT_EOI
    assert "Expression of Interest" in _BRIEF_PROMPT_EOI
    assert "must not" in _BRIEF_PROMPT_EOI.lower()
    assert "Gantt" in _BRIEF_PROMPT_EOI or "full technical" in _BRIEF_PROMPT_EOI.lower()


def test_proposal_brief_still_has_award_criteria():
    assert "Scored criteria" in _BRIEF_PROMPT
    assert "technical proposal" in _BRIEF_PROMPT.lower()


def test_briefs_require_problem_framing_and_output_users():
    for prompt in (_BRIEF_PROMPT, _BRIEF_PROMPT_EOI):
        assert "The problem as the client frames it" in prompt
        assert "Who uses the outputs and to decide what" in prompt


def test_eoi_strategy_is_shortlisting_not_a_method_chapter():
    assert "shortlisting" in _STRATEGY_PROMPT_EOI.lower()
    assert "Gantt" in _STRATEGY_PROMPT_EOI
    assert "Approach thesis" in _STRATEGY_PROMPT_EOI
    assert "Method thesis" in _STRATEGY_PROMPT
    assert "Method thesis" not in _STRATEGY_PROMPT_EOI


def test_win_strategy_skipped_without_documents_or_brief():
    assert build_win_strategy("", {"opportunity": {}}, doc_block="") == ""


def test_eoi_and_technical_prompts_forbid_financial_information():
    from intelligence.proposal_writer import EOI_WINNING_STANDARD, NO_MONETARY_RULE, generate_eoi
    import inspect

    assert "NO FINANCIAL INFORMATION" in NO_MONETARY_RULE
    assert "evaluation stays independent of price" in NO_MONETARY_RULE
    assert "Never include a financial offer" in EOI_WINNING_STANDARD
    src = inspect.getsource(generate_eoi)
    assert "monetary amount unless the REOI" not in src
    assert "financial offer unless" not in _STRATEGY_PROMPT_EOI


def test_docx_order_includes_full_eoi_backbone():
    keys = [k for k, _ in SECTION_ORDER]
    for required in (
        "cover_letter",
        "firm_profile",
        "understanding",
        "approach_summary",
        "relevant_experience",
        "key_experts",
        "eligibility",
        "compliance_matrix",
    ):
        assert required in keys
    assert keys.index("understanding") < keys.index("relevant_experience")
    assert keys.index("approach_summary") < keys.index("eligibility")


def test_past_work_context_survives_null_titles(monkeypatch):
    from intelligence import proposal_writer

    monkeypatch.setattr(
        proposal_writer,
        "search_past_proposals",
        lambda *args, **kwargs: [{
            "project_title": None,
            "won": True,
            "similarity": 0.4,
            "content_chunk": None,
            "metadata": {"location": None},
        }],
    )
    text = _build_past_work_context({"opportunity": {"title": "Energy evaluation"}})
    assert "RELEVANT PAST ASSIGNMENTS" in text


def test_generate_proposal_null_fields_dry_path(monkeypatch):
    from intelligence import proposal_writer

    monkeypatch.setattr(proposal_writer, "FULL_DRAFT_FOR_WATCH", False)
    monkeypatch.setattr(proposal_writer, "get_relevant_lessons", lambda *a, **k: "")
    monkeypatch.setattr(proposal_writer, "get_donor_intelligence", lambda *a, **k: "")
    monkeypatch.setattr(proposal_writer, "build_system_blocks", lambda *a, **k: [])
    monkeypatch.setattr(proposal_writer, "generate_cover_letter", lambda *a, **k: "letter")
    monkeypatch.setattr(
        proposal_writer, "generate_executive_summary", lambda *a, **k: "summary"
    )
    monkeypatch.setattr(proposal_writer, "generate_quality_self_score", lambda *a, **k: {})
    monkeypatch.setattr(proposal_writer, "_repair_weakest_section", lambda s, *a, **k: s)
    monkeypatch.setattr(proposal_writer, "_final_money_audit", lambda s: None)
    monkeypatch.setattr(proposal_writer, "_log_proposal_usage", lambda *a, **k: None)
    monkeypatch.setattr(proposal_writer, "load_past_work_matches", lambda *a, **k: [])

    sections = generate_proposal(
        {
            "bid_analysis": None,
            "opportunity": {"title": None, "project_location": None},
        },
        None,
        None,
    )
    assert sections["cover_letter"] == "letter"
    assert sections["executive_summary"] == "summary"
    assert sections["lightweight"] is True
    assert "claim_grounding" in sections
    assert "win_strategy" not in sections


def test_generate_eoi_attaches_win_strategy_meta(monkeypatch):
    from intelligence import proposal_writer

    def fake_blocks(*a, **k):
        return [
            {"type": "text", "text": "trusted guidance"},
            {
                "type": "meta",
                "tender_brief": "brief-meta",
                "win_strategy": "win-meta",
            },
        ]

    monkeypatch.setattr(proposal_writer, "get_relevant_lessons", lambda *a, **k: "")
    monkeypatch.setattr(proposal_writer, "get_donor_intelligence", lambda *a, **k: "")
    monkeypatch.setattr(proposal_writer, "build_system_blocks", fake_blocks)
    monkeypatch.setattr(proposal_writer, "_generate_section", lambda *a, **k: "section")
    monkeypatch.setattr(proposal_writer, "generate_quality_self_score", lambda *a, **k: {})
    monkeypatch.setattr(proposal_writer, "_repair_weakest_section", lambda s, *a, **k: s)
    monkeypatch.setattr(proposal_writer, "_final_money_audit", lambda s: None)
    monkeypatch.setattr(proposal_writer, "_log_proposal_usage", lambda *a, **k: None)
    monkeypatch.setattr(proposal_writer, "load_past_work_matches", lambda *a, **k: [])

    sections = generate_eoi(
        {"opportunity": {"title": None, "client": None}},
        None,
    )
    assert sections["submission_type"] == "EOI"
    assert sections["win_strategy"] == "win-meta"
    assert sections["tender_brief"] == "brief-meta"
    assert sections["cover_letter"] == "section"
    assert "claim_grounding" in sections


def test_executive_summary_null_location_does_not_crash(monkeypatch):
    from intelligence import proposal_writer

    monkeypatch.setattr(proposal_writer, "_generate_section", lambda *a, **k: "ok")
    text = proposal_writer.generate_executive_summary(
        {"opportunity": {"title": None, "project_location": None}},
        None,
        [],
    )
    assert text == "ok"


def test_quality_score_excludes_win_strategy_and_brief(monkeypatch):
    from types import SimpleNamespace

    from intelligence import proposal_writer

    captured = {}
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"overall_score": 80}')],
        usage=None,
    )
    monkeypatch.setattr(
        proposal_writer, "complete", lambda **kwargs: captured.update(kwargs) or response
    )
    proposal_writer.generate_quality_self_score(
        {
            "cover_letter": "letter about Mogadishu",
            "win_strategy": "SECRET STRATEGY SHOULD NOT BE SCORED",
            "tender_brief": "SECRET BRIEF SHOULD NOT BE SCORED",
            "submission_type": "EOI",
        },
        {"evaluation_criteria": []},
    )
    user = captured["messages"][0]["content"]
    assert "letter about Mogadishu" in user
    assert "SECRET STRATEGY SHOULD NOT BE SCORED" not in user
    assert "SECRET BRIEF SHOULD NOT BE SCORED" not in user
    assert "win_strategy" not in user
    assert "tender_brief" not in user
