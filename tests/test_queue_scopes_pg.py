"""dashboard_triggers scopes and atomic claims, against a real Postgres.

Proves, with concurrent connections and the shipped SQL (not a mock):

* every queued row is claimed exactly once while many workers poll at once;
* a worker only ever receives rows from its own scope;
* a reserved test host (https://dashboard-queue.test.invalid/...) is stored
  as scope 'test' even when enqueued as production, cannot be inserted as
  production at all, and is never handed to a production or staging worker;
* the old two-argument claim (what an un-upgraded worker calls) no longer
  exists, so it takes nothing;
* an expired lease is reclaimed only within its scope, and the stale worker
  can no longer finish the row.

Runs when QUEUE_TEST_DATABASE_URL points at a disposable Postgres (CI starts
one; locally: ``docker run -d -p 55432:5432 -e POSTGRES_PASSWORD=pw
postgres:17`` and ``QUEUE_TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres``).
Everything lives in a throwaway schema that is dropped afterwards. It refuses
to run against a Supabase host.
"""
from __future__ import annotations

import os
import threading
import uuid
from collections import Counter
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

ROOT = Path(__file__).resolve().parents[1]
DSN = os.getenv("QUEUE_TEST_DATABASE_URL", "")
MIGRATIONS = (
    "supabase_migration_opportunity_state.sql",
    "supabase_migration_opportunity_stages.sql",
    "supabase_migration_dashboard_triggers.sql",
    "supabase_migration_dashboard_controls.sql",
    "supabase_migration_trigger_scopes.sql",
)

pytestmark = pytest.mark.skipif(not DSN, reason="QUEUE_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def schema():
    if "supabase" in DSN:
        pytest.fail("QUEUE_TEST_DATABASE_URL must be a disposable Postgres, not Supabase")
    name = f"queue_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(f"SET search_path TO {name}, public")
        for migration in MIGRATIONS:
            conn.execute((ROOT / migration).read_text())
        # Re-running the scope migration must be a no-op (idempotent).
        conn.execute((ROOT / "supabase_migration_trigger_scopes.sql").read_text())
    try:
        yield name
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {name} CASCADE")


def _connect(schema: str):
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute(f"SET search_path TO {schema}, public")
    return conn


def _enqueue(conn, url: str, scope: str = "production", kind: str = "submit_url"):
    return conn.execute(
        "SELECT enqueue_dashboard_trigger(%s, %s, %s, %s)",
        (url, kind, "ci@example.org", scope),
    ).fetchone()[0]


def _claim(conn, token: str, scope: str, lease: int = 600):
    return conn.execute(
        "SELECT id, source_url, scope FROM claim_dashboard_trigger(%s, %s, %s)",
        (token, lease, scope),
    ).fetchone()


def test_concurrent_workers_claim_each_row_once_and_only_in_their_scope(schema):
    with _connect(schema) as conn:
        conn.execute("TRUNCATE dashboard_triggers")
        prod = {str(_enqueue(conn, f"https://tenders.example.org.za/p/{i}")) for i in range(120)}
        staging = {str(_enqueue(conn, f"https://tenders.example.org.za/s/{i}", "staging")) for i in range(40)}
        # Requested as production; the database must file them under test.
        probes = {str(_enqueue(conn, f"https://dashboard-queue.test.invalid/{uuid.uuid4()}")) for _ in range(25)}
        stored = dict(conn.execute(
            "SELECT scope, count(*) FROM dashboard_triggers GROUP BY scope"
        ).fetchall())
    assert stored == {"production": 120, "staging": 40, "test": 25}

    claims: list[tuple[str, str, str]] = []  # (worker scope, row id, row scope)
    lock = threading.Lock()
    start = threading.Barrier(12)

    def worker(scope: str) -> None:
        token = f"{scope}-{uuid.uuid4()}"
        with _connect(schema) as conn:
            start.wait()
            while True:
                row = _claim(conn, token, scope)
                if row is None:
                    return
                with lock:
                    claims.append((scope, str(row[0]), row[2]))

    threads = [threading.Thread(target=worker, args=("production",)) for _ in range(8)]
    threads += [threading.Thread(target=worker, args=("staging",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)

    counts = Counter(row_id for _scope, row_id, _row_scope in claims)
    assert all(n == 1 for n in counts.values()), "a row was claimed twice"
    by_scope = {s: {rid for ws, rid, _ in claims if ws == s} for s in ("production", "staging")}
    assert by_scope["production"] == prod
    assert by_scope["staging"] == staging
    assert all(ws == rs for ws, _rid, rs in claims)
    assert not (probes & set(counts)), "a test-host row was claimed outside the test scope"

    with _connect(schema) as conn:
        left = conn.execute(
            "SELECT scope, status, count(*) FROM dashboard_triggers "
            "WHERE status = 'queued' GROUP BY 1, 2"
        ).fetchall()
    assert left == [("test", "queued", 25)]


def test_a_production_row_for_a_test_host_is_impossible(schema):
    with _connect(schema) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO dashboard_triggers (source_url, trigger_kind, requested_by, scope) "
                "VALUES ('https://dashboard-queue.test.invalid/x', 'submit_url', 'ci', 'production')"
            )
        for host in ("https://a.test/x", "https://example/x", "http://localhost:8000/x",
                     "https://foo.EXAMPLE./x", "https://u@bar.invalid:443/x"):
            assert conn.execute("SELECT dashboard_trigger_is_test_url(%s)", (host,)).fetchone()[0], host
        for host in ("https://upload.cortechconsultinggroup.com/tor/abc",
                     "https://example.org/x", "https://testing.org/x", "library://doc/1"):
            assert not conn.execute("SELECT dashboard_trigger_is_test_url(%s)", (host,)).fetchone()[0], host


def test_an_unupgraded_worker_gets_nothing(schema):
    with _connect(schema) as conn:
        conn.execute("TRUNCATE dashboard_triggers")
        _enqueue(conn, "https://tenders.example.org.za/legacy")
        with pytest.raises(psycopg.errors.UndefinedFunction):
            conn.execute("SELECT * FROM claim_dashboard_trigger(%s, %s)", ("old-worker", 600))
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("SELECT * FROM claim_dashboard_trigger(%s, %s, %s)", ("w", 600, "everything"))
        assert conn.execute(
            "SELECT count(*) FROM dashboard_triggers WHERE status = 'queued'"
        ).fetchone()[0] == 1


def test_expired_lease_is_reclaimed_in_scope_and_stale_worker_cannot_finish(schema):
    with _connect(schema) as conn:
        conn.execute("TRUNCATE dashboard_triggers")
        _enqueue(conn, "https://tenders.example.org.za/lease")
        first = _claim(conn, "worker-a", "production")
        assert first is not None
        conn.execute(
            "UPDATE dashboard_triggers SET lease_until = NOW() - interval '1 second' WHERE id = %s",
            (first[0],),
        )
        assert _claim(conn, "worker-s", "staging") is None
        second = _claim(conn, "worker-b", "production")
        assert second is not None and second[0] == first[0]
        stale = conn.execute(
            "SELECT finish_dashboard_trigger(%s, %s, 'succeeded')", (first[0], "worker-a")
        ).fetchone()[0]
        owner = conn.execute(
            "SELECT finish_dashboard_trigger(%s, %s, 'succeeded')", (first[0], "worker-b")
        ).fetchone()[0]
        assert (stale, owner) == (False, True)


def test_worker_registry_records_scope_and_idle(schema):
    with _connect(schema) as conn:
        conn.execute(
            "SELECT register_dashboard_worker(%s, %s, %s, %s)",
            ("tok-1", "production", "cortech-bd-worker", "srv-1"),
        )
        conn.execute("SELECT register_dashboard_worker(%s, %s, %s)", ("tok-2", "", "BD-Intelligence-System"))
        rows = dict(conn.execute(
            "SELECT service_name, scope FROM dashboard_workers ORDER BY service_name"
        ).fetchall())
    assert rows == {"BD-Intelligence-System": None, "cortech-bd-worker": "production"}
