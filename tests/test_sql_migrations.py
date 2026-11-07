"""Offline checks that the four remaining migrations stay additive and ordered."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = ("DROP TABLE", "DELETE FROM", "TRUNCATE", "UPDATE ")


def _sql(name: str) -> str:
    return (ROOT / name).read_text()


def test_content_hash_migration_is_additive_on_opportunities_cache():
    sql = _sql("supabase_migration_content_hash.sql")
    assert "ALTER TABLE opportunities_cache" in sql
    assert "ADD COLUMN IF NOT EXISTS content_hash TEXT" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS opportunities_cache_content_hash_uidx" in sql
    assert "WHERE content_hash IS NOT NULL" in sql
    for token in FORBIDDEN:
        assert token not in sql
    assert "organizations" not in sql.lower()
    assert "award_observations" not in sql


def test_organizations_migration_is_additive_create_if_not_exists():
    sql = _sql("supabase_migration_organizations.sql")
    assert "CREATE TABLE IF NOT EXISTS organizations" in sql
    assert "CREATE TABLE IF NOT EXISTS organization_aliases" in sql
    assert "CREATE TABLE IF NOT EXISTS organization_observations" in sql
    assert "REFERENCES organizations(id) ON DELETE CASCADE" in sql
    for token in FORBIDDEN:
        assert token not in sql


def test_opportunity_facts_migration_is_additive_on_opportunities_cache():
    sql = _sql("supabase_migration_opportunity_facts.sql")
    assert "ADD COLUMN IF NOT EXISTS thematic_areas TEXT[]" in sql
    assert "ADD COLUMN IF NOT EXISTS locations TEXT[]" in sql
    assert "ADD COLUMN IF NOT EXISTS donor TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS discovered_at DATE" in sql
    for token in FORBIDDEN:
        assert token not in sql
    assert "CREATE TABLE" not in sql


def test_knowledge_library_migration_is_additive():
    sql = _sql("supabase_migration_knowledge_library.sql")
    assert "CREATE TABLE IF NOT EXISTS library_documents" in sql
    assert "CREATE TABLE IF NOT EXISTS library_events" in sql
    assert "library_documents_live_hash_uidx" in sql
    assert "ADD COLUMN IF NOT EXISTS email" in sql
    assert "ADD COLUMN IF NOT EXISTS created_by_email" in sql
    assert "'library_ingest'" in sql
    executable = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    for token in ("DROP TABLE", "DELETE FROM", "TRUNCATE", "UPDATE "):
        assert token not in executable
    assert "DROP CONSTRAINT IF EXISTS dashboard_triggers_trigger_kind_check" in executable


def test_award_relationships_migration_requires_organizations_fk():
    sql = _sql("supabase_migration_award_relationships.sql")
    orgs = _sql("supabase_migration_organizations.sql")
    assert "CREATE TABLE IF NOT EXISTS organizations" in orgs
    assert "REFERENCES organizations(id) ON DELETE SET NULL" in sql
    assert sql.count("REFERENCES organizations(id)") == 2
    assert "CREATE TABLE IF NOT EXISTS award_observations" in sql
    assert "CREATE TABLE IF NOT EXISTS relationship_edges" in sql
    for token in FORBIDDEN:
        assert token not in sql
    assert "AFTER" in sql or "organizations.sql" in sql


def test_procurement_sources_migration_is_durable_private_and_lease_safe():
    sql = _sql("supabase_migration_procurement_sources.sql")
    lower = sql.lower()
    assert "create table if not exists public.procurement_sources" in lower
    assert "url text not null unique" in sql
    assert "last_attempt_at timestamptz" in sql
    assert "last_success_at timestamptz" in sql
    assert "discovered_count bigint" in sql
    assert "enable row level security" in lower
    assert "claim_procurement_source_run" in sql
    assert "run_token = p_token" in sql
    assert "60 seconds" in sql
    assert "5 minutes" in sql and "24 hours" in sql
    assert "pg_advisory_xact_lock" in lower and "active_count >= 4" in lower
    assert "source_metadata jsonb" in sql
    assert "grant execute" in lower and "service_role" in lower
