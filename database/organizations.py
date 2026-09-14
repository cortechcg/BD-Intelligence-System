"""Fail-open persistence for canonical organizations (Phase 2).

Missing tables/columns behave like content_hash: log once, return empty, never
crash the pipeline. Do not apply supabase_migration_organizations.sql from
application code.
"""

from __future__ import annotations

from typing import Sequence

from loguru import logger

from intelligence.organizations import (
    MatchResult,
    ObservedRecord,
    OrgIndexEntry,
    STATUS_UNKNOWN,
    match_organization,
    normalize_outcome,
    sanitize_org_name,
)

_MISSING_MIGRATION = (
    "organizations tables missing — apply supabase_migration_organizations.sql. "
    "Client intelligence fail-opens (empty index, no crash)."
)

# None = not probed. False = confirmed missing this process.
_org_tables_available: bool | None = None


def reset_organizations_status() -> None:
    """Test helper."""
    global _org_tables_available
    _org_tables_available = None


def _is_missing_org_relation(exc: Exception) -> bool:
    msg = str(exc).lower()
    tokens = (
        "organizations",
        "organization_aliases",
        "organization_observations",
    )
    if not any(t in msg for t in tokens):
        # PostgREST sometimes names only the missing table in a generic error.
        if "pgrst205" in msg or "pgrst204" in msg or "42p01" in msg or "42703" in msg:
            return True
        return False
    return (
        "pgrst205" in msg
        or "pgrst204" in msg
        or "42p01" in msg
        or "42703" in msg
        or "schema cache" in msg
        or "does not exist" in msg
        or "could not find" in msg
        or "unknown column" in msg
    )


def _is_unique_violation(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "23505" in msg or "duplicate" in msg or "unique" in msg


def _mark_missing(exc: Exception) -> None:
    global _org_tables_available
    _org_tables_available = False
    logger.warning(f"{_MISSING_MIGRATION} Cause: {exc}")


def _client():
    from database.supabase_client import get_supabase

    return get_supabase()


def organizations_available() -> bool:
    global _org_tables_available
    if _org_tables_available is False:
        return False
    if _org_tables_available is True:
        return True
    try:
        _client().table("organizations").select("id").limit(1).execute()
        _org_tables_available = True
        return True
    except Exception as e:
        if _is_missing_org_relation(e):
            _mark_missing(e)
            return False
        logger.warning(f"organizations probe failed (not treating as missing): {e}")
        return False


def load_organization_index() -> tuple[list[OrgIndexEntry], str]:
    """Return (entries, storage_label). Missing table → ([], 'missing')."""
    global _org_tables_available
    if _org_tables_available is False:
        return [], "missing"
    try:
        sb = _client()
        orgs = (
            sb.table("organizations")
            .select("id, canonical_name, normalized_name, normalized_compact, entity_kind")
            .execute()
        )
        rows = orgs.data or []
    except Exception as e:
        if _is_missing_org_relation(e):
            _mark_missing(e)
            return [], "missing"
        logger.warning(f"organizations index load failed (fail-open): {e}")
        return [], "error"

    _org_tables_available = True
    entries: list[OrgIndexEntry] = []
    by_id: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        org_id = str(row["id"])
        by_id[org_id] = row
        entries.append(
            OrgIndexEntry(
                organization_id=org_id,
                canonical_name=str(row.get("canonical_name") or ""),
                normalized_name=str(row.get("normalized_name") or ""),
                normalized_compact=str(row.get("normalized_compact") or ""),
                alias_normalized=str(row.get("normalized_name") or ""),
                alias_compact=str(row.get("normalized_compact") or ""),
                alias_source="observed",
                entity_kind=str(row.get("entity_kind") or "unknown"),
            )
        )
    try:
        aliases = (
            sb.table("organization_aliases")
            .select(
                "organization_id, alias_raw, alias_normalized, alias_compact, alias_source"
            )
            .execute()
        )
        for row in aliases.data or []:
            if not isinstance(row, dict):
                continue
            org_id = str(row.get("organization_id") or "")
            parent = by_id.get(org_id)
            if not parent:
                continue
            entries.append(
                OrgIndexEntry(
                    organization_id=org_id,
                    canonical_name=str(parent.get("canonical_name") or ""),
                    normalized_name=str(parent.get("normalized_name") or ""),
                    normalized_compact=str(parent.get("normalized_compact") or ""),
                    alias_normalized=str(row.get("alias_normalized") or ""),
                    alias_compact=str(row.get("alias_compact") or ""),
                    alias_source=str(row.get("alias_source") or "observed"),
                    entity_kind=str(parent.get("entity_kind") or "unknown"),
                )
            )
    except Exception as e:
        if _is_missing_org_relation(e):
            logger.warning(f"{_MISSING_MIGRATION} aliases: {e}")
        else:
            logger.warning(f"organization_aliases load failed (fail-open): {e}")
    return entries, "supabase"


def _fetch_org_by_normalized(spaced: str, compact: str) -> dict | None:
    try:
        sb = _client()
        if spaced:
            result = (
                sb.table("organizations")
                .select("id, canonical_name, normalized_name, normalized_compact, entity_kind")
                .eq("normalized_name", spaced)
                .limit(1)
                .execute()
            )
            rows = result.data or []
            if rows and isinstance(rows[0], dict):
                return rows[0]
        if compact and len(compact) >= 6:
            result = (
                sb.table("organizations")
                .select("id, canonical_name, normalized_name, normalized_compact, entity_kind")
                .eq("normalized_compact", compact)
                .limit(1)
                .execute()
            )
            rows = result.data or []
            if rows and isinstance(rows[0], dict):
                return rows[0]
    except Exception as e:
        logger.warning(f"organizations lookup failed (fail-open): {e}")
    return None


def persist_match(match: MatchResult, *, entity_kind: str = "unknown") -> MatchResult:
    """Insert a new candidate or record this spelling as an alias. Fail-open."""
    if match.method == "empty" or not match.query_spaced:
        return match
    if _org_tables_available is False:
        return match
    try:
        sb = _client()
    except Exception as e:
        logger.warning(f"organizations persist skipped (fail-open): {e}")
        return match

    kind = entity_kind if entity_kind in {"client", "donor", "both", "unknown"} else "unknown"

    if match.is_new_candidate:
        payload = {
            "id": match.organization_id,
            "canonical_name": match.canonical_name or match.query_sanitized,
            "normalized_name": match.query_spaced,
            "normalized_compact": match.query_compact,
            "entity_kind": kind,
        }
        try:
            sb.table("organizations").insert(payload).execute()
        except Exception as e:
            if _is_missing_org_relation(e):
                _mark_missing(e)
                return match
            if _is_unique_violation(e):
                existing = _fetch_org_by_normalized(match.query_spaced, match.query_compact)
                if existing and existing.get("id"):
                    return MatchResult(
                        status="VERIFIED",
                        method="exact_spaced",
                        confidence=1.0,
                        organization_id=str(existing["id"]),
                        canonical_name=str(existing.get("canonical_name") or match.canonical_name),
                        is_new_candidate=False,
                        query_raw=match.query_raw,
                        query_sanitized=match.query_sanitized,
                        query_spaced=match.query_spaced,
                        query_compact=match.query_compact,
                        entity_kind=str(existing.get("entity_kind") or kind),
                    )
                return match
            logger.warning(f"organizations insert failed (fail-open): {e}")
            return match
        _insert_alias(sb, match, "observed")
        return match

    if match.organization_id:
        _insert_alias(sb, match, "observed")
        _maybe_update_kind(sb, match.organization_id, kind)
    return match


def _insert_alias(sb, match: MatchResult, source: str) -> None:
    if not match.organization_id or not match.query_spaced:
        return
    try:
        sb.table("organization_aliases").insert({
            "organization_id": match.organization_id,
            "alias_raw": match.query_sanitized or match.query_raw,
            "alias_normalized": match.query_spaced,
            "alias_compact": match.query_compact,
            "alias_source": source,
        }).execute()
    except Exception as e:
        if _is_missing_org_relation(e):
            _mark_missing(e)
            return
        if _is_unique_violation(e):
            return
        logger.warning(f"organization_aliases insert failed (fail-open): {e}")


def _maybe_update_kind(sb, org_id: str, kind: str) -> None:
    if kind not in {"client", "donor"}:
        return
    try:
        result = (
            sb.table("organizations")
            .select("entity_kind")
            .eq("id", org_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows or not isinstance(rows[0], dict):
            return
        current = str(rows[0].get("entity_kind") or "unknown")
        nxt = current
        if current in {"unknown", kind}:
            nxt = kind
        elif current in {"client", "donor"} and current != kind:
            nxt = "both"
        if nxt != current:
            sb.table("organizations").update({"entity_kind": nxt}).eq("id", org_id).execute()
    except Exception as e:
        logger.warning(f"organizations kind update failed (fail-open): {e}")


def persist_observation(match: MatchResult, record: ObservedRecord) -> None:
    if not match.organization_id or match.method == "empty":
        return
    if _org_tables_available is False:
        return
    try:
        sb = _client()
        sb.table("organization_observations").upsert(
            {
                "organization_id": match.organization_id,
                "role": record.role,
                "source_kind": record.source_kind,
                "source_id": str(record.source_id),
                "title": sanitize_org_name(record.title)[:200],
                "outcome": record.outcome if record.outcome in {"WON", "LOST", "UNKNOWN"} else "UNKNOWN",
                "outcome_status": record.outcome_status
                if record.outcome_status in {"VERIFIED", "INFERRED", "UNKNOWN"}
                else STATUS_UNKNOWN,
                "observed_name": sanitize_org_name(record.observed_name) or match.query_sanitized,
                "match_method": match.method,
                "match_confidence": match.confidence,
                "match_status": match.status,
            },
            on_conflict="organization_id,source_kind,source_id,role",
        ).execute()
    except Exception as e:
        if _is_missing_org_relation(e):
            _mark_missing(e)
            return
        logger.warning(f"organization_observations upsert failed (fail-open): {e}")


def load_observation_records() -> list[ObservedRecord]:
    """All stored observations. Fail-open to []."""
    if _org_tables_available is False:
        return []
    try:
        result = (
            _client()
            .table("organization_observations")
            .select(
                "organization_id, role, source_kind, source_id, title, "
                "outcome, outcome_status, observed_name"
            )
            .execute()
        )
        return _rows_to_records(result.data or [])
    except Exception as e:
        if _is_missing_org_relation(e):
            _mark_missing(e)
            return []
        logger.warning(f"organization_observations load failed (fail-open): {e}")
        return []


def load_records_for_organization(organization_id: str) -> list[ObservedRecord]:
    if not organization_id or _org_tables_available is False:
        return []
    try:
        result = (
            _client()
            .table("organization_observations")
            .select(
                "organization_id, role, source_kind, source_id, title, "
                "outcome, outcome_status, observed_name"
            )
            .eq("organization_id", organization_id)
            .execute()
        )
        return _rows_to_records(result.data or [])
    except Exception as e:
        if _is_missing_org_relation(e):
            _mark_missing(e)
            return []
        logger.warning(f"organization_observations by-org load failed (fail-open): {e}")
        return []


def _rows_to_records(rows) -> list[ObservedRecord]:
    out: list[ObservedRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        outcome, ostatus = row.get("outcome"), row.get("outcome_status")
        if outcome not in {"WON", "LOST", "UNKNOWN"}:
            outcome, ostatus = normalize_outcome(outcome)
        elif ostatus not in {"VERIFIED", "INFERRED", "UNKNOWN"}:
            ostatus = STATUS_UNKNOWN
        out.append(
            ObservedRecord(
                source_kind=str(row.get("source_kind") or "opportunity"),
                source_id=str(row.get("source_id") or ""),
                title=str(row.get("title") or ""),
                role=str(row.get("role") or "client"),
                observed_name=str(row.get("observed_name") or ""),
                outcome=str(outcome),
                outcome_status=str(ostatus),
            )
        )
    return out


def collect_matching_store_records(
    match: MatchResult,
    index: Sequence[OrgIndexEntry],
) -> list[ObservedRecord]:
    """Scan already-stored proposal/win-loss rows; attach only matcher hits."""
    if not match.organization_id or match.method == "empty":
        return []
    out: list[ObservedRecord] = []
    try:
        sb = _client()
    except Exception as e:
        logger.warning(f"org store scan skipped (fail-open): {e}")
        return []

    try:
        rows = (
            sb.table("proposal_embeddings")
            .select("airtable_proposal_id, project_title, won, metadata")
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.warning(f"proposal_embeddings org scan failed (fail-open): {e}")
        rows = []

    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("airtable_proposal_id") or "")
        if not pid:
            continue
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        title = str(row.get("project_title") or "")
        won_flag = row.get("won")
        if won_flag is True or (isinstance(meta.get("won"), bool) and meta.get("won") is True):
            won_flag = True
        else:
            won_flag = row.get("won")
        for role, raw_name in (
            ("client", meta.get("client") or ""),
            ("donor", meta.get("donor") or ""),
        ):
            if not raw_name:
                continue
            key = (pid, role)
            if key in seen:
                continue
            hit = match_organization(raw_name, index)
            if hit.organization_id != match.organization_id or hit.is_new_candidate:
                continue
            outcome, ostatus = normalize_outcome(meta.get("outcome"), won_flag=won_flag)
            seen.add(key)
            out.append(
                ObservedRecord(
                    source_kind="past_proposal",
                    source_id=pid,
                    title=title,
                    role=role,
                    observed_name=sanitize_org_name(raw_name),
                    outcome=outcome,
                    outcome_status=ostatus,
                )
            )

    try:
        wl = (
            sb.table("win_loss_memory")
            .select("opportunity_id, outcome, client, donor")
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.warning(f"win_loss_memory org scan failed (fail-open): {e}")
        wl = []
    for row in wl:
        if not isinstance(row, dict):
            continue
        oid = str(row.get("opportunity_id") or "")
        if not oid:
            continue
        outcome, ostatus = normalize_outcome(row.get("outcome"))
        for role, raw_name in (
            ("client", row.get("client") or ""),
            ("donor", row.get("donor") or ""),
        ):
            if not raw_name:
                continue
            hit = match_organization(raw_name, index)
            if hit.organization_id != match.organization_id or hit.is_new_candidate:
                continue
            out.append(
                ObservedRecord(
                    source_kind="win_loss",
                    source_id=oid,
                    title="",
                    role=role,
                    observed_name=sanitize_org_name(raw_name),
                    outcome=outcome,
                    outcome_status=ostatus,
                )
            )
    return out
