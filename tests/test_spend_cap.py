"""Per-run LLM spend cap. Provider must not be called after the cap fires."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main
from tests.test_pipeline_resume import MemoryLedger, _analysis, _wire_ledger
from utils.errors import SpendCapError
from utils.llm import complete
from utils.observability import (
    reset_opportunity_usage,
    reset_run_spend_cap,
    run_spend_snapshot,
    start_run_spend_cap,
)


@pytest.fixture(autouse=True)
def _clear_spend():
    main._execution_budget.set(None)
    reset_opportunity_usage()
    reset_run_spend_cap()
    yield
    main._execution_budget.set(None)
    reset_opportunity_usage()
    reset_run_spend_cap()


class _FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
            id="req-test",
        )


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages


def test_zero_cap_blocks_complete_before_provider(monkeypatch):
    messages = _FakeMessages()
    monkeypatch.setattr(
        "utils.llm.get_anthropic_client",
        lambda **kwargs: _FakeClient(messages),
    )
    start_run_spend_cap(0.0)
    with pytest.raises(SpendCapError, match="spend cap"):
        complete("claude-haiku-4-5", [{"role": "user", "content": "x"}], stage="kill")
    assert messages.calls == []
    snap = run_spend_snapshot()
    assert snap["blocked_calls"] == 1
    assert snap["provider_calls"] == 0

    with pytest.raises(SpendCapError):
        complete("claude-haiku-4-5", [{"role": "user", "content": "y"}], stage="kill2")
    assert messages.calls == []
    assert run_spend_snapshot()["blocked_calls"] == 2


def test_tiny_cap_allows_one_then_halts(monkeypatch):
    messages = _FakeMessages()
    monkeypatch.setattr(
        "utils.llm.get_anthropic_client",
        lambda **kwargs: _FakeClient(messages),
    )
    # First call is ~$4.6e-5 at haiku list prices with 11/7 tokens — too small
    # for a $0.01 cap. Use a cap below that so the second complete() is blocked.
    start_run_spend_cap(0.0000001)
    complete("claude-haiku-4-5", [{"role": "user", "content": "a"}], stage="one")
    assert len(messages.calls) == 1
    with pytest.raises(SpendCapError):
        complete("claude-haiku-4-5", [{"role": "user", "content": "b"}], stage="two")
    assert len(messages.calls) == 1


def test_spend_cap_during_draft_leaves_scored_retryable(monkeypatch):
    ledger = MemoryLedger()
    _wire_ledger(monkeypatch, ledger)
    monkeypatch.setattr("config.MAX_RUN_COST_USD", 0.0)
    monkeypatch.setattr(main, "MAX_OPPORTUNITIES_PER_RUN", 5)
    created = []

    class _Msgs:
        def create(self, **kwargs):
            created.append(1)
            raise AssertionError("provider must not be called")

    monkeypatch.setattr(
        "utils.llm.get_anthropic_client",
        lambda **kwargs: SimpleNamespace(messages=_Msgs()),
    )

    def draft_hits_cap(*args, **kwargs):
        complete(
            "claude-sonnet-5",
            [{"role": "user", "content": "draft"}],
            stage="killtest_draft",
        )
        return {"cover_letter": "should not reach"}

    monkeypatch.setattr(main, "generate_proposal", draft_hits_cap)
    monkeypatch.setattr(
        main,
        "generate_eoi",
        lambda *a, **k: pytest.fail("EOI must not run"),
    )

    source = {
        "title": "Somalia MEL",
        "source_url": "https://procurement.example/spend-cap",
        "source_portal": "Fixture",
    }
    # Seed scored via a first run that kills after persist, then spend-cap the draft.
    ledger.kill_after_stage = "scored"
    monkeypatch.setattr(
        main,
        "generate_proposal",
        lambda *a, **k: pytest.fail("draft must not run before scored persist"),
    )
    with pytest.raises(RuntimeError, match="killed mid-run after scored"):
        main.process_opportunity(source)
    row = ledger.rows["https://procurement.example/spend-cap"]
    assert row["pipeline_stage"] == "scored"
    assert row["state"] == "failed"

    monkeypatch.setattr(main, "generate_proposal", draft_hits_cap)
    fetches = []
    analyses = []
    monkeypatch.setattr(
        main,
        "fetch_and_extract",
        lambda *a, **k: fetches.append(1) or ("Terms of Reference for Somalia. " * 20),
    )
    monkeypatch.setattr(
        main,
        "analyze_rfp",
        lambda *a, **k: analyses.append(1) or _analysis(),
    )
    assert main.process_opportunity(source) is None
    row = ledger.rows["https://procurement.example/spend-cap"]
    assert row["pipeline_stage"] == "scored"
    assert row["state"] == "failed"
    assert "spend cap" in (row.get("last_error") or "")
    assert created == []
    assert fetches == []
    assert analyses == []
    assert row["state"] != "dead_letter"
    assert "NO-BID" not in (row.get("last_error") or "")
