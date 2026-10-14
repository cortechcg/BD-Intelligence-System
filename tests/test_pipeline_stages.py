"""Unit tests for pipeline stage services. No live LLM/Airtable/Supabase."""

from __future__ import annotations

import pytest

from intelligence.pipeline_stages import (
    MAX_DRAFT_FAILURES,
    PIPELINE_STAGES,
    ProcessingSnapshot,
    RetryableStageError,
    StageDeps,
    TerminalSkip,
    context_from_raw,
    run_draft_stage,
    run_extract_stage,
    run_opportunity_pipeline,
    run_score_stage,
)
from intelligence.analyzer import analyze_rfp
from intelligence.proposal_writer import (
    DraftingError,
    assert_usable_client_draft,
    draft_has_client_facing_prose,
)


def _analysis(*, consultancy=True, recommendation="BID", missing_consultancy=False):
    bid = {
        "submission_type": "FULL_PROPOSAL",
        "cortech_fit_score": 80,
        "win_probability": 70,
        "bid_recommendation": recommendation,
        "key_strengths": ["fit"],
        "key_gaps": [],
        "rationale": "test",
    }
    if not missing_consultancy:
        bid["is_consultancy_contract"] = consultancy
    return {
        "opportunity": {
            "title": "Somalia MEL Endline",
            "client": "UNICEF",
            "donor": "UNICEF",
            "submission_deadline": "2099-12-01",
            "project_location": ["Somalia"],
            "estimated_budget_usd": 80000,
        },
        "requirements": {"thematic_areas": ["evaluation"], "language_requirements": ["English"]},
        "team_requirements": [],
        "bid_analysis": bid,
        "bid_intelligence": {"score_version": "1.0.0"},
    }


def _deps(**overrides):
    defaults = dict(
        fetch_and_extract=lambda *a, **k: "Terms of Reference " * 40,
        analyze_rfp=lambda *a, **k: _analysis(),
        apply_bid_intelligence=lambda analysis, team=None: analysis,
        find_opportunity_by_content_hash=lambda digest: None,
        create_opportunity=lambda payload: "rec-1",
        update_opportunity=lambda *a, **k: None,
        match_team_to_requirements=lambda *a, **k: {
            "matched_team": {}, "gaps": [], "coverage_percent": 0,
        },
        calculate_budget=lambda *a, **k: {"status": "INSUFFICIENT DATA"},
        draft_bid_or_watch_proposal=lambda *a, **k: {
            "cover_letter": "A complete client-facing cover letter for review."
        },
        build_compliance_matrix=lambda *a, **k: [],
        save_draft_memory=lambda **k: None,
        build_client_intelligence=lambda **k: {"storage": "test"},
        persist_stage=lambda *a, **k: True,
        log_stage=lambda *a, **k: None,
    )
    defaults.update(overrides)
    return StageDeps(**defaults)


def test_pipeline_stage_names_are_exact():
    assert PIPELINE_STAGES == (
        "discovered", "extracted", "scored", "drafted", "reviewed", "outcome",
    )
    assert MAX_DRAFT_FAILURES == 3


def test_extract_stage_rejects_short_text():
    ctx = context_from_raw({"title": "x", "source_url": "https://example.test/a"})
    with pytest.raises(RetryableStageError, match="insufficient"):
        run_extract_stage(ctx, _deps(fetch_and_extract=lambda *a, **k: "too short"))


def test_score_stage_consultancy_false_stops_before_draft():
    drafted = []
    ctx = context_from_raw({"title": "Job", "source_url": "https://example.test/job"})
    ctx.full_text = "Terms of Reference " * 40
    deps = _deps(
        analyze_rfp=lambda *a, **k: _analysis(consultancy=False),
        draft_bid_or_watch_proposal=lambda *a, **k: drafted.append(1) or {},
    )
    with pytest.raises(TerminalSkip, match="not_consultancy"):
        run_score_stage(ctx, deps)
    assert drafted == []


def test_missing_consultancy_flag_defaults_true_and_continues():
    ctx = context_from_raw({"title": "Eval", "source_url": "https://example.test/eval"})
    ctx.full_text = "Terms of Reference " * 40
    ctx = run_score_stage(
        ctx,
        _deps(analyze_rfp=lambda *a, **k: _analysis(missing_consultancy=True)),
    )
    assert ctx.bid_analysis.get("is_consultancy_contract", True) is True
    assert ctx.airtable_record_id == "rec-1"


def test_client_intelligence_failure_does_not_block_score():
    ctx = context_from_raw({"title": "Eval", "source_url": "https://example.test/eval"})
    ctx.full_text = "Terms of Reference " * 40

    def boom(**kwargs):
        raise RuntimeError("organizations table missing")

    ctx = run_score_stage(ctx, _deps(build_client_intelligence=boom))
    assert ctx.airtable_record_id == "rec-1"
    assert ctx.client_intelligence.get("storage") == "unavailable"


def test_empty_draft_is_drafting_error_not_success():
    ctx = context_from_raw({"title": "Eval", "source_url": "https://example.test/eval"})
    ctx.full_text = "Terms of Reference " * 40
    ctx.analysis = _analysis()
    ctx._refresh_from_analysis()
    with pytest.raises(DraftingError):
        run_draft_stage(ctx, _deps(draft_bid_or_watch_proposal=lambda *a, **k: {}))


def test_assert_usable_client_draft():
    assert draft_has_client_facing_prose({
        "cover_letter": "A complete client-facing cover letter.",
        "quality_score": {"overall_score": 90},
    })
    with pytest.raises(DraftingError):
        assert_usable_client_draft({"cover_letter": "", "quality_score": {}})
    with pytest.raises(DraftingError):
        assert_usable_client_draft(None)


def test_run_pipeline_stops_after_score_with_a_recommended_outline():
    stages = []
    drafts = []

    def persist(url, token, stage, checkpoint):
        stages.append((stage, checkpoint))
        assert checkpoint.get("full_text")
        return True

    def draft(*args, **kwargs):
        drafts.append(kwargs.get("accepted_outline"))
        return {"cover_letter": "A complete client-facing cover letter for review."}

    outcome = run_opportunity_pipeline(
        {"title": "Eval", "source_url": "https://example.test/eval"},
        claim_token="tok",
        deps=_deps(persist_stage=persist, draft_bid_or_watch_proposal=draft),
        snapshot=ProcessingSnapshot(resume_available=True),
    )
    assert outcome.disposition == "awaiting_outline"
    assert outcome.result["awaiting_outline"] is True
    assert drafts == []
    assert [stage for stage, _ in stages] == ["extracted", "scored", "scored"]
    recommended = stages[-1][1]["recommended_outline"]
    headings = [item["heading"] for item in recommended["sections"]]
    assert "Methodology" in headings
    assert recommended["submission_type"] == "FULL_PROPOSAL"
    assert not stages[-1][1].get("accepted_outline", {}).get("sections")


def test_direct_draft_skips_the_score_and_still_reads_the_file():
    stages = []
    drafts = []
    fetches = []

    def persist(url, token, stage, checkpoint):
        stages.append(stage)
        assert checkpoint.get("skip_to_draft") is True
        return True

    def fetch(*args, **kwargs):
        fetches.append(args[0] if args else "")
        return "Terms of Reference " * 40

    def analyze(*args, **kwargs):
        raise AssertionError("the score must not run")

    def draft(*args, **kwargs):
        drafts.append((args, kwargs))
        return {"cover_letter": "A complete client-facing cover letter for review."}

    accepted = {
        "id": "house_eoi",
        "name": "Cortech EOI",
        "submission_type": "EOI",
        "sections": [
            {"heading": "Letter of interest"},
            {"heading": "Firm profile"},
        ],
    }
    outcome = run_opportunity_pipeline(
        {"title": "Eval", "source_url": "https://example.test/eval"},
        claim_token="tok",
        deps=_deps(
            fetch_and_extract=fetch,
            analyze_rfp=analyze,
            persist_stage=persist,
            draft_bid_or_watch_proposal=draft,
        ),
        snapshot=ProcessingSnapshot(
            pipeline_stage="discovered",
            resume_available=True,
            checkpoint={
                "accepted_outline": accepted,
                "skip_to_draft": True,
            },
        ),
    )
    assert fetches == ["https://example.test/eval"]
    assert outcome.disposition == "success"
    assert stages == ["extracted", "drafted"]
    analysis, kwargs = drafts[0][0][0], drafts[0][1]
    assert kwargs["submission_type"] == "EOI"
    assert kwargs["tor_text"].startswith("Terms of Reference")
    assert kwargs["accepted_outline"]["sections"][0]["heading"] == "Letter of interest"
    assert "evaluation_criteria" not in analysis
    assert analysis["bid_analysis"]["bid_recommendation"] == "BID"
    assert outcome.result["score"] is None


def test_accepted_outline_is_what_the_draft_stage_receives():
    drafts = []
    accepted = {
        "id": "custom",
        "name": "Custom",
        "submission_type": "EOI",
        "sections": [{"heading": "Letter of interest"}],
    }

    def draft(*args, **kwargs):
        drafts.append(kwargs)
        return {"cover_letter": "A complete client-facing cover letter for review."}

    outcome = run_opportunity_pipeline(
        {"title": "Eval", "source_url": "https://example.test/eval"},
        claim_token="tok",
        deps=_deps(draft_bid_or_watch_proposal=draft),
        snapshot=ProcessingSnapshot(
            pipeline_stage="scored",
            resume_available=True,
            checkpoint={
                "full_text": "Terms of Reference " * 40,
                "analysis": _analysis(),
                "accepted_outline": accepted,
                "recommended_outline": {
                    "sections": [{"heading": "Methodology"}],
                },
                "title": "Eval",
            },
        ),
    )
    assert outcome.disposition == "success"
    assert drafts and drafts[0]["submission_type"] == "EOI"
    assert drafts[0]["accepted_outline"]["sections"][0]["heading"] == "Letter of interest"
    assert outcome.result["proposal_sections"]["cover_letter"].startswith("A complete")


def test_score_stage_names_an_unusable_analysis_reply(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "intelligence.analyzer.complete",
        lambda **kwargs: SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="hidden")],
            stop_reason="max_tokens",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        ),
    )
    monkeypatch.setattr("intelligence.analyzer.usage_totals", lambda _response: (1, 1))
    monkeypatch.setattr("intelligence.analyzer.log_agent_action", lambda **kwargs: None)

    ctx = context_from_raw({
        "title": "Somalia MEL",
        "source_url": "https://example.com/tor",
    })
    ctx.full_text = "Terms of Reference for an evaluation of a livelihoods programme. " * 4
    with pytest.raises(RetryableStageError) as exc:
        run_score_stage(ctx, _deps(analyze_rfp=analyze_rfp))

    assert exc.value.error_type == "ANALYSIS_ERROR"
    assert "not a JSON object" in exc.value.reason
    assert "stop_reason=max_tokens" in exc.value.reason
    assert "content=thinking" in exc.value.reason
    assert "empty analysis payload" in exc.value.reason


def test_auto_accept_outline_runs_extract_score_and_draft_end_to_end():
    stages = []
    drafts = []

    def persist(url, token, stage, checkpoint):
        stages.append((stage, dict(checkpoint)))
        return True

    def draft(*args, **kwargs):
        drafts.append(kwargs.get("accepted_outline"))
        return {"cover_letter": "A complete client-facing cover letter for review."}

    outcome = run_opportunity_pipeline(
        {"title": "Eval", "source_url": "https://example.test/eval"},
        claim_token="tok",
        deps=_deps(persist_stage=persist, draft_bid_or_watch_proposal=draft),
        snapshot=ProcessingSnapshot(
            pipeline_stage="discovered",
            resume_available=True,
            checkpoint={"auto_accept_outline": True},
        ),
    )
    assert outcome.disposition == "success"
    assert outcome.result["awaiting_outline"] is False
    assert [stage for stage, _ in stages] == ["extracted", "scored", "drafted"]
    assert len(drafts) == 1
    assert drafts[0]["sections"]
    assert outcome.result["proposal_sections"]["cover_letter"].startswith("A complete")


def test_uploaded_full_text_in_checkpoint_skips_network_fetch_and_drafts():
    fetches = []
    drafts = []

    def fetch(*args, **kwargs):
        fetches.append(args)
        raise AssertionError("network fetch should not run when full_text is already uploaded")

    def draft(*args, **kwargs):
        drafts.append(kwargs)
        return {"cover_letter": "Drafted from uploaded ToR document."}

    uploaded_text = "Terms of Reference for Somalia Third-Party Monitoring and Evaluation. " * 25
    outcome = run_opportunity_pipeline(
        {"title": "", "source_url": "https://upload.cortechconsultinggroup.com/tor/abc123/tor.pdf"},
        claim_token="tok",
        deps=_deps(fetch_and_extract=fetch, draft_bid_or_watch_proposal=draft),
        snapshot=ProcessingSnapshot(
            pipeline_stage="discovered",
            resume_available=True,
            checkpoint={
                "title": "Somalia TPM Evaluation",
                "full_text": uploaded_text,
                "auto_accept_outline": True,
            },
        ),
    )
    assert fetches == []
    assert outcome.disposition == "success"
    assert len(drafts) == 1
    assert drafts[0]["tor_text"] == uploaded_text
    assert outcome.result["proposal_sections"]["cover_letter"] == "Drafted from uploaded ToR document."

