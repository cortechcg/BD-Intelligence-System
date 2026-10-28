"""Hosted ``dashboard_triggers`` queue behaviour.

Talks to the real Supabase project from .env, like the other
``test_live_supabase_*`` files, and is excluded from CI for the same reason.
It never prints keys, only ever touches rows whose ``source_url`` is under a
reserved ``https://dashboard-queue.test.invalid/`` prefix, and deletes them in
a finally block — it cannot disturb a real opportunity.

Scopes (supabase_migration_trigger_scopes.sql, 2026-10-28). Before them,
``claim()`` took the oldest queued row with no filter, so the production
workers (two Render services) ran these probe rows: 9 failed ledger rows for
``dashboard-queue.test.invalid`` between 10-18 and 10-25. Now every probe row
is stored as scope ``test`` by the database itself (reserved host), the test
claims with scope ``test``, and no deployed worker consumes that scope. A
probe can no longer reach production, and a test can no longer be handed a
real job.
"""
from __future__ import annotations

import uuid

import pytest

import config
from dashboard import triggers
from database.supabase_client import get_supabase

pytestmark = pytest.mark.skipif(
    not (config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY),
    reason="hosted SUPABASE_URL / SUPABASE_SERVICE_KEY not configured",
)

# Reserved, non-resolvable host. Nothing real can collide with this.
PREFIX = "https://dashboard-queue.test.invalid/"


SCOPE = triggers.TEST


@pytest.fixture
def probe_url():
    url = f"{PREFIX}{uuid.uuid4()}"
    try:
        yield url
    finally:
        sb = get_supabase()
        # The trigger row the test made. The ledger/cache deletes are kept as
        # a guard; with scopes no worker can have created them.
        for table in ("dashboard_triggers", "opportunity_processing", "opportunities_cache"):
            try:
                sb.table(table).delete().eq("source_url", url).execute()
            except Exception:
                pass


def _claim_probe(url: str, token: str, lease_seconds: int = 120) -> dict:
    """Claim from the test scope. Only probe rows live there."""
    job = triggers.claim(token, lease_seconds, SCOPE)
    assert job is not None, "the probe row should be waiting in the test scope"
    assert job["source_url"] == url, (
        f"claim() in the test scope returned {job['source_url'][:60]}; "
        "an older probe row was left queued"
    )
    assert job["scope"] == SCOPE
    return job


def test_probe_rows_are_stored_in_the_test_scope_whatever_was_asked(probe_url):
    tid = triggers.enqueue(probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com",
                           scope=triggers.PRODUCTION)
    row = get_supabase().table("dashboard_triggers").select("scope").eq("id", tid).execute().data
    assert row and row[0]["scope"] == "test"


def test_production_claim_never_sees_a_probe_row(probe_url):
    triggers.enqueue(probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com")
    rows = get_supabase().table("dashboard_triggers").select("id,scope").eq(
        "source_url", probe_url).execute().data
    assert rows and rows[0]["scope"] == "test"
    # Postgres, not Python, refuses a production row for a reserved host.
    with pytest.raises(Exception) as info:
        get_supabase().table("dashboard_triggers").insert({
            "source_url": probe_url + "-direct", "trigger_kind": "submit_url",
            "requested_by": "ci", "scope": "production",
        }).execute()
    assert "production_not_test_url" in str(info.value) or "23514" in str(info.value)


def test_queue_table_exists():
    assert triggers.queue_available() is True, (
        "apply supabase_migration_dashboard_triggers.sql"
    )


def test_enqueue_claim_heartbeat_finish_round_trip(probe_url):
    trigger_id = triggers.enqueue(
        probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com", scope=SCOPE
    )
    assert trigger_id

    token = f"test-{uuid.uuid4()}"
    job = _claim_probe(probe_url, token)
    assert job is not None
    assert job["source_url"] == probe_url
    assert job["trigger_kind"] == triggers.SUBMIT_URL
    assert job["attempt_count"] == 1

    alive, cancel_requested = triggers.heartbeat(
        str(job["id"]), token, 120,
        spend={"spent_usd": 0.1234, "limit_usd": 25.0, "provider_calls": 2, "cost_known": True},
    )
    assert alive is True and cancel_requested is False

    assert triggers.finish(
        str(job["id"]), token, status="succeeded", result={"probe": True},
        spend={"spent_usd": 0.5, "limit_usd": 25.0, "provider_calls": 4, "cost_known": True},
    ) is True
    row = triggers.get(str(job["id"]))
    assert row["spend_usd"] == pytest.approx(0.5)
    assert row["spend_limit_usd"] == 25.0
    assert row["provider_calls"] == 4


def test_a_second_request_for_an_inflight_url_is_refused(probe_url):
    first = triggers.enqueue(probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com")
    assert first
    second = triggers.enqueue(probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com")
    assert second is None, "the partial unique index must reject a duplicate in-flight run"


def test_a_stale_worker_cannot_finalise_someone_elses_run(probe_url):
    triggers.enqueue(probe_url, triggers.DRAFT_EXISTING, "ci@cortechconsultinggroup.com")
    token = f"test-{uuid.uuid4()}"
    job = _claim_probe(probe_url, token)
    assert job is not None

    assert triggers.finish(str(job["id"]), "some-other-token", status="succeeded") is False
    assert triggers.finish(str(job["id"]), token, status="failed",
                           error_kind="SPEND_CAP_ERROR",
                           error_message="per-run LLM spend cap reached") is True


def test_finished_rows_surface_their_failure_kind(probe_url):
    triggers.enqueue(probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com")
    token = f"test-{uuid.uuid4()}"
    job = _claim_probe(probe_url, token)
    triggers.finish(
        str(job["id"]), token, status="failed",
        error_kind="DEAD_LETTER", error_message="drafting dead-lettered",
    )
    row = triggers.get(str(job["id"]))
    assert row["ui_state"] == "dead_letter"
    assert row["error_kind"] == "DEAD_LETTER"
    # The dashboard lists production requests only; a probe never shows up.
    assert probe_url not in {r["source_url"] for r in triggers.recent(50)}


def test_in_flight_urls_lists_production_rows_only(probe_url):
    triggers.enqueue(probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com")
    queued = get_supabase().table("dashboard_triggers").select("status").eq(
        "source_url", probe_url).execute().data
    assert queued and queued[0]["status"] == "queued"
    assert probe_url not in triggers.in_flight_urls()


def test_enqueue_rejects_an_unknown_kind(probe_url):
    with pytest.raises(ValueError):
        triggers.enqueue(probe_url, "ship_it_to_the_client", "ci@cortechconsultinggroup.com")


def test_cancel_before_start_and_halt_request_while_running(probe_url):
    tid = triggers.enqueue(probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com")
    assert triggers.cancel(tid, "ci@cortechconsultinggroup.com") == "cancelled_before_start"
    row = triggers.get(tid)
    assert row["status"] == "cancelled" and row["ui_state"] == "cancelled"
    assert row["cancelled_by"] == "ci@cortechconsultinggroup.com"

    # A new request for the same URL is allowed once the old one is cancelled.
    tid2 = triggers.enqueue(probe_url, triggers.DRAFT_EXISTING, "ci@cortechconsultinggroup.com")
    assert tid2 and tid2 != tid
    token = f"test-{uuid.uuid4()}"
    job = _claim_probe(probe_url, token)
    assert str(job["id"]) == tid2
    assert triggers.cancel(tid2, "ci@cortechconsultinggroup.com") == "halt_requested"
    alive, cancel_requested = triggers.heartbeat(tid2, token, 120)
    assert alive is True and cancel_requested is True
    assert triggers.finish(tid2, token, status="cancelled", error_kind="CANCELLED",
                           error_message="halted", spend={"spent_usd": 0.2}) is True
    assert triggers.cancel(tid2, "x") == "already_cancelled"


def test_spend_window_counts_cancelled_runs(probe_url):
    before = triggers.spend_window(24)
    tid = triggers.enqueue(probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com")
    token = f"test-{uuid.uuid4()}"
    _claim_probe(probe_url, token)
    triggers.finish(tid, token, status="cancelled", error_kind="CANCELLED",
                    error_message="halted", spend={"spent_usd": 1.25, "cost_known": True})
    after = triggers.spend_window(24)
    assert after["spent_usd"] == pytest.approx(before["spent_usd"] + 1.25)
    assert after["runs"] == before["runs"] + 1
