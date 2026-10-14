"""Outline catalog and the pause between scoring and drafting."""
from __future__ import annotations

import intelligence.proposal_writer as proposal_writer
from intelligence.draft_templates import (
    FROM_TOR_ID,
    HOUSE_EOI_ID,
    HOUSE_TECHNICAL_ID,
    catalog,
    customize_outline,
    recommend_outline,
    resolve_outline,
)


def _analysis(submission_type="FULL_PROPOSAL", sections=None):
    analysis = {
        "bid_analysis": {"submission_type": submission_type, "bid_recommendation": "BID"},
        "opportunity": {"title": "Somalia evaluation"},
    }
    if sections is not None:
        analysis["submission_requirements"] = {"prescribed_proposal_sections": sections}
    return analysis


def test_recommendation_uses_the_tor_chapters_when_the_tor_lists_them():
    outline = recommend_outline(_analysis(sections=[
        "Understanding of the assignment",
        "Methodology (max 10 pages)",
        "Work plan",
    ]))
    assert outline["id"] == FROM_TOR_ID
    assert outline["source"] == "tor"
    assert outline["submission_type"] == "FULL_PROPOSAL"
    headings = [item["heading"] for item in outline["sections"]]
    assert "Methodology" in headings[1]
    assert outline["sections"][1]["page_limit"]
    assert outline["sections"][1]["route"] == "methodology"


def test_recommendation_falls_back_to_the_house_outline():
    outline = recommend_outline(_analysis())
    assert outline["id"] == FROM_TOR_ID
    assert outline["source"] == "template"
    assert "house outline" in outline["summary"]
    headings = [item["heading"] for item in outline["sections"]]
    assert headings[0] == "Cover letter"
    assert "Methodology" in headings


def test_eoi_house_template_does_not_grow_a_methodology_chapter():
    eoi = next(item for item in catalog() if item["id"] == HOUSE_EOI_ID)
    technical = next(item for item in catalog() if item["id"] == HOUSE_TECHNICAL_ID)
    assert eoi["submission_type"] == "EOI"
    assert technical["submission_type"] == "FULL_PROPOSAL"
    routes = {item["heading"]: item["route"] for item in eoi["sections"]}
    assert routes["Proposed approach"] == "approach_summary"
    assert "methodology" not in routes.values()
    approach = next(item for item in eoi["sections"] if item["heading"] == "Proposed approach")
    assert any("full methodology" in line.lower() for line in approach["must_include"])


def test_edited_sections_become_the_outline_for_this_opportunity():
    outline = customize_outline("EOI", [
        {"heading": "  ", "page_limit": "nope"},
        {
            "heading": "Firm profile",
            "page_limit": "3 pages",
            "must_include": ["Registration in Somalia"],
        },
        {"heading": "Proposed approach", "must_include": "Keep this short"},
    ], name="Somalia EOI")
    assert outline["id"] == "custom"
    assert outline["source"] == "custom"
    assert outline["submission_type"] == "EOI"
    assert [item["heading"] for item in outline["sections"]] == [
        "Firm profile",
        "Proposed approach",
    ]
    assert outline["sections"][0]["page_limit"] == "3 pages"
    assert outline["sections"][0]["must_include"] == ["Registration in Somalia"]
    assert outline["sections"][1]["route"] == "approach_summary"
    assert outline["sections"][1]["must_include"] == ["Keep this short"]


def test_selecting_a_template_does_not_change_the_catalog():
    before = catalog()
    chosen = resolve_outline({"template_id": HOUSE_TECHNICAL_ID, "name": "This bid only"})
    assert chosen["name"] == "This bid only"
    assert chosen["id"] == HOUSE_TECHNICAL_ID
    assert catalog()[0]["name"] == before[0]["name"]


def test_edited_sections_win_over_the_template_id():
    chosen = resolve_outline(
        {
            "template_id": HOUSE_TECHNICAL_ID,
            "submission_type": "EOI",
            "sections": [{"heading": "Letter of interest"}],
        },
        recommended=recommend_outline(_analysis()),
    )
    assert chosen["source"] == "custom"
    assert chosen["submission_type"] == "EOI"
    assert chosen["sections"][0]["route"] == "cover_letter"


def test_from_tor_uses_the_stored_recommendation():
    recommended = recommend_outline(_analysis(sections=["Work plan", "Proposed team"]))
    chosen = resolve_outline({"template_id": FROM_TOR_ID}, recommended=recommended)
    assert [item["heading"] for item in chosen["sections"]] == [
        item["heading"] for item in recommended["sections"]
    ]


def test_accepted_outline_replaces_the_house_structure(monkeypatch):
    monkeypatch.setattr(proposal_writer, "tender_documents_block", lambda *a, **k: "")
    monkeypatch.setattr(proposal_writer, "build_tor_brief", lambda *a, **k: "")
    monkeypatch.setattr(proposal_writer, "extract_document_lock", lambda *a, **k: {})
    monkeypatch.setattr(proposal_writer, "format_document_lock", lambda *a, **k: "")
    monkeypatch.setattr(
        proposal_writer,
        "plan_draft_outline",
        lambda *a, **k: {"prescribed": False, "sections": []},
    )
    monkeypatch.setattr(proposal_writer, "build_win_strategy", lambda *a, **k: "")
    monkeypatch.setattr(proposal_writer, "_build_guidance_block", lambda *a, **k: "guidance")
    monkeypatch.setattr(proposal_writer, "_build_untrusted_context_block", lambda *a, **k: "")
    accepted = customize_outline("FULL_PROPOSAL", [
        {"heading": "Suitability statement", "page_limit": "2 pages"},
        {"heading": "Work plan"},
    ])
    blocks = proposal_writer.build_system_blocks({}, accepted_outline=accepted)
    meta = next(block for block in blocks if block.get("type") == "meta")
    outline = meta["submission_outline"]
    assert outline["prescribed"] is True
    assert [item["heading"] for item in outline["sections"]] == [
        "Suitability statement",
        "Work plan",
    ]


def test_recommendation_enriches_chapters_with_tor_criteria_and_deliverables():
    analysis = _analysis(sections=["Methodology", "Scope of deliverables"])
    analysis["evaluation_criteria"] = [
        {"criterion": "Mixed-methods survey design", "description": "Household sampling in Gedo"},
    ]
    analysis["deliverables"] = [
        {"name": "Inception Report", "description": "Tools and sampling frame within 14 days"},
    ]
    outline = recommend_outline(analysis)
    by_route = {item["route"]: item for item in outline["sections"]}
    assert any("Mixed-methods survey design" in line for line in by_route["methodology"]["must_include"])
    assert any("Inception Report" in line for line in by_route["deliverables"]["must_include"])


def test_house_writer_receives_must_include_rules_from_customized_outline():
    rules = proposal_writer._draft_rules_for({
        "heading": "Methodology",
        "page_limit": "5 pages",
        "must_include": ["Stratified household sampling in Beletweyne", "KoboToolbox CAPI forms"],
    })
    assert "BINDING CHAPTER SCOPE" in rules
    assert "Stratified household sampling in Beletweyne" in rules
    assert "KoboToolbox CAPI forms" in rules

