"""Hosted checks for the four applied migrations.

Talks to the real project from .env. Never prints keys.
Skip reasons name the migration file — no unexplained skips.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest

import config
from check_supabase_migrations import check_postgres_objects, check_postgrest_objects
from database import organizations as org_store
from database.supabase_client import (
    reset_opportunity_facts_status,
    store_opportunity,
    supabase,
)
from intelligence.organizations import build_client_intelligence
from utils.hashing import content_hash


pytestmark = pytest.mark.skipif(
    not (config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY),
    reason="hosted SUPABASE_URL / SUPABASE_SERVICE_KEY not configured",
)


def test_hosted_postgrest_sees_all_four_migrations():
    missing = check_postgrest_objects()
    assert missing == [], (
        "PostgREST schema cache is missing "
        + ", ".join(f"{obj} ({mig})" for mig, obj in missing)
    )


def test_hosted_postgres_indexes_and_award_fks():
    try:
        missing = check_postgres_objects()
    except RuntimeError as exc:
        pytest.skip(f"DATABASE_URL psql unavailable: {exc}")
    assert missing == [], (
        "Postgres catalog is missing "
        + ", ".join(f"{obj} ({mig})" for mig, obj in missing)
    )


def test_hosted_store_opportunity_writes_hash_and_facts():
    reset_opportunity_facts_status()
    suffix = uuid.uuid4()
    source_url = f"https://migration-probe.example/cortech-facts-{suffix}"
    body = f"Probe tender body for content_hash {suffix}."
    digest = content_hash(body)
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        row_id = store_opportunity(
            source_url,
            "Migration probe facts",
            body,
            facts={
                "thematic_areas": ["MEL"],
                "locations": ["Kenya"],
                "donor": "Probe Donor Agency",
                "discovered_at": today,
            },
        )
        assert row_id, "INSUFFICIENT DATA: store_opportunity returned no id"
        result = (
            supabase.table("opportunities_cache")
            .select("id, source_url, content_hash, thematic_areas, locations, donor, discovered_at")
            .eq("id", row_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        assert rows, "INSUFFICIENT DATA: no opportunities_cache row after store"
        row = rows[0]
        assert row["content_hash"] == digest
        assert row["thematic_areas"] == ["MEL"]
        assert row["locations"] == ["Kenya"]
        assert row["donor"] == "Probe Donor Agency"
        discovered = str(row.get("discovered_at") or "")
        assert discovered.startswith(today), discovered
    finally:
        supabase.table("opportunities_cache").delete().eq("source_url", source_url).execute()


def test_hosted_build_client_intelligence_persists_organization_row():
    org_store.reset_organizations_status()
    suffix = uuid.uuid4()
    client = f"Probe Client {suffix} Ltd"
    donor = f"Probe Donor {suffix}"
    source_id = f"https://migration-probe.example/cortech-org-{suffix}"
    created_ids: list[str] = []
    try:
        payload = build_client_intelligence(
            client=client,
            donor=donor,
            title="Migration probe organization persist",
            opportunity_id=source_id,
            persist=True,
            fetch_stored=True,
        )
        client_id = ((payload.get("client") or {}).get("match") or {}).get("organization_id")
        donor_id = ((payload.get("donor") or {}).get("match") or {}).get("organization_id")
        assert client_id, payload
        created_ids.append(str(client_id))
        if donor_id:
            created_ids.append(str(donor_id))
        org_rows = (
            supabase.table("organizations")
            .select("id, canonical_name, entity_kind, normalized_name")
            .eq("id", client_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        assert org_rows, "INSUFFICIENT DATA: organizations insert did not land"
        assert client in (org_rows[0].get("canonical_name") or "")
        obs = (
            supabase.table("organization_observations")
            .select("organization_id, role, source_id, observed_name, outcome")
            .eq("source_id", source_id)
            .execute()
            .data
            or []
        )
        assert obs, "INSUFFICIENT DATA: organization_observations insert did not land"
        assert obs[0]["outcome"] == "UNKNOWN"
    finally:
        if created_ids:
            supabase.table("organizations").delete().in_("id", created_ids).execute()
