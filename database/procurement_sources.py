"""Durable configuration and health state for procurement sources.

All source configuration lives in Supabase. The monitor deliberately treats a
missing registry migration as an empty custom-source set so existing built-in
discovery remains available while an operator applies the documented schema.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from database.supabase_client import get_supabase
from utils.urls import canonicalize_url

SUPPORTED_CAPABILITIES = frozenset({"rss_atom", "public_listing"})


class SourceRegistryUnavailable(RuntimeError):
    """The durable source table or RPCs have not been installed."""


class DuplicateSource(ValueError):
    """A source with the same normalized URL already exists."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result_rows(result: Any) -> list[dict]:
    data = getattr(result, "data", None)
    return [row for row in (data or []) if isinstance(row, dict)]


def _rpc_bool(result: Any) -> bool:
    """Interpret a PostgREST scalar bool across client return shapes."""
    data = getattr(result, "data", False)
    if isinstance(data, list):
        if not data:
            return False
        first = data[0]
        if isinstance(first, dict):
            for key in ("result", "claimed", "enabled"):
                if key in first:
                    first = first[key]
                    break
            else:
                first = next(iter(first.values()), False) if len(first) == 1 else False
        return first is True or str(first).lower() == "true"
    return data is True or str(data).lower() == "true"


def _source_key(url: str) -> str:
    return canonicalize_url(url, preserve_query_keys={"ref"})


def builtin_source_key(name: str, url: str) -> str:
    normalized = canonicalize_url(url, preserve_query_keys={"ref"})
    digest = hashlib.sha256(f"{name}\n{normalized}".encode()).hexdigest()[:20]
    return f"builtin:{digest}"


def normalize_source_url(value: str) -> str:
    """Normalize a user supplied web URL and reject unsafe URL forms."""
    from utils.urls import UnsafeURLError, assert_public_http_url

    raw = str(value or "").strip()
    if not raw or any(ord(char) < 32 for char in raw):
        raise UnsafeURLError("Enter a valid HTTP(S) URL.")
    if "://" not in raw:
        raw = "https://" + raw
    assert_public_http_url(raw, resolve=False)
    normalized = canonicalize_url(raw, preserve_query_keys={"ref"})
    if not normalized:
        raise UnsafeURLError("Enter a valid HTTP(S) URL.")
    return normalized


def ensure_builtin_sources(definitions: list[dict]) -> dict[str, dict]:
    """Create health rows for adapters without changing their enabled state."""
    rows_by_key: dict[str, dict] = {}
    try:
        client = get_supabase()
    except Exception:
        return rows_by_key

    for source in definitions:
        try:
            name = str(source.get("name") or "Built-in source")[:160]
            url = normalize_source_url(str(source.get("url") or ""))
            key = builtin_source_key(name, url)
            found = client.table("procurement_sources").select("id").eq(
                "source_key", key
            ).limit(1).execute()
            if _result_rows(found):
                rows_by_key[key] = _result_rows(found)[0]
                continue
            # A built-in URL may already have been registered as custom before
            # the first Sources page visit. Keep it visible in the dashboard
            # through its static definition, and continue seeding other rows.
            found = client.table("procurement_sources").select("id,source_key").eq(
                "url", url
            ).limit(1).execute()
            if _result_rows(found):
                existing = _result_rows(found)[0]
                if existing.get("source_key") == key:
                    rows_by_key[key] = existing
                continue
            inserted = client.table("procurement_sources").insert({
                "source_key": key,
                "name": name,
                "url": url,
                "source_type": "builtin",
                "capability": "adapter",
                "adapter_config": source,
                "enabled": True,
                "status": "ready",
            }).execute()
            inserted_rows = _result_rows(inserted)
            if inserted_rows:
                rows_by_key[key] = inserted_rows[0]
            else:
                found = client.table("procurement_sources").select("*").eq(
                    "source_key", key
                ).limit(1).execute()
                if _result_rows(found):
                    rows_by_key[key] = _result_rows(found)[0]
        except Exception:
            # The source registry is an optional health surface. A row-level
            # error must not suppress other built-ins or discovery itself.
            continue
    return rows_by_key


def list_sources(*, include_builtins: bool = True) -> list[dict]:
    try:
        query = get_supabase().table("procurement_sources").select("*")
        result = query.execute()
    except Exception as exc:
        raise SourceRegistryUnavailable(
            "Source storage is unavailable. Apply supabase_migration_procurement_sources.sql."
        ) from exc
    rows = _result_rows(result)
    if include_builtins:
        return rows
    return [row for row in rows if row.get("source_type") == "custom"]


def get_source(source_id: str) -> dict | None:
    try:
        rows = _result_rows(
            get_supabase().table("procurement_sources").select("*")
            .eq("id", source_id).limit(1).execute()
        )
        return rows[0] if rows else None
    except Exception as exc:
        raise SourceRegistryUnavailable(
            "Source storage is unavailable. Apply the procurement sources migration."
        ) from exc


def create_custom_source(
    *, name: str, url: str, capability: str, probe_success: bool,
    error: str = "", preview: list[dict] | None = None, created_by: str = "",
) -> dict:
    canonical = normalize_source_url(url)
    client = get_supabase()
    try:
        duplicate = _result_rows(
            client.table("procurement_sources").select("id,name")
            .eq("url", canonical).limit(1).execute()
        )
    except Exception as exc:
        raise SourceRegistryUnavailable(
            "Source storage is unavailable. Apply supabase_migration_procurement_sources.sql."
        ) from exc
    if duplicate:
        raise DuplicateSource("A source with this URL already exists.")

    now = _now()
    payload = {
        "source_key": _source_key(canonical),
        "name": str(name or "").strip()[:160],
        "url": canonical,
        "source_type": "custom",
        "capability": capability,
        "adapter_config": {},
        "enabled": False,
        "status": "paused" if probe_success else "needs_attention",
        "last_attempt_at": now,
        "last_success_at": now if probe_success else None,
        "last_error": (error or "")[:500],
        "last_preview": (preview or [])[:5] if probe_success else [],
        "created_by": str(created_by or "")[:320],
    }
    try:
        inserted = _result_rows(client.table("procurement_sources").insert(payload).execute())
    except Exception as exc:
        # The unique URL index handles races between concurrent adds.
        try:
            duplicate = _result_rows(
                client.table("procurement_sources").select("id")
                .eq("url", canonical).limit(1).execute()
            )
        except Exception:
            duplicate = []
        if duplicate:
            raise DuplicateSource("A source with this URL already exists.") from exc
        raise SourceRegistryUnavailable(
            "Could not save this source. Confirm the source registry migration is applied."
        ) from exc
    if inserted:
        return inserted[0]
    found = _result_rows(
        client.table("procurement_sources").select("*")
        .eq("url", canonical).limit(1).execute()
    )
    if found:
        raise DuplicateSource("A source with this URL already exists.")
    raise SourceRegistryUnavailable("The source was not saved.")


def create_pending_custom_source(*, name: str, url: str, created_by: str = "") -> dict:
    """Persist a unique inactive source before its bounded capability check."""
    canonical = normalize_source_url(url)
    client = get_supabase()
    try:
        duplicate = _result_rows(
            client.table("procurement_sources").select("id")
            .eq("url", canonical).limit(1).execute()
        )
    except Exception as exc:
        raise SourceRegistryUnavailable(
            "Source storage is unavailable. Apply supabase_migration_procurement_sources.sql."
        ) from exc
    if duplicate:
        raise DuplicateSource("A source with this URL already exists.")

    payload = {
        "source_key": _source_key(canonical),
        "name": str(name or "").strip()[:160],
        "url": canonical,
        "source_type": "custom",
        "capability": "checking",
        "adapter_config": {},
        "enabled": False,
        "status": "pending",
        "last_error": "",
        "last_preview": [],
        "created_by": str(created_by or "")[:320],
    }
    try:
        inserted = _result_rows(client.table("procurement_sources").insert(payload).execute())
    except Exception as exc:
        try:
            duplicate = _result_rows(
                client.table("procurement_sources").select("id")
                .eq("url", canonical).limit(1).execute()
            )
        except Exception:
            duplicate = []
        if duplicate:
            raise DuplicateSource("A source with this URL already exists.") from exc
        raise SourceRegistryUnavailable(
            "Could not save this source. Confirm the source registry migration is applied."
        ) from exc
    if inserted:
        return inserted[0]
    raise SourceRegistryUnavailable("The source was not saved.")


def claim_source_run(
    source_id: str, token: str, *, allow_disabled: bool = False,
    respect_backoff: bool = True, lease_seconds: int = 120,
) -> bool:
    try:
        result = get_supabase().rpc("claim_procurement_source_run", {
            "p_source_id": source_id,
            "p_token": token,
            "p_lease_seconds": max(30, min(int(lease_seconds), 600)),
            "p_allow_disabled": bool(allow_disabled),
            "p_respect_backoff": bool(respect_backoff),
        }).execute()
    except Exception as exc:
        raise SourceRegistryUnavailable(
            "Source checks are unavailable. Apply the procurement sources migration."
        ) from exc
    return _rpc_bool(result)

def source_is_enabled(source_id: str) -> bool:
    row = get_source(source_id)
    return bool(row and row.get("enabled"))


def finish_source_run(
    source_id: str, token: str, *, success: bool, error: str = "",
    discovered_delta: int = 0, duplicate_delta: int = 0,
    filtered_delta: int = 0, stored_delta: int | None = None,
    preview: list[dict] | None = None,
    capability: str | None = None, cancelled: bool = False,
) -> bool:
    # ``discovered_delta`` was historically the number inserted into the
    # opportunity ledger. Preserve that meaning for older callers while the
    # new scheduled runner supplies all four separately measured counters.
    if stored_delta is None:
        stored_delta = discovered_delta
    try:
        result = get_supabase().rpc("finish_procurement_source_run_with_counts", {
            "p_source_id": source_id,
            "p_token": token,
            "p_outcome": "cancelled" if cancelled else ("success" if success else "failure"),
            "p_error": (error or "")[:500],
            "p_discovered_delta": max(0, min(int(discovered_delta), 1000)),
            "p_duplicate_delta": max(0, min(int(duplicate_delta), 1000)),
            "p_filtered_delta": max(0, min(int(filtered_delta), 1000)),
            "p_stored_delta": max(0, min(int(stored_delta), 1000)),
            "p_preview": (preview or [])[:5],
            "p_capability": capability,
        }).execute()
    except Exception:
        return False
    return _rpc_bool(result)


def set_custom_source_enabled(source_id: str, enabled: bool) -> bool:
    """Enable only a successfully validated supported custom source."""
    try:
        result = get_supabase().rpc("set_procurement_source_enabled", {
            "p_source_id": source_id,
            "p_enabled": bool(enabled),
        }).execute()
    except Exception as exc:
        raise SourceRegistryUnavailable(
            "Could not update this source. Confirm the procurement sources migration is applied."
        ) from exc
    return _rpc_bool(result)
