from intelligence.grounding import (
    annotate_unverified,
    build_evidence_chunks,
    extract_claims,
    ground_sections,
)


def _chunks():
    return build_evidence_chunks(
        past_matches=[{
            "project_title": "Endline Evaluation Water and Livelihoods Somalia",
            "airtable_proposal_id": "recARCH",
            "content_chunk": "Cortech delivered the WASH livelihoods endline for Arche Nova in Somalia.",
            "metadata": {"client": "Arche Nova", "year": 2025, "location": ["Somalia"]},
            "similarity": 0.81,
            "won": True,
        }],
        matched_team_result={
            "matched_team": {
                "Team Lead": {
                    "consultant_name": "Amina Hassan",
                    "airtable_consultant_id": "rec1",
                    "capability": {"why": ["Somalia MEL"], "evidence": ["cv:rec1"]},
                }
            }
        },
        static_past_work="Green Skills Documentation — Client: GREDO/DANIDA/Save the Children",
        analysis={
            "opportunity": {
                "title": "Catalyzing Energy Access Evaluation",
                "client": "Christian Aid",
            }
        },
    )


def test_named_past_client_in_chunk_is_verified():
    sections = {
        "org_profile_and_track_record": (
            "Cortech previously delivered an endline evaluation for Arche Nova "
            "covering WASH livelihoods in Somalia."
        )
    }
    claims = extract_claims(sections, _chunks())
    verified = [c for c in claims if c["status"] == "VERIFIED"]
    assert verified
    assert verified[0]["chunk_id"] == "proposal:recARCH"
    assert verified[0]["source"] == "proposal_embeddings"


def test_invented_past_client_is_not_verified_and_annotated():
    sections = {
        "org_profile_and_track_record": (
            "Cortech previously delivered a national energy evaluation for "
            "Acme International in 2019."
        )
    }
    result = ground_sections(
        sections,
        {"opportunity": {"title": "Energy evaluation", "client": "Christian Aid"}},
        None,
        past_matches=[{
            "project_title": "WASH Endline Somalia",
            "content_chunk": "Arche Nova WASH endline",
            "metadata": {"client": "Arche Nova"},
        }],
        static_past_work="",
    )
    claims = result["claim_grounding"]["claims"]
    assert any(c["status"] == "NOT VERIFIED" for c in claims)
    assert "[NOT VERIFIED]" in result["org_profile_and_track_record"]
    assert "Acme International" in result["org_profile_and_track_record"]


def test_generic_track_record_without_entity_is_insufficient_not_rewritten():
    text = "Cortech has a strong track record in mixed-methods evaluation."
    sections = {"executive_summary": text}
    result = ground_sections(
        sections,
        {"opportunity": {"title": "X", "client": "Y"}},
        None,
        past_matches=[],
        static_past_work="",
    )
    claims = result["claim_grounding"]["claims"]
    assert any(c["status"] == "INSUFFICIENT EVIDENCE" for c in claims)
    assert "[NOT VERIFIED]" not in result["executive_summary"]
    assert result["executive_summary"] == text


def test_current_tender_client_is_not_treated_as_invented_past_work():
    sections = {
        "cover_letter": (
            "This proposal is submitted to Christian Aid for the energy access evaluation."
        )
    }
    claims = extract_claims(sections, _chunks())
    assert not any(c["status"] == "NOT VERIFIED" for c in claims)


def test_annotate_does_not_double_tag():
    claims = [{
        "section": "org_profile_and_track_record",
        "sentence": "Cortech previously worked with Acme International.",
        "status": "NOT VERIFIED",
    }]
    once = annotate_unverified(
        {"org_profile_and_track_record": "Cortech previously worked with Acme International."},
        claims,
    )
    twice = annotate_unverified(once, claims)
    assert twice["org_profile_and_track_record"].count("[NOT VERIFIED]") == 1
