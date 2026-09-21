-- Dashboard trigger queue.
--
-- Scope: this is the *request* queue for manually-triggered runs. It is NOT a
-- second workflow ledger. `opportunity_processing` remains the single source of
-- truth for pipeline state (ADR 011 §1) — a row here records "a human asked for
-- this and here is what happened to the request", and nothing more. Pipeline
-- stage, checkpoint, resume, dead-letter and spend-cap state are all read back
-- out of `opportunity_processing`, never duplicated here.
--
-- Apply AFTER supabase_migration_opportunity_state.sql and
-- supabase_migration_opportunity_stages.sql.
--
-- Additive and reversible: DROP TABLE public.dashboard_triggers CASCADE;

CREATE TABLE IF NOT EXISTS dashboard_triggers (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_url    TEXT NOT NULL,
    trigger_kind  TEXT NOT NULL
                  CHECK (trigger_kind IN ('submit_url', 'draft_existing')),
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    requested_by  TEXT NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    worker_token  TEXT,
    lease_until   TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    -- Stable taxonomy string from utils/errors.ErrorType, plus 'DEAD_LETTER'
    -- and 'SPEND_CAP_ERROR'. Never a free-form guess.
    error_kind    TEXT,
    error_message TEXT,
    execution_id  TEXT,
    -- Small, non-authoritative echo of the run outcome for the queue list
    -- (recommendation / stage / draft path). The detail view always re-reads
    -- opportunity_processing rather than trusting this.
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS dashboard_triggers_status_idx
    ON dashboard_triggers (status, requested_at DESC);

CREATE INDEX IF NOT EXISTS dashboard_triggers_requested_idx
    ON dashboard_triggers (requested_at DESC);

-- One in-flight request per URL. A second click while a run is queued or
-- running is rejected by the database, not merely greyed out in the UI.
CREATE UNIQUE INDEX IF NOT EXISTS dashboard_triggers_inflight_uidx
    ON dashboard_triggers (source_url)
    WHERE status IN ('queued', 'running');

-- ── enqueue ────────────────────────────────────────────────────────────────
-- Returns the row id, or NULL when an identical request is already in flight.
CREATE OR REPLACE FUNCTION enqueue_dashboard_trigger(
    p_source_url   TEXT,
    p_trigger_kind TEXT,
    p_requested_by TEXT
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    v_id UUID;
BEGIN
    INSERT INTO dashboard_triggers (source_url, trigger_kind, requested_by)
    VALUES (p_source_url, p_trigger_kind, p_requested_by)
    ON CONFLICT DO NOTHING
    RETURNING id INTO v_id;

    RETURN v_id;
END;
$$;

-- ── claim ──────────────────────────────────────────────────────────────────
-- Atomically take the oldest queued request, or reclaim one whose worker lease
-- expired (the worker died mid-run). Same lease discipline as
-- claim_opportunity_processing: the caller supplies a token and the database
-- echoes back the token it actually accepted.
CREATE OR REPLACE FUNCTION claim_dashboard_trigger(
    p_worker_token  TEXT,
    p_lease_seconds INTEGER DEFAULT 3600
)
RETURNS TABLE (
    id            UUID,
    source_url    TEXT,
    trigger_kind  TEXT,
    requested_by  TEXT,
    attempt_count INTEGER,
    worker_token  TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH candidate AS (
        SELECT t.id
        FROM dashboard_triggers t
        WHERE t.status = 'queued'
           OR (t.status = 'running'
               AND t.lease_until IS NOT NULL
               AND t.lease_until <= NOW())
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
              d.attempt_count, d.worker_token;
END;
$$;

-- ── heartbeat ──────────────────────────────────────────────────────────────
-- Pipeline runs take minutes. Extend the lease so a long but healthy run is
-- not reclaimed by a second worker.
CREATE OR REPLACE FUNCTION heartbeat_dashboard_trigger(
    p_id            UUID,
    p_worker_token  TEXT,
    p_lease_seconds INTEGER DEFAULT 3600
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    UPDATE dashboard_triggers
    SET lease_until = NOW() + make_interval(secs => GREATEST(p_lease_seconds, 60))
    WHERE id = p_id
      AND worker_token = p_worker_token
      AND status = 'running';
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows > 0;
END;
$$;

-- ── finish ─────────────────────────────────────────────────────────────────
-- Only the lease holder may finalise. A stale worker cannot overwrite the
-- result of the run that superseded it.
CREATE OR REPLACE FUNCTION finish_dashboard_trigger(
    p_id            UUID,
    p_worker_token  TEXT,
    p_status        TEXT,
    p_error_kind    TEXT DEFAULT NULL,
    p_error_message TEXT DEFAULT NULL,
    p_execution_id  TEXT DEFAULT NULL,
    p_result        JSONB DEFAULT '{}'::jsonb
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    IF p_status NOT IN ('succeeded', 'failed') THEN
        RAISE EXCEPTION 'finish_dashboard_trigger: bad status %', p_status;
    END IF;

    UPDATE dashboard_triggers
    SET status         = p_status,
        finished_at    = NOW(),
        lease_until    = NULL,
        error_kind     = p_error_kind,
        error_message  = LEFT(COALESCE(p_error_message, ''), 2000),
        execution_id   = p_execution_id,
        result_summary = COALESCE(p_result, '{}'::jsonb)
    WHERE id = p_id
      AND worker_token = p_worker_token
      AND status = 'running';
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows > 0;
END;
$$;

-- RLS: the dashboard reaches Postgres only through the service role (which
-- bypasses RLS) from the server side. The anon key is never given this table,
-- and the browser never talks to PostgREST directly. Enabling RLS with no
-- permissive policy makes that explicit: an anon/authenticated key gets
-- nothing, so a leaked anon key cannot enqueue pipeline runs.
ALTER TABLE dashboard_triggers ENABLE ROW LEVEL SECURITY;
