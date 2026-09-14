"""Integration-shaped tests for the orchestrator with all external systems mocked."""

import main


def _stub_client_intelligence(monkeypatch):
    monkeypatch.setattr(
        main,
        "build_client_intelligence",
        lambda **kwargs: {
            "client": {
                "match": {
                    "status": "UNKNOWN",
                    "method": "new_candidate",
                    "canonical_name": kwargs.get("client") or "",
                },
                "headline": (
                    "Cortech has bid on 0 opportunities from this client before "
                    "(no matching past opportunity or proposal records in the observed store)."
                ),
                "outcome_note": "There are no stored outcomes to count.",
                "citations": [],
                "won": 0,
                "lost": 0,
                "unknown_outcomes": 0,
                "opportunities_before": 0,
            },
            "donor": None,
            "same_org": False,
            "storage": "test",
        },
    )


def _high_fit_analysis():
    return {
        "opportunity": {
            "title": "Somalia MEL Endline Evaluation",
            "client": "UNICEF",
            "donor": "UNICEF",
            "submission_deadline": "2099-12-01",
            "project_location": ["Somalia"],
            "estimated_budget_usd": 80000,
        },
        "requirements": {
            "thematic_areas": ["evaluation", "MEL"],
            "language_requirements": ["English", "Somali"],
            "certifications": ["PSEA policy"],
        },
        "team_requirements": [],
        "submission_requirements": {"cvs_required": False, "financial_proposal_required": True},
        "evaluation_criteria": [{"criterion": "Methodology", "weight_percent": 40}],
        "bid_analysis": {
            "is_consultancy_contract": True,
            "submission_type": "FULL_PROPOSAL",
            "cortech_fit_score": 1,
            "win_probability": 1,
            "bid_recommendation": "NO-BID",
            "key_strengths": [],
            "key_gaps": [],
        },
    }


def test_process_opportunity_runs_verified_vertical_slice(monkeypatch):
    updates = []
    stored = []
    monkeypatch.setattr(main, "fetch_and_extract", lambda *args, **kwargs: "Terms of Reference " * 30)
    monkeypatch.setattr(main, "claim_opportunity_processing", lambda *args, **kwargs: "claim-1")
    monkeypatch.setattr(main, "complete_opportunity_processing", lambda *args, **kwargs: True)
    monkeypatch.setattr(main, "fail_opportunity_processing", lambda *args, **kwargs: True)
    monkeypatch.setattr(main, "store_opportunity", lambda *args: stored.append(args) or "cache-id")
    monkeypatch.setattr(main, "analyze_rfp", lambda *args, **kwargs: _high_fit_analysis())
    monkeypatch.setattr(main, "create_opportunity", lambda payload: "airtable-id")
    monkeypatch.setattr(main, "update_opportunity", lambda record_id, fields: updates.append((record_id, fields)))
    monkeypatch.setattr(
        main,
        "calculate_budget",
        lambda *args, **kwargs: {
            "status": "INSUFFICIENT DATA",
            "reason": "No verified rate card inputs.",
            "missing_inputs": ["rate card"],
            "summary": {"known_personnel_subtotal_usd": 0},
        },
    )
    monkeypatch.setattr(main, "generate_proposal", lambda *args, **kwargs: {
        "cover_letter": "Evidence-bound draft section",
        "executive_summary": "Grounded in the tender.",
    })
    monkeypatch.setattr(main, "find_opportunity_by_content_hash", lambda *args, **kwargs: None)
    _stub_client_intelligence(monkeypatch)

    result = main.process_opportunity({
        "title": "Listing title",
        "source_url": "https://procurement.example/tender?utm_source=newsletter",
        "source_portal": "Fixture",
    })

    assert result is not None
    assert result["recommendation"] == "BID"  # not the LLM's NO-BID audit value
    assert result["budget"]["status"] == "INSUFFICIENT DATA"
    assert stored[0][0] == "https://procurement.example/tender"
    assert any(fields.get("status") == "Reviewing" for _, fields in updates)
    assert any(row["requirement"].startswith("Financial") for row in result["compliance_matrix"])


def test_no_bid_is_recorded_for_human_review_not_as_final_status(monkeypatch):
    saved = []
    analysis = _high_fit_analysis()
    analysis["opportunity"].update({"client": "Unknown Corp", "donor": "" , "project_location": ["Brazil"]})
    analysis["requirements"] = {
        "thematic_areas": ["extractive mining geology"],
        "language_requirements": [],
        "certifications": [],
    }
    monkeypatch.setattr(main, "fetch_and_extract", lambda *args, **kwargs: "Terms of Reference " * 30)
    monkeypatch.setattr(main, "claim_opportunity_processing", lambda *args, **kwargs: "claim-1")
    monkeypatch.setattr(main, "complete_opportunity_processing", lambda *args, **kwargs: True)
    monkeypatch.setattr(main, "fail_opportunity_processing", lambda *args, **kwargs: True)
    monkeypatch.setattr(main, "store_opportunity", lambda *args: "cache-id")
    monkeypatch.setattr(main, "analyze_rfp", lambda *args, **kwargs: analysis)
    monkeypatch.setattr(main, "create_opportunity", lambda payload: saved.append(payload) or "airtable-id")
    monkeypatch.setattr(main, "find_opportunity_by_content_hash", lambda *args, **kwargs: None)
    _stub_client_intelligence(monkeypatch)

    result = main.process_opportunity({
        "title": "Mining study",
        "source_url": "https://procurement.example/mining",
        "source_portal": "Fixture",
    })

    assert result is None
    assert saved[0]["bid_recommendation"] == "NO-BID"
    assert saved[0]["status"] == "New"
