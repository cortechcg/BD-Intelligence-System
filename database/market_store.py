"""Fail-open loaders for the observed-data market digest (Phase 3).

Prefer Supabase `opportunities_cache`. Airtable OPPORTUNITIES is secondary
and uses only fields already in check_schema.py. Missing tables/columns
return empty lists — never crash, never invent rows.
"""

from __future__ import annotations

from loguru import logger

from intelligence.market_trends import (
    ObservedOpportunity,
    extract_labels,
    max_store_rows,
    parse_observed_date,
)
from utils.urls import canonicalize_url

_CACHE_FULL = (
    "id, source_url, title, thematic_areas, locations, donor, "
    "discovered_at, created_at"
)
_CACHE_WITH_CREATED = "id, source_url, title, created_at"
_CACHE_MIN = "id, source_url, title"

_MISSING_FACTS = (
    "opportunities_cache observed-fact columns missing — apply "
    "supabase_migration_opportunity_facts.sql. Market digest fail-opens "
    "to timestamps/title only, then Airtable secondary fields."
)

_facts_columns_available: bool | None = None


def reset_market_store_status() -> None:
    """Test helper."""
    global _facts_columns_available
    _facts_columns_available = None


def _is_missing_relation_or_column(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "pgrst205" in msg
        or "pgrst204" in msg
        or "pgrst203" in msg
        or "42p01" in msg
        or "42703" in msg
        or "schema cache" in msg
        or "does not exist" in msg
        or "could not find" in msg
        or "unknown column" in msg
    )


def _client():
    from database.supabase_client import get_supabase

    return get_supabase()


def _row_identity(url: str, source_id: str, title: str) -> str:
    canon = canonicalize_url(url) or (url or "").strip()
    if canon:
        return f"url:{canon}"
    if source_id:
        return f"id:{source_id}"
    return f"title:{(title or '').strip().casefold()[:120]}"


def _from_cache_row(row: dict) -> ObservedOpportunity | None:
    if not isinstance(row, dict):
        return None
    source_id = str(row.get("id") or "")
    url = str(row.get("source_url") or "")
    title = str(row.get("title") or "")
    discovered = parse_observed_date(row.get("discovered_at")) or parse_observed_date(
        row.get("created_at")
    )
    themes, theme_bad = extract_labels(row.get("thematic_areas"))
    locations, loc_bad = extract_labels(row.get("locations") or row.get("location"))
    donor = ""
    raw_donor = row.get("donor")
    if isinstance(raw_donor, str):
        donor = " ".join(raw_donor.split())[:200]
    elif raw_donor not in (None,):
        # Non-string donor is unusable; do not invent a name.
        donor = ""
    return ObservedOpportunity(
        source_id=source_id or url or title,
        source="supabase",
        source_url=canonicalize_url(url) or url,
        title=title,
        discovered_on=discovered,
        themes=themes,
        locations=locations,
        donor=donor,
        malformed_theme_skips=theme_bad,
        malformed_location_skips=loc_bad,
    )


def _from_airtable_record(record: dict) -> ObservedOpportunity | None:
    if not isinstance(record, dict):
        return None
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else record
    if not isinstance(fields, dict):
        return None
    source_id = str(record.get("id") or fields.get("opportunity_id") or "")
    url = str(fields.get("source_url") or "")
    title = str(fields.get("title") or "")
    discovered = parse_observed_date(fields.get("discovered_at"))
    themes, theme_bad = extract_labels(fields.get("thematic_areas"))
    locations, loc_bad = extract_labels(fields.get("location") or fields.get("locations"))
    donor = ""
    raw_donor = fields.get("donor")
    if isinstance(raw_donor, str):
        donor = " ".join(raw_donor.split())[:200]
    elif isinstance(raw_donor, list) and raw_donor:
        # Airtable multi-select of one donor: take string items only.
        parts = [str(x).strip() for x in raw_donor if isinstance(x, str) and str(x).strip()]
        donor = ", ".join(parts)[:200]
    return ObservedOpportunity(
        source_id=source_id or url or title,
        source="airtable",
        source_url=canonicalize_url(url) or url,
        title=title,
        discovered_on=discovered,
        themes=themes,
        locations=locations,
        donor=donor,
        malformed_theme_skips=theme_bad,
        malformed_location_skips=loc_bad,
    )


def _merge(primary: ObservedOpportunity, secondary: ObservedOpportunity) -> ObservedOpportunity:
    """Fill empty structured fields from the secondary row; keep a stored date."""
    discovered = primary.discovered_on or secondary.discovered_on
    themes = primary.themes or secondary.themes
    locations = primary.locations or secondary.locations
    donor = primary.donor or secondary.donor
    title = primary.title or secondary.title
    url = primary.source_url or secondary.source_url
    return ObservedOpportunity(
        source_id=primary.source_id or secondary.source_id,
        source="merged" if primary.source != secondary.source else primary.source,
        source_url=url,
        title=title,
        discovered_on=discovered,
        themes=themes,
        locations=locations,
        donor=donor,
        malformed_theme_skips=primary.malformed_theme_skips + secondary.malformed_theme_skips,
        malformed_location_skips=primary.malformed_location_skips
        + secondary.malformed_location_skips,
    )


def load_supabase_opportunity_rows(limit: int | None = None) -> tuple[list[ObservedOpportunity], bool]:
    """Load cache rows. Missing table/columns → ([], False truncated)."""
    global _facts_columns_available
    cap = limit if limit is not None else max_store_rows()
    try:
        sb = _client()
    except Exception as e:
        logger.warning(f"opportunities_cache load skipped (fail-open): {e}")
        return [], False

    selects = []
    if _facts_columns_available is False:
        selects = [_CACHE_WITH_CREATED, _CACHE_MIN]
    else:
        selects = [_CACHE_FULL, _CACHE_WITH_CREATED, _CACHE_MIN]

    last_exc = None
    data = None
    used = None
    for cols in selects:
        try:
            result = (
                sb.table("opportunities_cache")
                .select(cols)
                .limit(cap + 1)
                .execute()
            )
            data = result.data or []
            used = cols
            if cols == _CACHE_FULL:
                _facts_columns_available = True
            break
        except Exception as e:
            last_exc = e
            if _is_missing_relation_or_column(e):
                if cols == _CACHE_FULL:
                    _facts_columns_available = False
                    logger.warning(f"{_MISSING_FACTS} Cause: {e}")
                continue
            logger.warning(f"opportunities_cache load failed (fail-open): {e}")
            return [], False
    if data is None:
        if last_exc is not None:
            logger.warning(f"opportunities_cache unavailable (fail-open): {last_exc}")
        return [], False

    truncated = len(data) > cap
    rows = []
    for raw in data[:cap]:
        parsed = _from_cache_row(raw)
        if parsed is not None:
            rows.append(parsed)
    logger.info(
        f"Market digest loaded {len(rows)} opportunities_cache rows "
        f"(select={used}, truncated={truncated})"
    )
    return rows, truncated


def load_airtable_opportunity_rows(limit: int | None = None) -> list[ObservedOpportunity]:
    """Secondary source. Existing OPPORTUNITIES fields only. Fail-open to []."""
    cap = limit if limit is not None else max_store_rows()
    try:
        from database.airtable_client import get_table

        table = get_table("opportunities")
        records = table.all()
    except Exception as e:
        logger.warning(f"Airtable OPPORTUNITIES load skipped (fail-open): {e}")
        return []
    out: list[ObservedOpportunity] = []
    for record in records or []:
        parsed = _from_airtable_record(record)
        if parsed is None:
            continue
        out.append(parsed)
        if len(out) >= cap:
            break
    return out


def load_observed_opportunities(
    *,
    include_airtable: bool = True,
    limit: int | None = None,
) -> tuple[list[ObservedOpportunity], bool]:
    """Supabase first, Airtable fills gaps / adds CRM-only rows. Dedup by URL."""
    cap = limit if limit is not None else max_store_rows()
    cache_rows, truncated = load_supabase_opportunity_rows(limit=cap)
    by_key: dict[str, ObservedOpportunity] = {}
    order: list[str] = []
    for row in cache_rows:
        key = _row_identity(row.source_url, row.source_id, row.title)
        if key not in by_key:
            order.append(key)
            by_key[key] = row
        else:
            by_key[key] = _merge(by_key[key], row)

    if include_airtable:
        for row in load_airtable_opportunity_rows(limit=cap):
            key = _row_identity(row.source_url, row.source_id, row.title)
            if key in by_key:
                by_key[key] = _merge(by_key[key], row)
            else:
                order.append(key)
                by_key[key] = row

    merged = [by_key[k] for k in order]
    if len(merged) > cap:
        return merged[:cap], True
    return merged, truncated


def load_org_index_for_digest() -> tuple[list, bool, int]:
    """Return (index entries, available, distinct org count). Fail-open."""
    try:
        from database.organizations import load_organization_index, organizations_available

        if not organizations_available():
            return [], False, 0
        entries, label = load_organization_index()
        if label == "missing":
            return [], False, 0
        org_ids = {e.organization_id for e in entries if e.organization_id}
        return list(entries), True, len(org_ids)
    except Exception as e:
        logger.warning(f"organizations index for digest failed (fail-open): {e}")
        return [], False, 0
