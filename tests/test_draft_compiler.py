"""The draft compiler owns order and repetition. No model calls."""
import intelligence.proposal_writer as proposal_writer
from intelligence.draft_compiler import (
    brief_for_section,
    build_claim_sheet,
    chapters_owned_by_siblings,
    compile_client_draft,
)
from intelligence.tender_reader import plan_draft_outline


def test_scope_of_deliverables_is_not_a_second_methodology():
    outline = plan_draft_outline({
        "format_prescribed": True,
        "prescribed_sections": [
            "Scope of deliverables",
            "Methodology",
        ],
    })
    routes = {item["heading"]: item["route"] for item in outline["sections"]}
    assert routes["Scope of deliverables"] == "deliverables"
    assert routes["Methodology"] == "methodology"


def test_envelope_skips_a_methodology_a_sibling_heading_already_owns(monkeypatch):
    called = []
    monkeypatch.setattr(
        proposal_writer,
        "generate_methodology",
        lambda *_a, **_k: called.append("method") or "METHOD",
    )
    monkeypatch.setattr(
        proposal_writer,
        "generate_introduction_and_framework",
        lambda *_a, **_k: "INTRO",
    )
    monkeypatch.setattr(proposal_writer, "generate_analysis_plan", lambda *_a, **_k: "ANALYSIS")
    monkeypatch.setattr(proposal_writer, "generate_qa_and_ethics", lambda *_a, **_k: "QA")
    monkeypatch.setattr(proposal_writer, "generate_risk_register", lambda *_a, **_k: "RISK")
    monkeypatch.setattr(proposal_writer, "generate_work_plan", lambda *_a, **_k: "PLAN")
    monkeypatch.setattr(proposal_writer, "generate_team_section", lambda *_a, **_k: "TEAM")
    text = proposal_writer.generate_technical_proposal_body(
        {"opportunity": {"title": "Gedo survey"}},
        [],
        item={"heading": "Technical proposal"},
        sibling_routes=["generic"],
        sibling_headings=["Proposed methodology"],
    )
    assert called == []
    assert "METHOD" not in text
    assert "INTRO" in text


def test_a_sibling_methodology_heading_is_not_written_again_inside_the_envelope():
    owned = chapters_owned_by_siblings(
        ["generic"],
        ["Proposed methodology"],
    )
    assert "methodology" in owned
    assert "approach_summary" in owned


def test_each_criterion_has_one_owner():
    sheet = build_claim_sheet(
        {
            "opportunity": {"title": "Gedo survey", "client": "DRC", "project_location": ["Gedo"]},
            "deliverables": [{"name": "Baseline report", "description": "Household survey in Gedo"}],
            "evaluation_criteria": [
                {"criterion": "Methodology", "weight_percent": 40, "description": "Survey design"},
            ],
        },
        {
            "sections": [
                {"key": "cover_letter", "heading": "Cover letter", "route": "cover_letter"},
                {"key": "methodology", "heading": "Methodology", "route": "methodology"},
                {"key": "deliverables", "heading": "Scope of deliverables", "route": "deliverables"},
            ]
        },
    )
    owners = {}
    for claim in sheet["claims"]:
        owners.setdefault(claim["text"], set()).add(claim["owner"])
    assert all(len(owner) == 1 for owner in owners.values())
    method = [claim for claim in sheet["claims"] if claim["owner"] == "methodology"]
    scope = [claim for claim in sheet["claims"] if claim["owner"] == "deliverables"]
    assert any("Survey design" in claim["text"] for claim in method)
    assert any("Baseline report" in claim["text"] for claim in scope)
    brief = brief_for_section(sheet, "deliverables")
    assert "Baseline report" in brief
    assert "Methodology" in brief
    assert "do not write them again" in brief.lower() or "OTHER CHAPTERS" in brief


def test_cover_letter_moves_to_the_front_and_a_repeat_is_dropped():
    repeated = (
        "The assignment surveys households in Gedo using the client's own "
        "sampling frame and reports against the baseline indicators."
    )
    sections = {
        "methodology": repeated + "\n\nThe method chapter adds the instrument list.",
        "scope_of_deliverables": (
            "Scope owns the products.\n\n" + repeated
        ),
        "cover_letter": "Dear procurement committee, Cortech submits this proposal.",
        "executive_summary": "This summary states the Gedo assignment in one page.",
        "section_order": [
            ("methodology", "Methodology"),
            ("scope_of_deliverables", "Scope of deliverables"),
            ("executive_summary", "Executive summary"),
            ("cover_letter", "Cover letter"),
        ],
        "submission_outline": {
            "sections": [
                {"key": "methodology", "heading": "Methodology", "route": "methodology"},
                {"key": "scope_of_deliverables", "heading": "Scope of deliverables", "route": "deliverables"},
                {"key": "executive_summary", "heading": "Executive summary", "route": "executive_summary"},
                {"key": "cover_letter", "heading": "Cover letter", "route": "cover_letter"},
            ]
        },
    }
    compiled = compile_client_draft(sections)
    order = [key for key, _heading in compiled["section_order"]]
    assert order[:2] == ["cover_letter", "executive_summary"]
    assert repeated not in compiled["scope_of_deliverables"]
    assert repeated in compiled["methodology"]
    assert compiled["draft_editor"]["repeated_passages_removed"] >= 1


def test_a_methodology_heading_inside_another_chapter_is_removed():
    sections = {
        "methodology": "The method is a household survey in Gedo.",
        "technical_proposal": (
            "Opening for the envelope.\n\n"
            "## Methodology and tools\n\n"
            "This whole methodology is repeated here with the same Gedo survey design.\n\n"
            "## Work plan\n\n"
            "Week one is inception."
        ),
        "section_order": [
            ("technical_proposal", "Technical proposal"),
            ("methodology", "Methodology"),
        ],
    }
    compiled = compile_client_draft(sections)
    assert "## Methodology" not in compiled["technical_proposal"]
    assert "Week one is inception." in compiled["technical_proposal"]
    assert compiled["draft_editor"]["nested_chapters_removed"] >= 1


def test_a_letter_buried_in_the_last_chapter_is_removed():
    sections = {
        "cover_letter": "Dear Sir, please find our proposal.",
        "work_plan": (
            "Week 1 inception. Week 2 fieldwork in Gedo.\n\n"
            "Dear Sir,\n\n"
            "Yours sincerely,\n\n"
            "Daud Hussein Ibrahim, Director"
        ),
        "section_order": [
            ("cover_letter", "Cover letter"),
            ("work_plan", "Work plan"),
        ],
    }
    compiled = compile_client_draft(sections)
    assert "Yours sincerely" not in compiled["work_plan"]
    assert "Week 1 inception" in compiled["work_plan"]
    assert "Dear Sir" in compiled["cover_letter"]
