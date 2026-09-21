"""Hosted ``dashboard_triggers`` queue behaviour.

Talks to the real Supabase project from .env, like the other
``test_live_supabase_*`` files, and is excluded from CI for the same reason.
It never prints keys, only ever touches rows whose ``source_url`` is under a
reserved ``https://dashboard-queue.test.invalid/`` prefix, and deletes them in
a finally block — it cannot disturb a real opportunity.
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


@pytest.fixture
def probe_url():
    url = f"{PREFIX}{uuid.uuid4()}"
    try:
        yield url
    finally:
        try:
            get_supabase().table("dashboard_triggers").delete().eq(
                "source_url", url
            ).execute()
        except Exception:
            pass


def test_queue_table_exists():
    assert triggers.queue_available() is True, (
        "apply supabase_migration_dashboard_triggers.sql"
    )


def test_enqueue_claim_heartbeat_finish_round_trip(probe_url):
    trigger_id = triggers.enqueue(
        probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com"
    )
    assert trigger_id

    token = f"test-{uuid.uuid4()}"
    job = triggers.claim(token, lease_seconds=120)
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
    job = triggers.claim(token, lease_seconds=120)
    assert job is not None

    assert triggers.finish(str(job["id"]), "some-other-token", status="succeeded") is False
    assert triggers.finish(str(job["id"]), token, status="failed",
                           error_kind="SPEND_CAP_ERROR",
                           error_message="per-run LLM spend cap reached") is True


def test_finished_rows_surface_their_failure_kind(probe_url):
    triggers.enqueue(probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com")
    token = f"test-{uuid.uuid4()}"
    job = triggers.claim(token, lease_seconds=120)
    triggers.finish(
        str(job["id"]), token, status="failed",
        error_kind="DEAD_LETTER", error_message="drafting dead-lettered",
    )
    rows = [r for r in triggers.recent(50) if r["source_url"] == probe_url]
    assert rows, "the finished trigger should be listed"
    assert rows[0]["ui_state"] == "dead_letter"
    assert rows[0]["error_kind"] == "DEAD_LETTER"


def test_in_flight_urls_reflects_queued_rows(probe_url):
    triggers.enqueue(probe_url, triggers.SUBMIT_URL, "ci@cortechconsultinggroup.com")
    assert probe_url in triggers.in_flight_urls()


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
    job = triggers.claim(token, lease_seconds=120)
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
    triggers.claim(token, lease_seconds=120)
    triggers.finish(tid, token, status="cancelled", error_kind="CANCELLED",
                    error_message="halted", spend={"spent_usd": 1.25, "cost_known": True})
    after = triggers.spend_window(24)
    assert after["spent_usd"] == pytest.approx(before["spent_usd"] + 1.25)
    assert after["runs"] == before["runs"] + 1
