"""Hosted opportunity_processing stage columns and spend-cap kill-test.

These tests talk to the real Supabase project from .env. They never print keys.
They fail if pipeline_stage / checkpoint / draft_fail_count are missing.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import config
import main
from database.supabase_client import (
    REQUIRED_STAGE_COLUMNS,
    check_opportunity_stage_schema,
    claim_opportunity_processing,
    complete_opportunity_processing,
    fail_opportunity_processing,
    load_processing_snapshot,
    persist_opportunity_stage,
    reset_opportunity_ledger_status,
    supabase,
)
from tests.test_pipeline_resume import _analysis
from utils.llm import complete
from utils.observability import reset_opportunity_usage, reset_run_spend_cap
from utils.urls import canonicalize_url


pytestmark = pytest.mark.skipif(
    not (config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY),
    reason="hosted SUPABASE_URL / SUPABASE_SERVICE_KEY not configured",
)


def _row(source_url: str) -> dict:
    key = canonicalize_url(source_url) or source_url
    result = (
        supabase.table("opportunity_processing")
        .select(
            "source_url,state,pipeline_stage,draft_fail_count,last_error,checkpoint"
        )
        .eq("source_url", key)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    assert rows, f"INSUFFICIENT DATA: no opportunity_processing row for test URL"
    return rows[0]


def _delete(source_url: str) -> None:
    key = canonicalize_url(source_url) or source_url
    try:
        supabase.table("opportunity_processing").delete().eq("source_url", key).execute()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_live_ledger_flags():
    reset_opportunity_ledger_status()
    main._execution_budget.set(None)
    reset_opportunity_usage()
    reset_run_spend_cap()
    yield
    reset_opportunity_ledger_status()
    main._execution_budget.set(None)
    reset_opportunity_usage()
    reset_run_spend_cap()


def test_hosted_opportunity_processing_stage_columns_exist():
    missing = check_opportunity_stage_schema()
    assert missing == [], (
        "hosted opportunity_processing is missing stage columns "
        f"{missing}. Apply supabase_migration_opportunity_stages.sql."
    )
    for col in REQUIRED_STAGE_COLUMNS:
        assert col not in missing


@pytest.fixture
def hosted_stage_columns():
    missing = check_opportunity_stage_schema()
    if missing:
        pytest.skip(
            "hosted stage columns missing "
            f"{missing}; apply supabase_migration_opportunity_stages.sql"
        )


def test_hosted_pipeline_stage_is_written_discovered_to_drafted(hosted_stage_columns):
    missing = check_opportunity_stage_schema()
    assert missing == [], missing
    source_url = f"https://phase8-stages.example/cortech-{uuid.uuid4()}"
    token = claim_opportunity_processing(
        source_url, "Phase 8 stage-write probe", force=True
    )
    assert token, "INSUFFICIENT DATA: claim_opportunity_processing returned no token"
    try:
        ckpt = {
            "full_text": "Terms of Reference for a Somalia MEL assignment. " * 12,
            "title": "Phase 8 stage-write probe",
        }
        assert persist_opportunity_stage(source_url, token, "extracted", ckpt)
        row = _row(source_url)
        assert row["pipeline_stage"] == "extracted"
        ckpt["analysis"] = _analysis()
        assert persist_opportunity_stage(source_url, token, "scored", ckpt)
        row = _row(source_url)
        assert row["pipeline_stage"] == "scored"
        ckpt["proposal_sections"] = {
            "cover_letter": "Client-facing draft written on the hosted ledger."
        }
        assert persist_opportunity_stage(source_url, token, "drafted", ckpt)
        row = _row(source_url)
        assert row["pipeline_stage"] == "drafted"
        assert row["state"] == "processing"
        snap = load_processing_snapshot(source_url)
        assert snap["pipeline_stage"] == "drafted"
        assert snap["resume_available"] is True
        assert complete_opportunity_processing(source_url, token)
        row = _row(source_url)
        assert row["state"] == "completed"
        assert row["pipeline_stage"] == "drafted"
    finally:
        _delete(source_url)


def test_spend_cap_kill_leaves_hosted_row_resumable(monkeypatch, hosted_stage_columns):
    missing = check_opportunity_stage_schema()
    assert missing == [], missing

    source_url = f"https://phase8-killtest.example/cortech-{uuid.uuid4()}"
    token = claim_opportunity_processing(
        source_url, "Phase 8 spend-cap kill-test", force=True
    )
    assert token
    analysis = _analysis()
    ckpt = {
        "full_text": "Terms of Reference for a Somalia MEL assignment. " * 12,
        "analysis": analysis,
        "title": "Phase 8 spend-cap kill-test",
        "airtable_record_id": "rec-killtest",
        "recommendation": "BID",
        "fit_score": 80,
        "win_prob": 70,
        "opp_id": "rec-killtest",
        "submission_type": "FULL_PROPOSAL",
        "matched_team_result": {"matched_team": {}, "gaps": [], "coverage_percent": 0},
        "budget": {"status": "INSUFFICIENT DATA"},
        "client_intelligence": {"storage": "test"},
        "intelligence": {"score_version": "1.0.0"},
        "matrix": [],
        "source_url": source_url,
        "dedup_url": canonicalize_url(source_url),
    }
    try:
        assert persist_opportunity_stage(source_url, token, "extracted", ckpt)
        assert persist_opportunity_stage(source_url, token, "scored", ckpt)
        assert fail_opportunity_processing(
            source_url, token, "seeded scored for spend-cap kill-test"
        )
        seeded = _row(source_url)
        assert seeded["pipeline_stage"] == "scored"
        assert seeded["state"] == "failed"

        created = []

        class _Msgs:
            def create(self, **kwargs):
                created.append(kwargs)
                raise AssertionError("hosted kill-test must not call Anthropic")

        monkeypatch.setattr(
            "utils.llm.get_anthropic_client",
            lambda **kwargs: SimpleNamespace(messages=_Msgs()),
        )
        monkeypatch.setattr("config.MAX_RUN_COST_USD", 0.0)
        monkeypatch.setattr(main, "MAX_OPPORTUNITIES_PER_RUN", 5)
        monkeypatch.setattr(main, "fetch_and_extract", lambda *a, **k: created.append("fetch"))
        monkeypatch.setattr(main, "analyze_rfp", lambda *a, **k: created.append("analyze"))
        monkeypatch.setattr(main, "create_opportunity", lambda payload: "rec-killtest")
        monkeypatch.setattr(main, "update_opportunity", lambda *a, **k: None)
        monkeypatch.setattr(
            main,
            "match_team_to_requirements",
            lambda *a, **k: {"matched_team": {}, "gaps": [], "coverage_percent": 0},
        )
        monkeypatch.setattr(
            main, "calculate_budget", lambda *a, **k: {"status": "INSUFFICIENT DATA"}
        )
        monkeypatch.setattr(
            main,
            "build_client_intelligence",
            lambda **k: {"storage": "test", "client": {"match": {"status": "UNKNOWN"}}},
        )
        monkeypatch.setattr(main, "save_draft_memory", lambda **k: None)
        monkeypatch.setattr(main, "build_compliance_matrix", lambda *a, **k: [])
        monkeypatch.setattr(main, "store_opportunity", lambda *a, **k: "cache")

        def draft_hits_cap(*args, **kwargs):
            complete(
                config.CLAUDE_MODEL_PROPOSAL,
                [{"role": "user", "content": "draft"}],
                stage="killtest_draft",
            )
            return {"cover_letter": "should not reach"}

        monkeypatch.setattr(main, "generate_proposal", draft_hits_cap)

        source = {
            "title": "Phase 8 spend-cap kill-test",
            "source_url": source_url,
            "source_portal": "Fixture",
        }
        assert main.process_opportunity(source) is None
        halted = _row(source_url)
        assert halted["pipeline_stage"] == "scored"
        assert halted["state"] == "failed"
        assert halted["state"] != "dead_letter"
        error = halted.get("last_error") or ""
        assert "spend cap" in error.lower()
        assert created == []

        monkeypatch.setattr("config.MAX_RUN_COST_USD", 25.0)
        monkeypatch.setattr(
            main,
            "generate_proposal",
            lambda *a, **k: {
                "cover_letter": "Resumed client-facing draft after spend cap."
            },
        )
        result = main.process_opportunity(source)
        assert result is not None
        resumed = _row(source_url)
        assert resumed["pipeline_stage"] == "drafted"
        assert resumed["state"] == "completed"
        assert created == []
    finally:
        _delete(source_url)
