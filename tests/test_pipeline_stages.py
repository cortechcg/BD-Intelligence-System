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


def test_run_pipeline_persists_extracted_then_scored_then_drafted():
    stages = []

    def persist(url, token, stage, checkpoint):
        stages.append(stage)
        assert checkpoint.get("full_text")
        if stage in ("scored", "drafted"):
            assert checkpoint.get("analysis")
        return True

    outcome = run_opportunity_pipeline(
        {"title": "Eval", "source_url": "https://example.test/eval"},
        claim_token="tok",
        deps=_deps(persist_stage=persist),
        snapshot=ProcessingSnapshot(resume_available=True),
    )
    assert outcome.disposition == "success"
    assert stages == ["extracted", "scored", "drafted"]
    assert outcome.result["proposal_sections"]["cover_letter"].startswith("A complete")
