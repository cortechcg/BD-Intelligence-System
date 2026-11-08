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
