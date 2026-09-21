"""The ``dashboard_triggers`` request queue.

A Supabase table plus a handful of RPCs, rather than a message broker — this
repo has stayed dependency-lean by design and a queue whose depth is "a few
clicks an hour" does not justify Redis.

Nothing in here decides pipeline state. ``opportunity_processing`` remains the
single ledger (ADR 011 §1); a row here records that a human asked for a run,
who they were, what it has cost so far, and what became of the request.

Trigger kinds — every one maps onto an existing pipeline entry point:

    submit_url            main.submit_single_url(url)                      force=True
    draft_existing        main.submit_single_url(url, force=False)         resume
    retry_dead_letter     main.submit_single_url(url, force=False,
                                                 retry_dead_letter=True)   resume
    rerun_from_extracted  rewind_opportunity_stage(url, "extracted") then resume
    rerun_from_scored     rewind_opportunity_stage(url, "scored")    then resume
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from database.supabase_client import get_supabase
from utils.urls import canonicalize_url

SUBMIT_URL = "submit_url"
DRAFT_EXISTING = "draft_existing"
RETRY_DEAD_LETTER = "retry_dead_letter"
RERUN_FROM_EXTRACTED = "rerun_from_extracted"
RERUN_FROM_SCORED = "rerun_from_scored"
KINDS = (SUBMIT_URL, DRAFT_EXISTING, RETRY_DEAD_LETTER, RERUN_FROM_EXTRACTED, RERUN_FROM_SCORED)

#: Human-readable, used in the UI and in the audit trail.
KIND_LABELS = {
    SUBMIT_URL: "run from URL (full pipeline)",
    DRAFT_EXISTING: "draft — resume from checkpoint",
    RETRY_DEAD_LETTER: "retry dead-letter — resume from checkpoint",
    RERUN_FROM_EXTRACTED: "re-run from extracted (re-score + re-draft)",
    RERUN_FROM_SCORED: "re-run from scored (re-draft only)",
}

_SELECT = (
    "id,source_url,trigger_kind,status,requested_by,requested_at,started_at,"
    "finished_at,attempt_count,error_kind,error_message,execution_id,result_summary,"
    "cancel_requested_at,cancelled_by,last_heartbeat_at,spend_usd,spend_limit_usd,"
    "provider_calls,spend_known"
)


class QueueUnavailable(RuntimeError):
    """The dashboard_triggers table or its RPCs are missing."""


def _is_missing_queue(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "pgrst202" in text
        or "pgrst205" in text
        or ("dashboard_triggers" in text and "does not exist" in text)
        or ("could not find the table" in text and "dashboard_triggers" in text)
        or ("could not find the function" in text and "dashboard_" in text)
    )


def _rpc(name: str, params: dict) -> Any:
    try:
        return get_supabase().rpc(name, params).execute().data
    except Exception as exc:
        if _is_missing_queue(exc):
            raise QueueUnavailable(
                "dashboard_triggers is missing or out of date — apply "
                "supabase_migration_dashboard_triggers.sql and "
                "supabase_migration_dashboard_controls.sql"
            ) from exc
        raise


def _first(data: Any) -> dict | None:
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else None
    return data if isinstance(data, dict) else None


def _as_bool(data: Any) -> bool:
    if isinstance(data, list):
        return bool(data[0]) if data else False
    return bool(data)


# ── enqueue ─────────────────────────────────────────────────────────────────

def enqueue(source_url: str, kind: str, requested_by: str) -> str | None:
    """Queue a run. Returns the trigger id, or ``None`` if one is in flight.

    ``None`` is the database's unique partial index refusing a duplicate, not
    an error — a second click while a run is queued/running is a no-op.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown trigger kind: {kind}")
    canonical = canonicalize_url(source_url) or source_url
    if not canonical:
        raise ValueError("empty source_url")
    data = _rpc(
        "enqueue_dashboard_trigger",
        {
            "p_source_url": canonical,
            "p_trigger_kind": kind,
            "p_requested_by": (requested_by or "unknown")[:320],
        },
    )
    if isinstance(data, list):
        data = data[0] if data else None
    return str(data) if data else None


def enqueue_many(source_urls: list[str], kind: str, requested_by: str) -> list[dict]:
    """Bulk trigger. One row per URL; each is its own job on the worker.

    Fan-out happens here at the queue, not inside the HTTP request — the
    request only inserts rows. Returns one entry per input URL with the id
    or the reason it was not queued, so the UI can report each one.
    """
    out = []
    for url in source_urls:
        try:
            tid = enqueue(url, kind, requested_by)
            out.append({"source_url": url, "id": tid, "queued": tid is not None,
                        "reason": "" if tid else "already queued or running"})
        except ValueError as exc:
            out.append({"source_url": url, "id": None, "queued": False, "reason": str(exc)})
    return out


# ── worker side ─────────────────────────────────────────────────────────────

def claim(worker_token: str, lease_seconds: int = 3600) -> dict | None:
    """Atomically take the next request, or reclaim an expired lease."""
    row = _first(_rpc(
        "claim_dashboard_trigger",
        {"p_worker_token": worker_token, "p_lease_seconds": int(lease_seconds)},
    ))
    if not row:
        return None
    # Same discipline as claim_opportunity_processing: the database echoes the
    # token it accepted, and a mismatch fails safe rather than letting this
    # worker finalise somebody else's lease.
    if str(row.get("worker_token") or "") != worker_token:
        logger.error("dashboard trigger claim did not return its ownership token")
        return None
    return row


def heartbeat(
    trigger_id: str,
    worker_token: str,
    lease_seconds: int = 3600,
    *,
    spend: dict | None = None,
) -> tuple[bool, bool]:
    """Extend the lease, report live spend, learn whether a cancel is pending.

    Returns ``(alive, cancel_requested)``. ``spend`` is the dict from
    ``utils.observability.latest_run_spend_snapshot()`` — the same bucket the
    cap enforces against — or None to report nothing new.
    """
    params: dict = {
        "p_id": trigger_id,
        "p_worker_token": worker_token,
        "p_lease_seconds": int(lease_seconds),
    }
    if spend:
        params.update({
            "p_spend_usd": float(spend.get("spent_usd") or 0.0),
            "p_spend_limit": float(spend.get("limit_usd") or 0.0),
            "p_provider_calls": int(spend.get("provider_calls") or 0),
            "p_spend_known": bool(spend.get("cost_known", True)),
        })
    try:
        row = _first(_rpc("heartbeat_dashboard_trigger", params))
    except Exception as exc:
        logger.warning(f"trigger heartbeat failed (non-fatal): {exc}")
        return False, False
    if not row:
        return False, False
    return bool(row.get("alive")), bool(row.get("cancel_requested"))


def finish(
    trigger_id: str,
    worker_token: str,
    *,
    status: str,
    error_kind: str | None = None,
    error_message: str | None = None,
    execution_id: str | None = None,
    result: dict[str, Any] | None = None,
    spend: dict | None = None,
) -> bool:
    if status not in ("succeeded", "failed", "cancelled"):
        raise ValueError(f"bad trigger status: {status}")
    params: dict = {
        "p_id": trigger_id,
        "p_worker_token": worker_token,
        "p_status": status,
        "p_error_kind": error_kind,
        "p_error_message": (error_message or "")[:2000] or None,
        "p_execution_id": execution_id,
        "p_result": result or {},
    }
    if spend:
        params.update({
            "p_spend_usd": float(spend.get("spent_usd") or 0.0),
            "p_spend_limit": float(spend.get("limit_usd") or 0.0),
            "p_provider_calls": int(spend.get("provider_calls") or 0),
            "p_spend_known": bool(spend.get("cost_known", True)),
        })
    try:
        return _as_bool(_rpc("finish_dashboard_trigger", params))
    except Exception as exc:
        logger.error(f"could not finalise dashboard trigger {trigger_id}: {exc}")
        return False


# ── control ─────────────────────────────────────────────────────────────────

def cancel(trigger_id: str, cancelled_by: str) -> str:
    """Cancel a job. Returns the database's word for what actually happened:

    ``cancelled_before_start``  it was still queued; the worker never sees it.
    ``halt_requested``          it is running; the worker's next heartbeat
                                asks the pipeline to stop before its next
                                model call. The call in flight completes.
    ``already_<status>``        nothing to cancel.
    ``not_found``
    """
    row = _first(_rpc(
        "cancel_dashboard_trigger",
        {"p_id": trigger_id, "p_cancelled_by": (cancelled_by or "unknown")[:320]},
    ))
    return str((row or {}).get("outcome") or "not_found")


def spend_window(hours: int | None = None) -> dict:
    """Aggregate dashboard spend over the trailing window. Never raises."""
    from dashboard import settings

    h = int(hours or settings.AGGREGATE_WINDOW_HOURS)
    try:
        row = _first(_rpc("dashboard_spend_window", {"p_hours": h})) or {}
    except Exception as exc:
        logger.warning(f"spend window unavailable: {exc}")
        return {"spent_usd": None, "runs": 0, "runs_unknown_cost": 0,
                "hours": h, "cap_usd": settings.AGGREGATE_CAP_USD, "available": False}
    spent = row.get("spent_usd")
    return {
        "spent_usd": float(spent) if spent is not None else 0.0,
        "runs": int(row.get("runs") or 0),
        "runs_unknown_cost": int(row.get("runs_unknown_cost") or 0),
        "window_start": row.get("window_start"),
        "hours": h,
        "cap_usd": settings.AGGREGATE_CAP_USD,
        "available": True,
    }


def aggregate_cap_reached(window: dict | None = None) -> tuple[bool, str]:
    """The worker-level gate. Returns ``(blocked, reason)``.

    Unknown-cost runs count as over: if any run in the window could not price
    its calls we cannot prove we are under the cap, which is the same rule the
    per-run cap applies (utils/observability._note_provider_cost).
    """
    from dashboard import settings

    w = window or spend_window()
    cap = float(settings.AGGREGATE_CAP_USD)
    if not w.get("available", True):
        return True, "aggregate spend could not be read — refusing to start a run blind"
    if w.get("runs_unknown_cost"):
        return True, (
            f"{w['runs_unknown_cost']} run(s) in the last {w['hours']}h had unknown cost; "
            "cannot prove the aggregate cap is not exceeded"
        )
    spent = float(w.get("spent_usd") or 0.0)
    if spent >= cap:
        return True, (
            f"aggregate dashboard spend {spent:.2f} USD in the last {w['hours']}h has "
            f"reached DASHBOARD_AGGREGATE_CAP_USD={cap:.2f}"
        )
    return False, f"{spent:.2f} of {cap:.2f} USD used in the last {w['hours']}h"


# ── reads ───────────────────────────────────────────────────────────────────

_UI_STATE = {
    "queued": "pending",
    "running": "processing",
    "succeeded": "completed",
    "cancelled": "cancelled",
}


def _decorate(row: dict) -> dict:
    row = dict(row)
    status = row.get("status")
    if status == "failed":
        kind = row.get("error_kind")
        row["ui_state"] = {
            "DEAD_LETTER": "dead_letter",
            "SPEND_CAP_ERROR": "spend_cap",
            "CANCELLED": "cancelled",
        }.get(kind, "failed")
    else:
        row["ui_state"] = _UI_STATE.get(status, "pending")
    row["kind_label"] = KIND_LABELS.get(row.get("trigger_kind"), row.get("trigger_kind"))
    row["spend_usd"] = float(row["spend_usd"]) if row.get("spend_usd") is not None else None
    row["spend_limit_usd"] = (
        float(row["spend_limit_usd"]) if row.get("spend_limit_usd") is not None else None
    )
    return row


def get(trigger_id: str) -> dict | None:
    try:
        res = (
            get_supabase().table("dashboard_triggers").select(_SELECT)
            .eq("id", trigger_id).limit(1).execute()
        )
    except Exception as exc:
        logger.warning(f"could not read trigger {trigger_id}: {exc}")
        return None
    rows = res.data or []
    return _decorate(rows[0]) if rows and isinstance(rows[0], dict) else None


def recent(limit: int = 15) -> list[dict]:
    """Most recent requests, newest first. Never raises into a page render."""
    try:
        res = (
            get_supabase().table("dashboard_triggers").select(_SELECT)
            .order("requested_at", desc=True).limit(limit).execute()
        )
        rows = res.data or []
    except Exception as exc:
        if not _is_missing_queue(exc):
            logger.warning(f"could not list dashboard triggers: {exc}")
        return []
    return [_decorate(r) for r in rows if isinstance(r, dict)]


def for_url(source_url: str, limit: int = 5) -> list[dict]:
    canonical = canonicalize_url(source_url) or source_url
    try:
        res = (
            get_supabase().table("dashboard_triggers").select(_SELECT)
            .eq("source_url", canonical).order("requested_at", desc=True)
            .limit(limit).execute()
        )
    except Exception:
        return []
    return [_decorate(r) for r in (res.data or []) if isinstance(r, dict)]


def in_flight_urls() -> set[str]:
    """Canonical URLs with a queued/running request, for disabling buttons."""
    try:
        res = (
            get_supabase().table("dashboard_triggers").select("source_url")
            .in_("status", ["queued", "running"]).limit(500).execute()
        )
    except Exception:
        return set()
    return {r.get("source_url") for r in (res.data or []) if isinstance(r, dict)}


def in_flight_by_url() -> dict[str, dict]:
    """Canonical URL → the queued/running trigger row (for job links)."""
    try:
        res = (
            get_supabase().table("dashboard_triggers").select(_SELECT)
            .in_("status", ["queued", "running"]).limit(500).execute()
        )
    except Exception:
        return {}
    return {r["source_url"]: _decorate(r) for r in (res.data or []) if isinstance(r, dict)}


def queue_available() -> bool:
    try:
        get_supabase().table("dashboard_triggers").select("id,spend_usd").limit(1).execute()
        return True
    except Exception:
        return False
