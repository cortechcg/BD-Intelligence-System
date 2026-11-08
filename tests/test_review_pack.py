from intelligence import extraction_provenance, review_pack


def test_requirement_rows_retain_source_passage_and_page_reference():
    analysis = {
        "evaluation_criteria": [{
            "criterion": "Methodology and analysis",
            "description": "Sampling and analysis framework",
            "weight_percent": 60,
        }],
        "submission_requirements": {
            "prescribed_proposal_sections": ["Methodology"],
            "required_annexes": [],
            "disqualifying_conditions": [],
        },
    }
    source = (
        "===== SOURCE FILE: tender.pdf =====\n"
        "----- PAGE 7 -----\n"
        "Methodology and analysis will be scored at 60 percent. Sampling and analysis framework.\n"
        "Methodology"
    )
    extraction_provenance.attach_extraction_provenance(analysis, source)
    sections = {
        "methodology": "## Methodology\n\nMethodology and analysis. Sampling and analysis framework. "
        "This section describes the sample, survey tools, analytical framework, safeguards, and validation steps."
    }
    pack = review_pack.build_review_pack(analysis, sections)
    criterion = next(row for row in pack["requirement_matrix"] if row["kind"] == "evaluation_criterion")
    assert criterion["source_evidence"]["status"] == "VERIFIED"
    assert criterion["source_evidence"]["page"] == 7
    assert criterion["source_evidence"]["source_file"] == "tender.pdf"


def test_release_approval_requires_separate_human_attestations(monkeypatch):
    monkeypatch.setattr(review_pack.quality_gate, "score_draft", lambda *_a, **_kw: {
        "status": "READY", "score": 100, "blockers": [],
    })
    analysis = {"submission_requirements": {"required_annexes": ["Signed declaration"]}}
    sections = {"forms": "Signed declaration will accompany the proposal."}
    without = review_pack.build_review_pack(analysis, sections)
    assert without["release_eligible"] is True
    assert without["release_ready"] is False
    assert without["needs_attachment_ack"] is True

    approved = review_pack.build_review_pack(analysis, sections, acknowledgements={
        "attachments_verified": True,
        "submission_checked": True,
        "final_document_checked": True,
    })
    assert approved["release_ready"] is True


def test_duplicate_consultant_identity_is_a_hard_team_conflict(monkeypatch):
    monkeypatch.setattr(review_pack.quality_gate, "score_draft", lambda *_a, **_kw: {
        "status": "READY", "score": 100, "blockers": [],
    })
    analysis = {"team_requirements": [{"role": "Team Leader"}, {"role": "Evaluator"}]}
    person = {
        "consultant_name": "A. Consultant", "airtable_consultant_id": "person-1",
        "capability": {"status": "SATISFIED", "evidence": ["Evaluation CV, page 2"]},
        "all_matches": [],
    }
    team = {"matched_team": {"Team Leader": dict(person), "Evaluator": dict(person)}}
    pack = review_pack.build_review_pack(analysis, {"methodology": "Draft."}, team, acknowledgements={
        "submission_checked": True, "final_document_checked": True,
    })
    assert any(x["kind"] == "concurrent_role_conflict" for x in pack["hard_compliance_failures"])
    assert pack["release_ready"] is False


def test_draft_fingerprint_changes_when_evidence_or_text_changes():
    base = review_pack.draft_fingerprint({"title": "Tender"}, {"method": "Plan A"}, {}, "evidence-1")
    assert review_pack.draft_fingerprint({"title": "Tender"}, {"method": "Plan A"}, {}, "evidence-2") != base
    assert review_pack.draft_fingerprint({"title": "Tender"}, {"method": "Plan B"}, {}, "evidence-1") != base


def test_internal_rubric_uses_tender_weights_and_team_evidence_gaps():
    analysis = {
        "evaluation_criteria": [
            {"criterion": "Methodology and analysis", "description": "Sampling design and analytic framework", "weight_percent": 60},
            {"criterion": "Team qualifications", "description": "Qualified team leader", "weight_percent": 40},
        ],
        "team_requirements": [{"role": "Team leader"}],
    }
    sections = {
        "methodology": (
            "Our methodology and analysis use a stratified sampling design, validated survey tools, "
            "a prespecified analytic framework, field supervision, data quality checks, triangulation, "
            "and a transparent limitations review before findings are finalized for the client."
        ),
        "team": "The team will coordinate fieldwork and submit the report.",
    }
    pack = review_pack.build_review_pack(analysis, sections, {})
    assessment = pack["committee_assessment"]
    assert "not an official procurement committee score" in assessment["label"].lower()
    assert assessment["source"] == "tender_rubric"
    assert assessment["weighted_total"]["maximum"] == 100
    team_row = next(row for row in assessment["criteria"] if row["criterion"].startswith("Team qualifications"))
    assert team_row["maximum_score"] == 40
    assert team_row["awarded_score"] == 0
    assert team_row["gaps"]


def test_missing_tender_weights_are_not_given_invented_maxima():
    pack = review_pack.build_review_pack(
        {"evaluation_criteria": [{"criterion": "Approach", "weight_percent": None}]},
        {"approach": "The approach describes the methodology and implementation plan."},
    )
    assessment = pack["committee_assessment"]
    assert assessment["source"] == "tender_rubric"
    assert assessment["weighted_total"] is None
    assert assessment["criteria"][0]["maximum_score"] is None
    assert assessment["criteria"][0]["awarded_score"] is None
    assert assessment["missing_weights"] == ["Approach"]
