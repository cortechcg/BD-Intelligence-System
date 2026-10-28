-- Dashboard trigger scopes: each worker consumes exactly one scope.
--
-- Apply AFTER supabase_migration_dashboard_triggers.sql,
-- supabase_migration_dashboard_controls.sql and
-- supabase_migration_knowledge_library.sql. Idempotent: safe to re-run.
--
-- Why (2026-10-28): two Render services (cortech-bd-worker and the
-- hand-made BD-Intelligence-System in Oregon) both called
-- claim_dashboard_trigger() with no filter, so every request went to
-- whichever polled first, and the live queue tests' probe rows under
-- https://dashboard-queue.test.invalid/ were run by production workers
-- (9 failed ledger rows between 10-18 and 10-25).
--
-- Rules enforced here, in the database, not only in Python:
--   1. Every trigger has a scope: production | staging | test.
--   2. A URL on a reserved test host (RFC 2606/6761: .invalid, .test,
--      .example, .localhost) is ALWAYS scope 'test'. enqueue forces it and a
--      CHECK constraint makes a production test-URL row impossible.
--   3. claim_dashboard_trigger() requires p_scope (no default). An old
--      worker that still calls the two-argument form gets PGRST202 and takes
--      nothing. A production claim also refuses test hosts (belt and braces).
--   4. dashboard_workers records which service consumes which scope, so the
--      split is observable without Render dashboard access.
--
-- Reverse: DROP TABLE dashboard_workers; DROP the scope constraints/column;
-- re-run supabase_migration_dashboard_triggers.sql's claim/enqueue.

CREATE OR REPLACE FUNCTION dashboard_trigger_is_test_url(p_url TEXT)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT COALESCE(
        p_url ~* '^[a-z][a-z0-9+.-]*://([^/@]*@)?([^/:?#]*\.)?(invalid|test|example|localhost)\.?(:[0-9]+)?([/?#]|$)',
        FALSE
    );
$$;

ALTER TABLE dashboard_triggers
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'production';

-- Existing probe rows become what they always were.
UPDATE dashboard_triggers
SET scope = 'test'
WHERE dashboard_trigger_is_test_url(source_url) AND scope <> 'test';

ALTER TABLE dashboard_triggers DROP CONSTRAINT IF EXISTS dashboard_triggers_scope_check;
ALTER TABLE dashboard_triggers
    ADD CONSTRAINT dashboard_triggers_scope_check
    CHECK (scope IN ('production', 'staging', 'test'));

ALTER TABLE dashboard_triggers DROP CONSTRAINT IF EXISTS dashboard_triggers_production_not_test_url;
ALTER TABLE dashboard_triggers
    ADD CONSTRAINT dashboard_triggers_production_not_test_url
    CHECK (scope <> 'production' OR NOT dashboard_trigger_is_test_url(source_url));

CREATE INDEX IF NOT EXISTS dashboard_triggers_scope_claim_idx
    ON dashboard_triggers (scope, status, requested_at);

-- ── enqueue: scope parameter, test hosts forced to 'test' ───────────────────
DROP FUNCTION IF EXISTS enqueue_dashboard_trigger(TEXT, TEXT, TEXT);
CREATE OR REPLACE FUNCTION enqueue_dashboard_trigger(
    p_source_url   TEXT,
    p_trigger_kind TEXT,
    p_requested_by TEXT,
    p_scope        TEXT DEFAULT 'production'
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    v_id UUID;
    v_scope TEXT := COALESCE(NULLIF(p_scope, ''), 'production');
BEGIN
    IF dashboard_trigger_is_test_url(p_source_url) THEN
        v_scope := 'test';
    END IF;
    INSERT INTO dashboard_triggers (source_url, trigger_kind, requested_by, scope)
    VALUES (p_source_url, p_trigger_kind, p_requested_by, v_scope)
    ON CONFLICT DO NOTHING
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$$;

-- ── claim: one scope only, p_scope mandatory ────────────────────────────────
DROP FUNCTION IF EXISTS claim_dashboard_trigger(TEXT, INTEGER);
DROP FUNCTION IF EXISTS claim_dashboard_trigger(TEXT, INTEGER, TEXT);
CREATE FUNCTION claim_dashboard_trigger(
    p_worker_token  TEXT,
    p_lease_seconds INTEGER,
    p_scope         TEXT
)
RETURNS TABLE (
    id            UUID,
    source_url    TEXT,
    trigger_kind  TEXT,
    requested_by  TEXT,
    attempt_count INTEGER,
    worker_token  TEXT,
    scope         TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_scope IS NULL OR p_scope NOT IN ('production', 'staging', 'test') THEN
        RAISE EXCEPTION 'claim_dashboard_trigger: bad scope %', p_scope;
    END IF;
    IF COALESCE(p_worker_token, '') = '' THEN
        RAISE EXCEPTION 'claim_dashboard_trigger: empty worker token';
    END IF;

    RETURN QUERY
    WITH candidate AS (
        SELECT t.id
        FROM dashboard_triggers t
        WHERE t.scope = p_scope
          AND (p_scope <> 'production' OR NOT dashboard_trigger_is_test_url(t.source_url))
          AND (
                t.status = 'queued'
             OR (t.status = 'running'
                 AND t.lease_until IS NOT NULL
                 AND t.lease_until <= NOW())
          )
        ORDER BY t.requested_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE dashboard_triggers d
    SET status        = 'running',
        started_at    = COALESCE(d.started_at, NOW()),
        worker_token  = p_worker_token,
        lease_until   = NOW() + make_interval(secs => GREATEST(p_lease_seconds, 60)),
        attempt_count = d.attempt_count + 1
    FROM candidate c
    WHERE d.id = c.id
    RETURNING d.id, d.source_url, d.trigger_kind, d.requested_by,
              d.attempt_count, d.worker_token, d.scope;
END;
$$;

-- ── worker registry ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dashboard_workers (
    worker_token  TEXT PRIMARY KEY,
    scope         TEXT,                 -- NULL = idle: this process consumes nothing
    service_name  TEXT NOT NULL DEFAULT '',
    service_id    TEXT NOT NULL DEFAULT '',
    instance_id   TEXT NOT NULL DEFAULT '',
    git_commit    TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT dashboard_workers_scope_check
        CHECK (scope IS NULL OR scope IN ('production', 'staging', 'test'))
);
ALTER TABLE dashboard_workers ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION register_dashboard_worker(
    p_worker_token TEXT,
    p_scope        TEXT,
    p_service_name TEXT DEFAULT '',
    p_service_id   TEXT DEFAULT '',
    p_instance_id  TEXT DEFAULT '',
    p_git_commit   TEXT DEFAULT '',
    p_note         TEXT DEFAULT ''
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO dashboard_workers AS w (
        worker_token, scope, service_name, service_id, instance_id, git_commit, note
    )
    VALUES (
        p_worker_token, NULLIF(p_scope, ''), COALESCE(p_service_name, ''),
        COALESCE(p_service_id, ''), COALESCE(p_instance_id, ''),
        COALESCE(p_git_commit, ''), LEFT(COALESCE(p_note, ''), 500)
    )
    ON CONFLICT (worker_token) DO UPDATE
    SET last_seen_at = NOW(),
        scope = EXCLUDED.scope,
        note = EXCLUDED.note;
    RETURN TRUE;
END;
$$;
