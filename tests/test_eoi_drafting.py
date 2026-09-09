from intelligence.proposal_writer import (
    _build_past_work_context,
    _opportunity_fields,
    generate_proposal,
)
from intelligence.tender_reader import _BRIEF_PROMPT, _BRIEF_PROMPT_EOI
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


def test_executive_summary_null_location_does_not_crash(monkeypatch):
    from intelligence import proposal_writer

    monkeypatch.setattr(proposal_writer, "_generate_section", lambda *a, **k: "ok")
    text = proposal_writer.generate_executive_summary(
        {"opportunity": {"title": None, "project_location": None}},
        None,
        [],
    )
    assert text == "ok"
