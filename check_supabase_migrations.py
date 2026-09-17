"""Compare expected Phase 1/2/3/5 objects against hosted Supabase.

Same idea as check_supabase_stages.py. Never prints credentials.
Uses DATABASE_URL (psql) for indexes/FKs and PostgREST for the API the
pipeline actually calls. Missing objects return names, never a fake OK.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from database.supabase_client import supabase

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True, interpolate=False)

MIGRATION_OBJECTS = (
    ("supabase_migration_content_hash.sql", "column:opportunities_cache.content_hash"),
    ("supabase_migration_content_hash.sql", "index:opportunities_cache_content_hash_uidx"),
    ("supabase_migration_organizations.sql", "table:organizations"),
    ("supabase_migration_organizations.sql", "table:organization_aliases"),
    ("supabase_migration_organizations.sql", "table:organization_observations"),
    ("supabase_migration_opportunity_facts.sql", "column:opportunities_cache.thematic_areas"),
    ("supabase_migration_opportunity_facts.sql", "column:opportunities_cache.locations"),
    ("supabase_migration_opportunity_facts.sql", "column:opportunities_cache.donor"),
    ("supabase_migration_opportunity_facts.sql", "column:opportunities_cache.discovered_at"),
    ("supabase_migration_award_relationships.sql", "table:award_observations"),
    ("supabase_migration_award_relationships.sql", "table:relationship_edges"),
    ("supabase_migration_award_relationships.sql", "fk:award_observations.organization_id"),
    ("supabase_migration_award_relationships.sql", "fk:relationship_edges.organization_id"),
)


def _clean(name: str) -> str | None:
    raw = os.getenv(name) or ""
    return raw.strip().strip('"').strip("'").rstrip("/").strip() or None


def _redact(text: str) -> str:
    secrets = [
        _clean("DATABASE_URL"),
        _clean("SUPABASE_SERVICE_KEY"),
        _clean("SUPABASE_ACCESS_TOKEN"),
        _clean("SUPABASE_DB_PASSWORD"),
    ]
    db_url = _clean("DATABASE_URL")
    if db_url:
        password = urlparse(db_url).password
        if password:
            secrets.append(password)
    out = text or ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out[:1200]


def _is_missing_postgrest(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("pgrst204", "pgrst205", "42703", "42p01", "schema cache", "does not exist")
    )


def _psql(sql: str) -> str:
    db_url = _clean("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL unset — cannot inspect indexes/FKs")
    env = os.environ.copy()
    env["PGCONNECT_TIMEOUT"] = "15"
    proc = subprocess.run(
        ["psql", db_url, "-v", "ON_ERROR_STOP=1", "-At", "-c", sql],
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(_redact(proc.stderr or proc.stdout or "psql failed"))
    return proc.stdout or ""


def check_postgrest_objects() -> list[tuple[str, str]]:
    """Return (migration, object) pairs missing from the PostgREST schema cache."""
    missing: list[tuple[str, str]] = []
    column_probes = [
        ("supabase_migration_content_hash.sql", "opportunities_cache", "content_hash"),
        ("supabase_migration_opportunity_facts.sql", "opportunities_cache", "thematic_areas"),
        ("supabase_migration_opportunity_facts.sql", "opportunities_cache", "locations"),
        ("supabase_migration_opportunity_facts.sql", "opportunities_cache", "donor"),
        ("supabase_migration_opportunity_facts.sql", "opportunities_cache", "discovered_at"),
    ]
    for migration, table, column in column_probes:
        try:
            supabase.table(table).select(column).limit(1).execute()
        except Exception as exc:
            if _is_missing_postgrest(exc):
                missing.append((migration, f"column:{table}.{column}"))
                continue
            raise
    table_probes = [
        ("supabase_migration_organizations.sql", "organizations"),
        ("supabase_migration_organizations.sql", "organization_aliases"),
        ("supabase_migration_organizations.sql", "organization_observations"),
        ("supabase_migration_award_relationships.sql", "award_observations"),
        ("supabase_migration_award_relationships.sql", "relationship_edges"),
    ]
    for migration, table in table_probes:
        try:
            supabase.table(table).select("id").limit(1).execute()
        except Exception as exc:
            if _is_missing_postgrest(exc):
                missing.append((migration, f"table:{table}"))
                continue
            raise
    return missing


def check_postgres_objects() -> list[tuple[str, str]]:
    """Return (migration, object) pairs missing from pg_catalog."""
    missing: list[tuple[str, str]] = []
    index = _psql(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname='public' AND indexname='opportunities_cache_content_hash_uidx';"
    ).strip()
    if index != "opportunities_cache_content_hash_uidx":
        missing.append(
            ("supabase_migration_content_hash.sql", "index:opportunities_cache_content_hash_uidx")
        )
    fks = _psql(
        "SELECT conrelid::regclass::text || '.' || a.attname "
        "FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey) "
        "WHERE c.contype='f' AND confrelid='organizations'::regclass "
        "AND conrelid IN ('award_observations'::regclass,'relationship_edges'::regclass);"
    )
    fk_set = {line.strip() for line in fks.splitlines() if line.strip()}
    if "award_observations.organization_id" not in fk_set:
        missing.append(
            ("supabase_migration_award_relationships.sql", "fk:award_observations.organization_id")
        )
    if "relationship_edges.organization_id" not in fk_set:
        missing.append(
            ("supabase_migration_award_relationships.sql", "fk:relationship_edges.organization_id")
        )
    return missing


def main() -> int:
    print("=" * 60)
    print("  hosted migration objects (psql + PostgREST)")
    print("=" * 60)
    missing: list[tuple[str, str]] = []
    missing.extend(check_postgrest_objects())
    try:
        missing.extend(check_postgres_objects())
    except RuntimeError as exc:
        print(f"  PSQL_FAIL {exc}")
        return 1
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for item in missing:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    if not unique:
        print(f"  All {len(MIGRATION_OBJECTS)} expected objects exist:")
        for migration, obj in MIGRATION_OBJECTS:
            print(f"     - {obj}  ({migration})")
        return 0
    print(f"  MISSING ({len(unique)}):")
    for migration, obj in unique:
        print(f"     - {obj}  apply {migration}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
