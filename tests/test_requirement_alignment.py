import pytest

from intelligence.proposal_writer import DraftingError
from intelligence.requirement_alignment import (
    align_draft_to_requirements,
    criteria_alignment_percent,
    fail_closed_requirement_alignment,
    meets_evaluation_bar,
)
from reporting import email_report


def _analysis():
    return {
        "opportunity": {"submission_deadline": "2099-12-01"},
        "evaluation_criteria": [
            {
                "criterion": "Household registration in Beletweyne",
                "weight_percent": 40,
                "description": "The bidder must explain registration of households in Beletweyne lot 1.",
            },
            {
                "criterion": "Nutrition survey experience in Gedo",
                "weight_percent": 30,
                "description": "Named experience of nutrition surveys in Gedo.",
            },
        ],
        "submission_requirements": {
            "prescribed_proposal_sections": ["Technical methodology"],
            "technical_proposal_page_limit": 20,
            "cvs_required": True,
            "financial_proposal_required": True,
            "required_annexes": ["Annex B registration form"],
            "disqualifying_conditions": [
                "Bids received after the deadline will be rejected."
            ],
        },
    }


def _sections():
    methodology = (
        "The team will register households in Beletweyne lot 1 using the "
        "client's household register. Enumerators will cover every listed "
        "settlement in that lot before the field report is written. "
        + ("Field notes continue. " * 30)
    )
    return {
        "section_order": [
            {"key": "methodology", "heading": "Technical methodology"},
        ],
        "methodology": methodology,
        "cover_letter": "We will submit this file by 2099-12-01.",
    }


def test_criterion_is_addressed_only_when_the_sentence_is_in_the_section():
    report = align_draft_to_requirements(_analysis(), _sections())
    rows = {row["id"]: row for row in report["rows"]}
    hit = rows["evaluation_criterion:0"]
    assert hit["status"] == "addressed"
    assert hit["section"] == "methodology"
    assert hit["quote"] in _sections()["methodology"]
    assert "Beletweyne" in hit["quote"]
    assert report["criteria_addressed"] == 1
    assert report["criteria_total"] == 2
    assert "Nutrition survey experience in Gedo" in report["headline"]


def test_unmentioned_criterion_is_not_treated_as_satisfied():
    report = align_draft_to_requirements(_analysis(), _sections())
    rows = {row["id"]: row for row in report["rows"]}
    missed = rows["evaluation_criterion:1"]
    assert missed["status"] == "not_addressed"
    assert missed["section"] == ""
    assert missed["quote"] == ""


def test_naming_a_criterion_is_not_a_scored_answer():
    sections = _sections()
    sections["methodology"] = "Household registration in Beletweyne."
    report = align_draft_to_requirements(_analysis(), sections)
    hit = next(row for row in report["rows"] if row["id"] == "evaluation_criterion:0")
    assert hit["status"] == "partially_addressed"
    assert report["meets_criteria_bar"] is False


def test_partial_match_is_not_counted_as_addressed():
    sections = _sections()
    sections["methodology"] = (
        "The field team will work in Beletweyne on listing tasks. "
        + ("Notes continue here. " * 20)
    )
    report = align_draft_to_requirements(_analysis(), sections)
    hit = next(row for row in report["rows"] if row["id"] == "evaluation_criterion:0")
    assert hit["status"] == "partially_addressed"
    assert hit["quote"] in sections["methodology"]
    assert report["criteria_addressed"] == 0


def test_prescribed_heading_traces_to_that_section_and_annex_does_not():
    report = align_draft_to_requirements(_analysis(), _sections())
    rows = {row["label"]: row for row in report["rows"]}
    prescribed = rows["Technical methodology"]
    assert prescribed["status"] == "addressed"
    assert prescribed["section"] == "methodology"
    assert prescribed["quote"] in _sections()["methodology"]
    annex = rows["Annex B registration form"]
    assert annex["status"] == "not_addressed"
    assert annex["quote"] == ""


def test_disqualifier_and_financial_are_never_assumed_satisfied():
    sections = _sections()
    sections["methodology"] += " Bids received after the deadline will be rejected."
    report = align_draft_to_requirements(_analysis(), sections)
    rows = {row["id"]: row for row in report["rows"]}
    assert rows["disqualifier:0"]["status"] == "not_addressed"
    assert rows["financial"]["status"] == "not_addressed"
    assert rows["deadline"]["status"] == "addressed"
    assert rows["deadline"]["quote"] in sections["cover_letter"]


def test_empty_draft_fails_closed():
    report = align_draft_to_requirements(_analysis(), {})
    assert report["addressed"] == 0
    assert report["criteria_addressed"] == 0
    assert all(row["status"] == "not_addressed" for row in report["rows"])
    assert report["rows"]


def test_tracer_error_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "intelligence.requirement_alignment.collect_requirements",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    report = align_draft_to_requirements(_analysis(), _sections())
    assert report["addressed"] == 0
    assert report["error"]
    assert "satisfied" in report["headline"].lower() or report["criteria_addressed"] == 0


def test_fail_closed_helper_does_not_mark_rows_addressed():
    report = fail_closed_requirement_alignment(_analysis(), error="verifier down")
    assert report["error"] == "verifier down"
    assert report["addressed"] == 0
    assert all(row["status"] == "not_addressed" and row["quote"] == "" for row in report["rows"])


def test_review_email_shows_the_alignment_headline(monkeypatch):
    sent = {}
    monkeypatch.setattr(email_report, "build_proposal_docx", lambda *args: None)
    monkeypatch.setattr(
        email_report,
        "_send_email",
        lambda subject, content, attachment_path=None: sent.update(content=content) or True,
    )
    analysis = _analysis()
    sections = _sections()
    sections["requirement_alignment"] = align_draft_to_requirements(analysis, sections)
    email_report.send_proposal_email({
        "title": "Beletweyne registration",
        "client": "Client",
        "deadline": "2099-12-01",
        "score": 70,
        "recommendation": "BID",
        "source_url": "https://procurement.example/tender",
        "analysis": analysis,
        "matched_team": {"matched_team": {}},
        "budget": {"status": "INSUFFICIENT DATA", "reason": "no rates"},
        "proposal_sections": sections,
    })
    assert "This draft addresses 1 of 2 evaluation criteria" in sent["content"]
    assert "Nutrition survey experience in Gedo" in sent["content"]
    assert "Requirement alignment" in sent["content"]


def test_description_overlap_does_not_count_as_addressed():
    analysis = {
        "evaluation_criteria": [{
            "criterion": "Is sampling criteria and sample size adequate?",
            "weight_percent": 20,
            "description": "Appropriateness of the mixed-methods design and outcome harvesting.",
        }],
    }
    sections = {
        "previous_related_experience": (
            "The earlier assignment used a mixed-methods design and outcome harvesting "
            "across pastoralist communities in Turkana."
        ),
    }
    report = align_draft_to_requirements(analysis, sections)
    row = report["rows"][0]
    assert row["status"] == "partially_addressed"
    assert row["section"] == "previous_related_experience"
    assert row["quote"] in sections["previous_related_experience"]
    assert report["criteria_addressed"] == 0


def test_outline_headings_are_what_gets_traced():
    analysis = {
        "submission_requirements": {
            "prescribed_proposal_sections": [
                "Technical proposal summarizing experience, understanding, and methodology"
            ],
        },
    }
    body = (
        "Understanding of the TOR starts from the learning paper on integration "
        "in cross-border counties. "
    ) * 8
    sections = {
        "section_order": [["understanding_of_the_tor", "Understanding of the TOR"]],
        "understanding_of_the_tor": body,
    }
    outline = {
        "prescribed": True,
        "sections": [{"key": "understanding_of_the_tor", "heading": "Understanding of the TOR"}],
    }
    report = align_draft_to_requirements(analysis, sections, outline=outline)
    labels = [row["label"] for row in report["rows"]]
    assert "Understanding of the TOR" in labels
    assert not any(label.startswith("Technical proposal summarizing") for label in labels)
    hit = next(row for row in report["rows"] if row["label"] == "Understanding of the TOR")
    assert hit["status"] == "addressed"
    assert hit["quote"] in body


def test_a_mistitled_body_is_not_treated_as_the_required_chapter():
    outline = {
        "prescribed": True,
        "sections": [{
            "key": "contacts",
            "heading": "Contact details of three organisations",
        }],
    }
    sections = {
        "section_order": [["contacts", "Contact details of three organisations"]],
        "contacts": (
            "Contact details of three organisations\n\n"
            + "The firm was established in Nairobi and works on evaluations across the region. " * 8
        ),
    }
    report = align_draft_to_requirements({}, sections, outline=outline)
    row = report["rows"][0]
    assert row["status"] == "partially_addressed"
    assert row["quote"] in sections["contacts"]


def test_financial_requirement_is_not_collapsed_into_an_annex_label():
    analysis = {
        "submission_requirements": {
            "financial_proposal_required": True,
            "required_annexes": ["Itemized financial proposal"],
        },
    }
    report = align_draft_to_requirements(analysis, {"cover_letter": "A cover note only."})
    labels = [row["label"] for row in report["rows"]]
    assert "Financial proposal" in labels
    assert "Itemized financial proposal" in labels


def test_fifty_page_tor_is_not_cut_before_the_scoring_section():
    from config import MAX_EXTRACTED_TEXT_CHARS
    from intelligence.tender_reader import MAX_TENDER_CHARS, pack_tender_text

    page = "Scope of work paragraph. " * 140
    pages = [f"----- PAGE {index} -----\n{page}" for index in range(1, 51)]
    pages[40] = (
        "----- PAGE 41 -----\nEVALUATION CRITERIA\n\n"
        "Technical approach is weighted 40 percent of the score."
    )
    text = "\n\n".join(pages)
    assert len(text) > 120_000
    assert len(text) <= MAX_EXTRACTED_TEXT_CHARS
    assert len(text) <= MAX_TENDER_CHARS
    assert "40 percent" not in text[:120_000]
    packed = pack_tender_text(text)
    assert "40 percent" in packed
    assert len(packed) == len(text.strip()) or "40 percent" in packed


def test_alignment_score_uses_criterion_weights():
    report = align_draft_to_requirements(_analysis(), _sections())
    # 40% addressed, 30% missing → 40/70.
    assert report["criteria_score_percent"] == 57.1
    assert report["meets_criteria_bar"] is False
    assert "Alignment score 57.1%" in report["headline"]

    both = _sections()
    both["methodology"] += (
        " The firm offers nutrition survey experience in Gedo from a completed "
        "assignment in that region."
    )
    passed = align_draft_to_requirements(_analysis(), both)
    assert passed["criteria_score_percent"] == 100.0
    assert meets_evaluation_bar(passed["criteria_score_percent"]) is True


def test_partial_credit_is_half_the_weight():
    sections = _sections()
    sections["methodology"] = (
        "The field team will work in Beletweyne on listing tasks. "
        + ("Notes continue here. " * 20)
    )
    report = align_draft_to_requirements(_analysis(), sections)
    # Only the 40% criterion is partial. 20/70.
    assert report["criteria_score_percent"] == 28.6
    assert criteria_alignment_percent(report["rows"]) == 28.6


def test_equal_weights_when_any_criterion_has_no_weight():
    rows = [
        {"kind": "evaluation_criterion", "status": "addressed", "weight_percent": 40},
        {"kind": "evaluation_criterion", "status": "addressed"},
        {"kind": "evaluation_criterion", "status": "addressed", "weight_percent": 20},
        {"kind": "evaluation_criterion", "status": "not_addressed", "weight_percent": 10},
        {"kind": "evaluation_criterion", "status": "addressed", "weight_percent": 10},
    ]
    # One weight is missing, so each criterion counts once: 4/5 = 80.
    assert criteria_alignment_percent(rows) == 80.0
    assert meets_evaluation_bar(80.0) is True
    assert meets_evaluation_bar(79.9) is False


def test_draft_is_rewritten_until_criteria_alignment_clears_80(monkeypatch):
    from intelligence import proposal_writer

    sections = _sections()
    calls = []

    def rewrite(stage, model, tokens, blocks, prompt):
        calls.append(stage)
        return (
            sections["methodology"]
            + " The firm offers nutrition survey experience in Gedo from a "
            "completed assignment in that region."
        )

    monkeypatch.setattr(proposal_writer, "_generate_section", rewrite)
    raised = proposal_writer._raise_to_criteria_bar(sections, _analysis(), [])
    assert calls == ["methodology_criteria_repair"]
    report = align_draft_to_requirements(_analysis(), raised)
    assert report["criteria_score_percent"] >= 80
    assert report["meets_criteria_bar"] is True


def test_draft_below_80_after_repair_is_not_returned(monkeypatch):
    from intelligence import proposal_writer

    monkeypatch.setattr(
        proposal_writer,
        "_generate_section",
        lambda *args, **kwargs: "The team will continue the same field plan. " * 8,
    )
    with pytest.raises(DraftingError, match="80%"):
        proposal_writer._raise_to_criteria_bar(_sections(), _analysis(), [])


def test_missing_criteria_does_not_invent_a_passing_score():
    from intelligence import proposal_writer

    sections = {"methodology": "A complete methods chapter for this assignment. " * 12}
    returned = proposal_writer._raise_to_criteria_bar(sections, {}, [])
    assert returned["methodology"].startswith("A complete methods")
    report = align_draft_to_requirements({}, returned)
    assert report["criteria_score_percent"] is None
    assert report["meets_criteria_bar"] is False
