"""Background worker for dashboard-triggered runs.

Runs as a separate Render service. LLM calls take minutes, so a trigger cannot
be executed inside an HTTP request.

The only thing this module does to a pipeline is *call it*:

    main.submit_single_url(url, force=..., retry_dead_letter=...)

That is the same function ``python main.py --submit-url`` calls. There is no
parallel orchestration here — no second stage runner, no second ledger, no
re-implemented retry policy. Spend cap, dead-letter, resume and the consultancy
gate all behave exactly as they do under cron, because they are the same code.

Two controls live here and nowhere else, because they are about *requests*,
not about the pipeline:

* the **aggregate cap** (DASHBOARD_AGGREGATE_CAP_USD) — checked before a
  claimed job is started, against spend recorded on trigger rows; and
* **cancel** — the heartbeat sees ``cancel_requested_at`` and calls
  ``utils.observability.request_run_halt()``, which makes the pipeline's own
  spend-cap check raise before the next model call.

    python -m dashboard.worker
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import uuid

from loguru import logger

from config import require_env
from dashboard import settings, triggers
from database.supabase_client import get_supabase, rewind_opportunity_stage
from utils.errors import ErrorType, RunHaltRequested, SpendCapError, classify_exception
from utils.observability import (
    configure_logging,
    latest_run_spend_snapshot,
    request_run_halt,
)
from utils.urls import canonicalize_url

_stop = threading.Event()


def _handle_signal(signum, _frame):
    logger.info(f"worker received signal {signum}; finishing current run then exiting")
    _stop.set()


def _ledger_snapshot(source_url: str) -> dict:
    """Re-read the real ledger row after a run. Never invents an outcome."""
    canonical = canonicalize_url(source_url) or source_url
    try:
        res = (
            get_supabase()
            .table("opportunity_processing")
            .select("state,pipeline_stage,draft_fail_count,last_error")
            .eq("source_url", canonical)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows and isinstance(rows[0], dict) else {}
    except Exception as exc:
        logger.warning(f"could not re-read ledger after run (non-fatal): {exc}")
        return {}


def _failure_from_ledger(snapshot: dict) -> tuple[str, str, str]:
    """Map the ledger's own words to (status, error_kind, error_message).

    The reason shown in the UI is whatever the ledger actually recorded — this
    function classifies it, it does not author it.
    """
    state = (snapshot.get("state") or "").strip()
    last_error = (snapshot.get("last_error") or "").strip()
    low = last_error.lower()

    if state == "dead_letter":
        return "failed", "DEAD_LETTER", last_error or (
            "drafting dead-lettered after 3 failures (ADR 011 §5)"
        )
    if "cancelled" in low or "halted at operator request" in low:
        return "cancelled", ErrorType.CANCELLED, last_error
    if "spend cap" in low or "max_run_cost_usd" in low:
        return "failed", ErrorType.SPEND_CAP_ERROR, last_error
    if last_error:
        return "failed", ErrorType.INGESTION_ERROR, last_error
    return "failed", ErrorType.INGESTION_ERROR, (
        "the pipeline did not produce a result and recorded no error; "
        f"ledger state={state or 'unknown'}, "
        f"stage={snapshot.get('pipeline_stage') or 'unknown'}"
    )


def _result_summary(result: dict | None, snapshot: dict) -> dict:
    """A small, explicitly non-authoritative echo for the queue list."""
    summary = {
        "ledger_state": snapshot.get("state"),
        "pipeline_stage": snapshot.get("pipeline_stage"),
        "draft_fail_count": snapshot.get("draft_fail_count"),
    }
    if isinstance(result, dict):
        # These are the keys OpportunityContext.to_result() actually emits.
        summary.update({
            "recommendation": result.get("recommendation"),
            "score": result.get("score"),
            "client": result.get("client"),
            "deadline": result.get("deadline"),
            "estimated_cost_usd": result.get("estimated_cost_usd"),
            "draft_path": result.get("_draft_path"),
        })
        summary["has_draft"] = bool(result.get("proposal_sections"))
    return {k: v for k, v in summary.items() if v is not None}


def _pipeline_args(kind: str) -> dict:
    """How each trigger kind enters main.submit_single_url. Nothing else."""
    if kind == triggers.SUBMIT_URL:
        # A pasted URL is exactly `main.py --submit-url`.
        return {"force": True}
    if kind == triggers.RETRY_DEAD_LETTER:
        # Resume a dead_letter row from its checkpoint with a fresh
        # MAX_DRAFT_FAILURES budget (supabase_migration_dashboard_controls.sql).
        return {"force": False, "retry_dead_letter": True}
    # draft_existing / rerun_from_*: honour the Phase 7 checkpoint.
    return {"force": False}


def run_trigger(job: dict) -> dict:
    """Execute one claimed trigger. Returns the finish() kwargs.

    Kept separate from the loop so tests can drive it directly without a
    queue, a signal handler, or a sleep.
    """
    import main  # imported here so the module can be inspected without config

    url = job["source_url"]
    kind = job["trigger_kind"]

    # "Re-run from [stage]" is a ledger rewind followed by the ordinary resume.
    if kind in (triggers.RERUN_FROM_EXTRACTED, triggers.RERUN_FROM_SCORED):
        to_stage = "extracted" if kind == triggers.RERUN_FROM_EXTRACTED else "scored"
        ok, reason = rewind_opportunity_stage(url, to_stage)
        if not ok:
            return {
                "status": "failed",
                "error_kind": ErrorType.VALIDATION_ERROR,
                "error_message": f"could not rewind to {to_stage}: {reason}",
                "execution_id": "",
                "result": _result_summary(None, _ledger_snapshot(url)),
            }

    try:
        result = main.submit_single_url(url, **_pipeline_args(kind))
    except RunHaltRequested as exc:
        snapshot = _ledger_snapshot(url)
        return {
            "status": "cancelled",
            "error_kind": ErrorType.CANCELLED,
            "error_message": str(exc),
            "execution_id": "",
            "result": _result_summary(None, snapshot),
            "spend": latest_run_spend_snapshot(),
        }
    except SpendCapError as exc:
        snapshot = _ledger_snapshot(url)
        return {
            "status": "failed",
            "error_kind": ErrorType.SPEND_CAP_ERROR,
            "error_message": str(exc) or "per-run LLM spend cap reached",
            "execution_id": "",
            "result": _result_summary(None, snapshot),
            "spend": latest_run_spend_snapshot(),
        }
    except Exception as exc:
        snapshot = _ledger_snapshot(url)
        return {
            "status": "failed",
            "error_kind": classify_exception(exc),
            "error_message": f"{type(exc).__name__}: {exc}",
            "execution_id": "",
            "result": _result_summary(None, snapshot),
            "spend": latest_run_spend_snapshot(),
        }

    spend = latest_run_spend_snapshot()
    execution_id = str(result.get("execution_id") or "") if isinstance(result, dict) else ""
    snapshot = _ledger_snapshot(url)

    if result is None:
        status, kind_, message = _failure_from_ledger(snapshot)
        return {
            "status": status,
            "error_kind": kind_,
            "error_message": message,
            "execution_id": execution_id,
            "result": _result_summary(None, snapshot),
            "spend": spend,
        }

    return {
        "status": "succeeded",
        "error_kind": None,
        "error_message": None,
        "execution_id": execution_id,
        "result": _result_summary(result, snapshot),
        "spend": spend,
    }


def _heartbeat_loop(trigger_id: str, token: str, done: threading.Event, requested_by: str) -> None:
    """Every HEARTBEAT_SECONDS: extend the lease, publish live spend, honour cancel.

    Runs in its own thread. ``latest_run_spend_snapshot()`` is the process-
    level view of the very bucket the cap enforces against, so the figures the
    UI shows are the figures the cap will act on — not a parallel estimate.
    """
    halted = False
    while not done.wait(settings.HEARTBEAT_SECONDS):
        spend = latest_run_spend_snapshot()
        _alive, cancel_requested = triggers.heartbeat(
            trigger_id, token, settings.WORKER_LEASE_SECONDS, spend=spend or None
        )
        if cancel_requested and not halted:
            # Cooperative: the call already in flight completes and is paid
            # for; the next complete() raises RunHaltRequested.
            if request_run_halt(f"cancelled via dashboard by {requested_by}"):
                halted = True
                logger.info(
                    f"trigger {trigger_id}: halt requested; stopping before next model call"
                )
            else:
                logger.info(f"trigger {trigger_id}: cancel seen but no run is active yet")


def _with_heartbeat(job: dict, token: str, fn):
    done = threading.Event()
    t = threading.Thread(
        target=_heartbeat_loop,
        args=(str(job["id"]), token, done, str(job.get("requested_by") or "")),
        name="trigger-heartbeat",
        daemon=True,
    )
    t.start()
    try:
        return fn()
    finally:
        done.set()
        t.join(timeout=5)


def run_once(worker_token: str) -> bool:
    """Claim and run at most one trigger. Returns True if work was done."""
    job = triggers.claim(worker_token, settings.WORKER_LEASE_SECONDS)
    if not job:
        return False

    trigger_id = str(job["id"])
    logger.info(
        f"claimed trigger {trigger_id} kind={job['trigger_kind']} "
        f"by={job['requested_by']} url={job['source_url'][:80]}"
    )

    # ── Aggregate cap: enforced here, before any provider call ──
    # A user who queues forty jobs gets forty rows; every one of them passes
    # through this gate, so the cap holds no matter how many were queued.
    blocked, reason = triggers.aggregate_cap_reached()
    if blocked:
        triggers.finish(
            trigger_id, worker_token,
            status="failed",
            error_kind=ErrorType.SPEND_CAP_ERROR,
            error_message=f"not started: {reason}",
            result={"gate": "aggregate_cap"},
        )
        logger.warning(f"trigger {trigger_id} refused by aggregate cap: {reason}")
        return True

    try:
        outcome = _with_heartbeat(job, worker_token, lambda: run_trigger(job))
    except BaseException as exc:
        # Never leave a trigger stuck in `running` because of an unexpected
        # crash; the lease would eventually expire, but the UI should say why.
        triggers.finish(
            trigger_id,
            worker_token,
            status="failed",
            error_kind=classify_exception(exc) if isinstance(exc, Exception) else "FATAL",
            error_message=f"worker aborted: {type(exc).__name__}: {exc}",
            spend=latest_run_spend_snapshot() or None,
        )
        raise

    triggers.finish(trigger_id, worker_token, **outcome)
    logger.info(f"trigger {trigger_id} finished: {outcome['status']}")
    return True


def main_loop() -> int:
    configure_logging()
    require_env()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "IDENTITY: cortech-bd-worker — dashboard background worker. Consumes "
        "dashboard_triggers only; it has NO schedule of its own and never runs "
        "discovery. Each job calls main.submit_single_url() for one URL."
    )
    worker_token = f"{os.getenv('RENDER_INSTANCE_ID') or 'local'}-{uuid.uuid4()}"
    logger.info(
        f"dashboard worker up (token {worker_token[:24]}…); aggregate cap "
        f"{settings.AGGREGATE_CAP_USD:.2f} USD / {settings.AGGREGATE_WINDOW_HOURS}h"
    )

    if not triggers.queue_available():
        logger.critical(
            "dashboard_triggers is not present or predates the controls migration. "
            "Apply supabase_migration_dashboard_triggers.sql and "
            "supabase_migration_dashboard_controls.sql. Exiting so the platform "
            "surfaces this rather than idling silently."
        )
        return 1

    idle = settings.WORKER_POLL_SECONDS
    while not _stop.is_set():
        try:
            did_work = run_once(worker_token)
        except triggers.QueueUnavailable as exc:
            logger.critical(str(exc))
            return 1
        except Exception as exc:
            logger.exception(f"worker iteration failed: {exc}")
            did_work = False
        if not did_work:
            _stop.wait(idle)
    logger.info("dashboard worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main_loop())
