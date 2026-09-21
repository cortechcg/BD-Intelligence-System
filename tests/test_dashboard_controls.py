"""Interactive controls: bulk trigger, aggregate cap, retry, cancel, re-run.

The addendum's definition of done, item by item:

* bulk-triggering respects the spend cap in aggregate, not just per job;
* Retry resumes from checkpoint rather than reprocessing from `discovered`;
* Cancel's real behaviour is documented in the UI and matches the code;
* the CLI/dashboard parity still holds now that these controls exist
  (tests/test_dashboard_parity.py runs unchanged alongside this file).

External systems are stubbed. No network.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import main
from dashboard import app as app_module
from dashboard import settings, triggers, worker
from utils import observability
from utils.errors import ErrorType, RunHaltRequested, SpendCapError, classify_exception

URL = "https://example.org/tenders/controls"


# ── aggregate cap ───────────────────────────────────────────────────────────

def _window(spent: float, runs: int = 3, unknown: int = 0, available: bool = True) -> dict:
    return {"spent_usd": spent, "runs": runs, "runs_unknown_cost": unknown,
            "hours": 24, "cap_usd": settings.AGGREGATE_CAP_USD, "available": available}


def test_aggregate_cap_blocks_when_window_spend_reaches_cap(monkeypatch):
    monkeypatch.setattr(settings, "AGGREGATE_CAP_USD", 10.0)
    blocked, reason = triggers.aggregate_cap_reached(_window(10.0))
    assert blocked is True
    assert "DASHBOARD_AGGREGATE_CAP_USD=10.00" in reason
    blocked, _ = triggers.aggregate_cap_reached(_window(9.99))
    assert blocked is False


def test_aggregate_cap_treats_unknown_cost_as_over(monkeypatch):
    """Same rule the per-run cap applies: unpriced calls cannot prove 'under'."""
    monkeypatch.setattr(settings, "AGGREGATE_CAP_USD", 100.0)
    blocked, reason = triggers.aggregate_cap_reached(_window(1.0, unknown=1))
    assert blocked is True
    assert "unknown cost" in reason


def test_aggregate_cap_refuses_to_run_blind(monkeypatch):
    blocked, reason = triggers.aggregate_cap_reached(_window(0.0, available=False))
    assert blocked is True
    assert "blind" in reason


def test_zero_aggregate_cap_blocks_every_run(monkeypatch):
    monkeypatch.setattr(settings, "AGGREGATE_CAP_USD", 0.0)
    assert triggers.aggregate_cap_reached(_window(0.0))[0] is True


def test_bulk_trigger_of_forty_jobs_is_bounded_by_the_aggregate_cap(monkeypatch):
    """The headline: mashing "Draft selected" cannot buy forty fresh $25 caps.

    Forty jobs are queued (each its own row — fan-out is at the queue). The
    worker claims them one at a time. We simulate real spend accumulating on
    finished rows: after enough runs the window reaches the cap and every
    remaining job is refused *by the worker, before any pipeline call*.
    """
    monkeypatch.setattr(settings, "AGGREGATE_CAP_USD", 30.0)

    queue: list[dict] = [
        {"id": f"job-{i}", "source_url": f"{URL}/{i}", "trigger_kind": triggers.DRAFT_EXISTING,
         "requested_by": "ops@cortechconsultinggroup.com", "attempt_count": 1,
         "worker_token": "tok"}
        for i in range(40)
    ]
    ledger: dict[str, dict] = {}          # trigger id → finish() kwargs
    window = {"spent": 0.0}
    pipeline_calls = {"n": 0}

    monkeypatch.setattr(triggers, "claim", lambda token, lease: queue.pop(0) if queue else None)
    monkeypatch.setattr(
        triggers, "spend_window",
        lambda hours=None: _window(window["spent"], runs=len(ledger)),
    )

    def _finish(tid, token, **kw):
        ledger[tid] = kw
        spend = kw.get("spend") or {}
        window["spent"] += float(spend.get("spent_usd") or 0.0)
        return True

    monkeypatch.setattr(triggers, "finish", _finish)
    monkeypatch.setattr(worker, "_with_heartbeat", lambda job, token, fn: fn())

    def _fake_run_trigger(job):
        pipeline_calls["n"] += 1
        # each run "costs" 8 USD, well under the 25 per-run cap
        return {"status": "succeeded", "error_kind": None, "error_message": None,
                "execution_id": "e", "result": {}, "spend": {"spent_usd": 8.0}}

    monkeypatch.setattr(worker, "run_trigger", _fake_run_trigger)

    while worker.run_once("tok"):
        pass

    assert len(ledger) == 40, "every queued job must be finalised, not left running"
    # 0, 8, 16, 24 → run; at 32 ≥ 30 the gate closes. Four runs, thirty-six refused.
    assert pipeline_calls["n"] == 4
    refused = [k for k, v in ledger.items() if v.get("result") == {"gate": "aggregate_cap"}]
    assert len(refused) == 36
    for tid in refused:
        assert ledger[tid]["status"] == "failed"
        assert ledger[tid]["error_kind"] == ErrorType.SPEND_CAP_ERROR
        assert "aggregate dashboard spend" in ledger[tid]["error_message"]
    assert window["spent"] == pytest.approx(32.0)


def test_aggregate_gate_runs_before_any_pipeline_call(monkeypatch):
    """Structural: the worker checks the aggregate cap before run_trigger."""
    src = inspect.getsource(worker.run_once)
    assert src.index("aggregate_cap_reached") < src.index("run_trigger(job)")


def test_bulk_endpoint_queues_one_row_per_url_and_never_runs_inline(monkeypatch):
    """The HTTP request inserts rows. It does not call the pipeline."""
    src = inspect.getsource(app_module.trigger_bulk)
    assert "enqueue_many" in src
    assert "submit_single_url" not in src
    assert "run_trigger" not in src

    calls = []
    monkeypatch.setattr(triggers, "enqueue", lambda url, kind, by: calls.append((url, kind, by)) or f"id-{len(calls)}")
    out = triggers.enqueue_many([f"{URL}/a", f"{URL}/b"], triggers.DRAFT_EXISTING, "ops@cortechconsultinggroup.com")
    assert [o["queued"] for o in out] == [True, True]
    assert [c[1] for c in calls] == [triggers.DRAFT_EXISTING] * 2
    assert all(c[2] == "ops@cortechconsultinggroup.com" for c in calls), "identity travels with every job"


# ── retry from checkpoint ───────────────────────────────────────────────────

def test_retry_resumes_from_checkpoint_not_from_discovered(monkeypatch):
    """Retry must enter the pipeline with force=False and retry_dead_letter=True.

    force=True is what resets pipeline_stage to `discovered` and wipes the
    checkpoint (ADR 011 §4). A retry that used it would re-pay for extraction
    and analysis, which is exactly what the addendum forbids.
    """
    seen = {}

    def _submit(url, *, force=True, retry_dead_letter=False):
        seen.update({"url": url, "force": force, "retry_dead_letter": retry_dead_letter})
        return {"execution_id": "e", "recommendation": "BID", "proposal_sections": {"x": "y"}}

    monkeypatch.setattr(main, "submit_single_url", _submit)
    monkeypatch.setattr(worker, "_ledger_snapshot", lambda url: {"state": "completed", "pipeline_stage": "drafted"})

    outcome = worker.run_trigger(
        {"id": "t", "source_url": URL, "trigger_kind": triggers.RETRY_DEAD_LETTER,
         "requested_by": "ops@cortechconsultinggroup.com"}
    )
    assert outcome["status"] == "succeeded"
    assert seen["force"] is False
    assert seen["retry_dead_letter"] is True


def test_retry_flag_reaches_the_ledger_claim(monkeypatch):
    """main.submit_single_url → process_opportunity → claim RPC, flag intact."""
    captured = {}

    def _claim(source_url, title="", *, force=False, retry_dead_letter=False, **kw):
        captured.update({"force": force, "retry_dead_letter": retry_dead_letter})
        return None  # "already claimed" → process_opportunity returns None

    monkeypatch.setattr(main, "claim_opportunity_processing", _claim)
    monkeypatch.setattr(main, "check_opportunity_exists", lambda *a, **k: False)
    monkeypatch.setattr(main, "process_win_loss_outcomes", lambda *a, **k: None)

    assert main.submit_single_url(URL, force=False, retry_dead_letter=True) is None
    assert captured == {"force": False, "retry_dead_letter": True}


def test_cli_default_still_forces_from_discovered(monkeypatch):
    """The historical --submit-url behaviour is untouched."""
    captured = {}

    def _claim(source_url, title="", *, force=False, retry_dead_letter=False, **kw):
        captured.update({"force": force, "retry_dead_letter": retry_dead_letter})
        return None

    monkeypatch.setattr(main, "claim_opportunity_processing", _claim)
    monkeypatch.setattr(main, "check_opportunity_exists", lambda *a, **k: False)
    monkeypatch.setattr(main, "process_win_loss_outcomes", lambda *a, **k: None)
    main.submit_single_url(URL)
    assert captured == {"force": True, "retry_dead_letter": False}


def test_claim_rpc_is_sent_the_retry_flag(monkeypatch):
    from database import supabase_client as sc

    sent = {}

    class _Res:
        data = [{"acquired": True, "claim_token": None}]

    class _RPC:
        def __init__(self, name, params):
            sent["name"] = name; sent["params"] = params
        def execute(self):
            _Res.data[0]["claim_token"] = sent["params"]["p_claim_token"]
            return _Res()

    class _SB:
        def rpc(self, name, params):
            return _RPC(name, params)

    monkeypatch.setattr(sc, "supabase", _SB())
    token = sc.claim_opportunity_processing(URL, "t", force=False, retry_dead_letter=True)
    assert token
    assert sent["name"] == "claim_opportunity_processing"
    assert sent["params"]["p_retry_dead_letter"] is True
    assert sent["params"]["p_force"] is False


def test_migration_extends_the_same_claim_function_not_a_second_one():
    sql = Path("supabase_migration_dashboard_controls.sql").read_text()
    assert sql.count("CREATE FUNCTION claim_opportunity_processing(") == 1
    assert "p_retry_dead_letter BOOLEAN DEFAULT FALSE" in sql
    # keeps the checkpoint on a dead-letter retry, resets only the fail budget
    assert "AND opportunity_processing.state = 'dead_letter' THEN 0" in sql
    assert "WHEN p_force THEN '{}'::jsonb" in sql
    assert "CREATE TABLE" not in sql.split("-- ── 3.")[0], "no second ledger"


# ── cancel ──────────────────────────────────────────────────────────────────

def test_request_run_halt_makes_the_next_model_call_raise_a_cancel(monkeypatch):
    """The mechanism Cancel relies on, end to end at the observability layer."""
    observability.start_run_spend_cap(25.0)
    try:
        observability.assert_under_spend_cap()  # under cap: no raise
        assert observability.request_run_halt("cancelled via dashboard by ops@x") is True
        with pytest.raises(RunHaltRequested) as exc:
            observability.assert_under_spend_cap()
        assert "cancelled via dashboard by ops@x" in str(exc.value)
        # It is still a SpendCapError, so the pipeline's existing handler
        # (retryable, stage preserved, no draft-fail increment) applies.
        assert isinstance(exc.value, SpendCapError)
        assert classify_exception(exc.value) == ErrorType.CANCELLED
    finally:
        observability.reset_run_spend_cap()


def test_halt_with_no_active_run_reports_false():
    observability.reset_run_spend_cap()
    assert observability.request_run_halt("x") is False


def test_latest_spend_is_readable_from_another_thread():
    """The heartbeat thread must see the very bucket the cap enforces."""
    import threading

    observability.start_run_spend_cap(25.0)
    try:
        observability._note_provider_cost(0.25, True)
        seen = {}

        def read():
            seen.update(observability.latest_run_spend_snapshot())

        t = threading.Thread(target=read); t.start(); t.join()
        assert seen["spent_usd"] == pytest.approx(0.25)
        assert seen["limit_usd"] == 25.0
        assert seen["provider_calls"] == 1
    finally:
        observability.reset_run_spend_cap()


def test_cancelled_run_is_recorded_as_cancelled_with_its_spend(monkeypatch):
    def _submit(url, *, force=True, retry_dead_letter=False):
        raise RunHaltRequested("cancelled via dashboard by ops@x (spent_usd=0.4100 limit_usd=25.0000)")

    monkeypatch.setattr(main, "submit_single_url", _submit)
    monkeypatch.setattr(worker, "_ledger_snapshot",
                        lambda url: {"state": "failed", "pipeline_stage": "scored", "draft_fail_count": 0})
    monkeypatch.setattr(worker, "latest_run_spend_snapshot", lambda: {"spent_usd": 0.41, "limit_usd": 25.0})

    outcome = worker.run_trigger(
        {"id": "t", "source_url": URL, "trigger_kind": triggers.SUBMIT_URL,
         "requested_by": "ops@cortechconsultinggroup.com"}
    )
    assert outcome["status"] == "cancelled"
    assert outcome["error_kind"] == ErrorType.CANCELLED
    # stage preserved for resume; spend that was incurred is recorded
    assert outcome["result"]["pipeline_stage"] == "scored"
    assert outcome["result"]["draft_fail_count"] == 0
    assert outcome["spend"]["spent_usd"] == pytest.approx(0.41)


def test_pipeline_handler_treats_a_cancel_like_a_cap_halt():
    """ADR 012 §4's handler catches SpendCapError; RunHaltRequested is one."""
    from intelligence import pipeline_stages

    src = inspect.getsource(pipeline_stages.run_opportunity_pipeline)
    assert "except SpendCapError" in src
    assert issubclass(RunHaltRequested, SpendCapError)


def test_cancel_copy_matches_the_mechanism():
    """The UI text is asserted against what the code actually does.

    If someone changes the heartbeat cadence, the halt point, or how spend is
    recorded, this pins that the copy must change with it.
    """
    copy = app_module.CANCEL_COPY
    assert "not instant" in copy
    assert f"every {int(settings.HEARTBEAT_SECONDS)}s" in copy
    assert "before its next model call" in copy
    assert "already in flight finishes" in copy
    assert "cost is incurred and recorded" in copy
    assert "keeps its last saved stage and checkpoint" in copy
    assert "Cancelled spend still counts toward the aggregate cap" in copy

    # …and the worker really does what the copy says.
    hb = inspect.getsource(worker._heartbeat_loop)
    assert "request_run_halt" in hb
    assert "settings.HEARTBEAT_SECONDS" in hb
    fin = inspect.getsource(worker.run_trigger)
    assert '"status": "cancelled"' in fin
    assert "latest_run_spend_snapshot()" in fin
    sql = Path("supabase_migration_dashboard_controls.sql").read_text()
    assert "'cancelled_before_start'" in sql and "'halt_requested'" in sql


def test_cancel_copy_is_rendered_where_cancel_is_offered():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(loader=FileSystemLoader("dashboard/templates"),
                      autoescape=select_autoescape(["html"]))
    job = {"id": "abcdef12-0000", "status": "running", "ui_state": "processing",
           "kind_label": "x", "requested_by": "ops@cortechconsultinggroup.com",
           "requested_at": "2026-09-21T00:00:00", "started_at": None, "finished_at": None,
           "last_heartbeat_at": None, "attempt_count": 1, "execution_id": None,
           "source_url": URL, "error_message": None, "cancel_requested_at": None,
           "spend_usd": None, "spend_limit_usd": None, "provider_calls": None, "spend_known": None}
    html = env.get_template("job.html").render(
        job=job, d=None, notify=True, cancel_copy=app_module.CANCEL_COPY,
        view="queue", viewer="ops", csrf_token="t", poll_ms=5000, static_base="/s",
        url_queue="/", url_portfolio="/p", url_detail="/o", url_job="/job", url_logout="/l",
        url_trigger_cancel="/trigger/cancel", url_draft="/draft", generated_at="now",
        flash="", flash_kind="",
    )
    import html as _html

    assert "Cancel" in html
    # Autoescape entity-encodes the apostrophes; compare the decoded text.
    assert app_module.CANCEL_COPY in _html.unescape(html)


# ── re-run from stage ───────────────────────────────────────────────────────

def test_rerun_rewinds_then_resumes_without_force(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "rewind_opportunity_stage",
                        lambda url, stage: calls.append(("rewind", stage)) or (True, "rewound"))
    monkeypatch.setattr(main, "submit_single_url",
                        lambda url, *, force=True, retry_dead_letter=False:
                        calls.append(("submit", force, retry_dead_letter)) or {"execution_id": "e"})
    monkeypatch.setattr(worker, "_ledger_snapshot", lambda url: {"state": "completed", "pipeline_stage": "drafted"})

    worker.run_trigger({"id": "t", "source_url": URL, "trigger_kind": triggers.RERUN_FROM_SCORED,
                        "requested_by": "ops@cortechconsultinggroup.com"})
    assert calls == [("rewind", "scored"), ("submit", False, False)]

    calls.clear()
    worker.run_trigger({"id": "t", "source_url": URL, "trigger_kind": triggers.RERUN_FROM_EXTRACTED,
                        "requested_by": "ops@cortechconsultinggroup.com"})
    assert calls == [("rewind", "extracted"), ("submit", False, False)]


def test_rerun_refused_rewind_does_not_touch_the_pipeline(monkeypatch):
    monkeypatch.setattr(worker, "rewind_opportunity_stage",
                        lambda url, stage: (False, "a worker holds the lease — cancel or wait first"))
    called = {"n": 0}
    monkeypatch.setattr(main, "submit_single_url", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(worker, "_ledger_snapshot", lambda url: {})

    out = worker.run_trigger({"id": "t", "source_url": URL, "trigger_kind": triggers.RERUN_FROM_SCORED,
                              "requested_by": "ops@cortechconsultinggroup.com"})
    assert out["status"] == "failed"
    assert "holds the lease" in out["error_message"]
    assert called["n"] == 0


def test_rewind_sql_strips_exactly_the_later_stage_keys():
    """Key ownership must match OpportunityContext.to_checkpoint()."""
    from intelligence.pipeline_stages import OpportunityContext

    ckpt_keys = set(inspect.getsource(OpportunityContext.to_checkpoint).split('"')[1::2])
    sql = Path("supabase_migration_dashboard_controls.sql").read_text()
    draft_keys = {"matched_team_result", "budget", "proposal_sections", "matrix", "submission_type"}
    for k in draft_keys:
        assert f"- '{k}'" in sql, f"re-run from scored must drop {k}"
        assert k in ckpt_keys
    for k in ("full_text", "title", "opp_id", "source_url", "dedup_url"):
        assert f"'{k}'" in sql and k in ckpt_keys


def test_rewind_only_targets_agent_stages_that_resume_can_continue_from():
    from database import supabase_client as sc

    assert sc.rewind_opportunity_stage(URL, "discovered") == (False, "to_stage must be extracted or scored")
    assert sc.rewind_opportunity_stage(URL, "drafted")[0] is False


# ── parity still holds with the new kinds ───────────────────────────────────

def test_every_trigger_kind_enters_through_submit_single_url():
    src = inspect.getsource(worker.run_trigger)
    assert src.count("main.submit_single_url(") == 1, "one entry point, parametrised"
    for kind in triggers.KINDS:
        assert worker._pipeline_args(kind).keys() <= {"force", "retry_dead_letter"}
    assert worker._pipeline_args(triggers.SUBMIT_URL) == {"force": True}
    assert worker._pipeline_args(triggers.DRAFT_EXISTING) == {"force": False}
    assert worker._pipeline_args(triggers.RETRY_DEAD_LETTER) == {"force": False, "retry_dead_letter": True}


def test_identity_travels_with_every_action(monkeypatch):
    """requested_by is the audit trail; it must reach the row and the halt reason."""
    sent = {}
    monkeypatch.setattr(triggers, "_rpc", lambda name, params: sent.setdefault(name, params) and "id")
    triggers.enqueue(URL, triggers.SUBMIT_URL, "ops@cortechconsultinggroup.com")
    assert sent["enqueue_dashboard_trigger"]["p_requested_by"] == "ops@cortechconsultinggroup.com"
    triggers.cancel("some-id", "ops@cortechconsultinggroup.com")
    assert sent["cancel_dashboard_trigger"]["p_cancelled_by"] == "ops@cortechconsultinggroup.com"
    assert "requested_by" in inspect.getsource(worker._heartbeat_loop)
