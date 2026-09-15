"""Kill-mid-run resume and drafting dead-letter on the existing ledger."""

from __future__ import annotations

from pathlib import Path

import pytest

import main
from intelligence.pipeline_stages import MAX_DRAFT_FAILURES
from utils.observability import reset_opportunity_usage, reset_run_spend_cap
from utils.urls import canonicalize_url


@pytest.fixture(autouse=True)
def _clear_run_context():
    main._execution_budget.set(None)
    reset_opportunity_usage()
    reset_run_spend_cap()
    yield
    main._execution_budget.set(None)
    reset_opportunity_usage()
    reset_run_spend_cap()


class MemoryLedger:
    """In-memory opportunity_processing. Same claim/resume/dead-letter rules."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.kill_after_stage: str | None = None
        self.killed = False
        self.dead_letters: list[tuple] = []
        self.failed: list[tuple] = []
        self.completed: list[tuple] = []

    def _key(self, url: str) -> str:
        return canonicalize_url(url) or url

    def claim(self, url, title="", force=False, **kwargs):
        key = self._key(url)
        row = self.rows.get(key)
        if row and row["state"] in ("completed", "dead_letter") and not force:
            return None
        token = f"claim-{len(self.rows) + 1}-{row['pipeline_stage'] if row else 'new'}"
        if row is None:
            self.rows[key] = {
                "state": "processing",
                "pipeline_stage": "discovered",
                "checkpoint": {},
                "draft_fail_count": 0,
                "claim_token": token,
                "title": title,
            }
        else:
            if force:
                row["pipeline_stage"] = "discovered"
                row["checkpoint"] = {}
                row["draft_fail_count"] = 0
            row["state"] = "processing"
            row["claim_token"] = token
        return self.rows[key]["claim_token"]

    def load(self, url):
        key = self._key(url)
        row = self.rows.get(key)
        if not row:
            return {
                "pipeline_stage": "discovered",
                "checkpoint": {},
                "draft_fail_count": 0,
                "resume_available": True,
            }
        checkpoint = dict(row.get("checkpoint") or {})
        count = int(row.get("draft_fail_count") or 0)
        if not count:
            count = int(checkpoint.get("draft_fail_count") or 0)
        return {
            "pipeline_stage": row.get("pipeline_stage") or "discovered",
            "checkpoint": checkpoint,
            "draft_fail_count": count,
            "resume_available": True,
        }

    def persist(self, url, token, stage, checkpoint=None):
        key = self._key(url)
        row = self.rows.setdefault(key, {
            "state": "processing",
            "pipeline_stage": "discovered",
            "checkpoint": {},
            "draft_fail_count": 0,
            "claim_token": token,
        })
        row["pipeline_stage"] = stage
        row["checkpoint"] = dict(checkpoint or {})
        if "draft_fail_count" in row["checkpoint"]:
            row["draft_fail_count"] = int(row["checkpoint"]["draft_fail_count"] or 0)
        if self.kill_after_stage == stage and not self.killed:
            self.killed = True
            raise RuntimeError(f"killed mid-run after {stage}")
        return True

    def complete(self, url, token):
        key = self._key(url)
        row = self.rows.get(key)
        if not row or row.get("claim_token") != token:
            return False
        row["state"] = "completed"
        self.completed.append((key, token))
        return True

    def fail(self, url, token, error=""):
        key = self._key(url)
        row = self.rows.get(key)
        if not row or row.get("claim_token") != token:
            return False
        row["state"] = "failed"
        row["last_error"] = error
        self.failed.append((key, token, error))
        return True

    def dead_letter(self, url, token, error=""):
        key = self._key(url)
        row = self.rows.get(key)
        if not row or row.get("claim_token") != token:
            return False
        row["state"] = "dead_letter"
        row["last_error"] = error
        self.dead_letters.append((key, token, error))
        return True


def _analysis():
    return {
        "opportunity": {
            "title": "Somalia MEL Evaluation",
            "client": "UNICEF",
            "donor": "UNICEF",
            "submission_deadline": "2099-12-01",
            "project_location": ["Somalia"],
            "estimated_budget_usd": 50000,
        },
        "requirements": {"thematic_areas": ["evaluation"], "language_requirements": ["English"]},
        "team_requirements": [],
        "bid_analysis": {
            "is_consultancy_contract": True,
            "submission_type": "FULL_PROPOSAL",
            "cortech_fit_score": 80,
            "win_probability": 70,
            "bid_recommendation": "BID",
            "key_strengths": [],
            "key_gaps": [],
        },
        "bid_intelligence": {"score_version": "1.0.0"},
    }


def _wire_ledger(monkeypatch, ledger: MemoryLedger):
    monkeypatch.setattr(main, "claim_opportunity_processing", ledger.claim)
    monkeypatch.setattr(main, "load_processing_snapshot", ledger.load)
    monkeypatch.setattr(main, "persist_opportunity_stage", ledger.persist)
    monkeypatch.setattr(main, "complete_opportunity_processing", ledger.complete)
    monkeypatch.setattr(main, "fail_opportunity_processing", ledger.fail)
    monkeypatch.setattr(main, "dead_letter_opportunity_processing", ledger.dead_letter)
    monkeypatch.setattr(main, "store_opportunity", lambda *a, **k: "cache")
    monkeypatch.setattr(main, "find_opportunity_by_content_hash", lambda *a, **k: None)
    monkeypatch.setattr(
        main, "fetch_and_extract",
        lambda *a, **k: "Terms of Reference for an evaluation in Somalia. " * 20,
    )
    monkeypatch.setattr(main, "analyze_rfp", lambda *a, **k: _analysis())
    monkeypatch.setattr(main, "apply_bid_intelligence", lambda analysis, team=None: analysis)
    monkeypatch.setattr(main, "create_opportunity", lambda payload: "rec-1")
    monkeypatch.setattr(main, "update_opportunity", lambda *a, **k: None)
    monkeypatch.setattr(main, "calculate_budget", lambda *a, **k: {"status": "INSUFFICIENT DATA"})
    monkeypatch.setattr(main, "match_team_to_requirements", lambda *a, **k: {
        "matched_team": {}, "gaps": [], "coverage_percent": 0,
    })
    monkeypatch.setattr(
        main, "build_client_intelligence",
        lambda **k: {"storage": "test", "client": {"match": {"status": "UNKNOWN"}}},
    )
    monkeypatch.setattr(main, "save_draft_memory", lambda **k: None)
    monkeypatch.setattr(main, "build_compliance_matrix", lambda *a, **k: [])


def test_kill_mid_run_after_scored_resumes_at_draft_not_from_scratch(monkeypatch):
    """Simulate a crash between scored and drafted; the next claim resumes."""
    ledger = MemoryLedger()
    ledger.kill_after_stage = "scored"
    fetches = []
    analyses = []
    drafts = []

    _wire_ledger(monkeypatch, ledger)
    monkeypatch.setattr(
        main, "fetch_and_extract",
        lambda *a, **k: fetches.append(1) or ("Terms of Reference for Somalia. " * 20),
    )
    monkeypatch.setattr(
        main, "analyze_rfp",
        lambda *a, **k: analyses.append(1) or _analysis(),
    )
    monkeypatch.setattr(
        main, "generate_proposal",
        lambda *a, **k: drafts.append(1) or {
            "cover_letter": "Resumed client-facing draft after the crash."
        },
    )

    source = {
        "title": "Somalia MEL",
        "source_url": "https://procurement.example/resume-me",
        "source_portal": "Fixture",
    }
    with pytest.raises(RuntimeError, match="killed mid-run after scored"):
        main.process_opportunity(source)

    row = ledger.rows["https://procurement.example/resume-me"]
    assert row["pipeline_stage"] == "scored"
    assert row["state"] == "failed"
    assert row["checkpoint"].get("full_text")
    assert row["checkpoint"].get("analysis")
    assert fetches == [1]
    assert analyses == [1]
    assert drafts == []

    result = main.process_opportunity(source)
    assert result is not None
    assert result["proposal_sections"]["cover_letter"].startswith("Resumed")
    assert fetches == [1], "resume must not re-fetch"
    assert analyses == [1], "resume must not re-analyze"
    assert drafts == [1], "resume starts at draft"
    assert ledger.rows["https://procurement.example/resume-me"]["state"] == "completed"
    assert ledger.rows["https://procurement.example/resume-me"]["pipeline_stage"] == "drafted"


def test_drafting_failure_is_retryable_then_dead_lettered(monkeypatch):
    ledger = MemoryLedger()
    _wire_ledger(monkeypatch, ledger)
    monkeypatch.setattr(main, "generate_proposal", lambda *a, **k: {})

    source = {
        "title": "Somalia MEL",
        "source_url": "https://procurement.example/empty-draft",
    }
    for _ in range(MAX_DRAFT_FAILURES - 1):
        assert main.process_opportunity(source) is None
        row = ledger.rows["https://procurement.example/empty-draft"]
        assert row["state"] == "failed"
        assert row["pipeline_stage"] == "scored"
        assert ledger.dead_letters == []

    assert main.process_opportunity(source) is None
    row = ledger.rows["https://procurement.example/empty-draft"]
    assert row["state"] == "dead_letter"
    assert ledger.dead_letters
    assert "drafting failed" in (ledger.dead_letters[0][2] or "")

    # Dead-lettered items are not silently skipped off the ledger; they stay
    # visible and are not reclaimed by bulk processing.
    assert main.process_opportunity(source) is None
    assert row["state"] == "dead_letter"


def test_consultancy_false_is_terminal_and_does_not_draft(monkeypatch):
    ledger = MemoryLedger()
    _wire_ledger(monkeypatch, ledger)
    drafted = []
    analysis = _analysis()
    analysis["bid_analysis"]["is_consultancy_contract"] = False
    monkeypatch.setattr(main, "analyze_rfp", lambda *a, **k: analysis)
    monkeypatch.setattr(main, "generate_proposal", lambda *a, **k: drafted.append(1) or {})

    result = main.process_opportunity({
        "title": "Staff vacancy",
        "source_url": "https://procurement.example/job",
    })
    assert result is None
    assert drafted == []
    assert ledger.rows["https://procurement.example/job"]["state"] == "completed"


def test_stage_migration_extends_existing_ledger_not_a_second_table():
    sql = Path("supabase_migration_opportunity_stages.sql").read_text()
    assert "ALTER TABLE opportunity_processing" in sql
    assert "pipeline_stage" in sql
    assert "checkpoint" in sql
    assert "dead_letter" in sql
    assert "persist_opportunity_stage" in sql
    assert "CREATE TABLE" not in sql.split("opportunity_processing", 1)[0]
    assert "CREATE TABLE IF NOT EXISTS opportunity_processing" not in sql
    assert "discovered" in sql and "extracted" in sql and "scored" in sql
    assert "drafted" in sql and "reviewed" in sql and "outcome" in sql
    # Reclaim must preserve checkpoint unless force.
    assert "WHEN p_force THEN 'discovered'" in sql
    assert "ELSE opportunity_processing.checkpoint" in sql
    assert "dead_letter" in sql
    original = Path("supabase_migration_opportunity_state.sql").read_text()
    assert "claim_opportunity_processing" in original
    assert "DROP TABLE" not in sql
    assert "DELETE FROM" not in sql.upper()
