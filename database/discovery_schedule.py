"""Persistent lease for the one production recurring discovery runner."""
from __future__ import annotations

import uuid
from typing import Any

from database.supabase_client import get_supabase


class DiscoveryScheduleUnavailable(RuntimeError):
    """The durable schedule migration or its RPCs are not installed."""


def _bool(result: Any) -> bool:
    value = getattr(result, "data", False)
    if isinstance(value, list):
        if not value:
            return False
        first = value[0]
        if isinstance(first, dict):
            first = first.get("result", first.get("claimed", next(iter(first.values()), False)))
        value = first
    return value is True or str(value).lower() == "true"


def claim_due(*, token: str | None = None, lease_seconds: int = 900) -> tuple[str, bool]:
    token = str(token or uuid.uuid4())
    try:
        result = get_supabase().rpc("claim_discovery_schedule", {
            "p_token": token,
            "p_lease_seconds": max(120, min(int(lease_seconds), 1800)),
        }).execute()
    except Exception as exc:
        raise DiscoveryScheduleUnavailable(
            "The discovery schedule migration is not available; no run was started."
        ) from exc
    return token, _bool(result)


def finish(
    token: str,
    *,
    outcome: str,
    error: str = "",
    summary: dict | None = None,
    interval_seconds: int = 7200,
) -> bool:
    if outcome not in {"success", "partial", "failure"}:
        raise ValueError("invalid discovery schedule outcome")
    try:
        result = get_supabase().rpc("finish_discovery_schedule", {
            "p_token": token,
            "p_outcome": outcome,
            "p_interval_seconds": max(3600, min(int(interval_seconds), 86400)),
            "p_error": str(error or "")[:500],
            "p_summary": summary or {},
        }).execute()
    except Exception as exc:
        raise DiscoveryScheduleUnavailable("Could not finalize the discovery schedule lease.") from exc
    return _bool(result)


def status() -> dict:
    try:
        result = get_supabase().table("discovery_schedule_state").select(
            "next_run_at,last_attempt_at,last_success_at,last_outcome,last_error,last_summary,lease_expires_at"
        ).eq("schedule_key", "production").limit(1).execute()
    except Exception as exc:
        raise DiscoveryScheduleUnavailable(
            "The discovery schedule status is unavailable; apply supabase_migration_discovery_schedule.sql."
        ) from exc
    rows = getattr(result, "data", None) or []
    return rows[0] if rows and isinstance(rows[0], dict) else {}
