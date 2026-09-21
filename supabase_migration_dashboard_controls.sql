-- Dashboard interactive controls: retry-from-checkpoint, re-run-from-stage,
-- cancel, live spend, aggregate spend window.
--
-- Apply AFTER supabase_migration_opportunity_stages.sql and
-- supabase_migration_dashboard_triggers.sql.
--
-- What this changes about ADR 011 (stated here so it is not discovered later):
--
--   ADR 011 §2 said a `dead_letter` row may only be reprocessed by a forced
--   run from `discovered`. The dashboard's Retry needs to resume a
--   dead-lettered opportunity from its last good checkpoint (typically
--   `scored`) with a fresh draft-failure budget, so a human can retry a draft
--   without re-paying for extraction and analysis. That is done by ONE new
--   optional parameter on the SAME claim function — not a second claim path
--   and not a second ledger. With the parameter at its default (FALSE) the
--   function behaves exactly as before.
--
--   "Re-run from [stage]" is a deliberate human rewind of `pipeline_stage`
--   plus removal of the checkpoint keys that later stages produced. The
--   normal resume path then does the rest. Rewind is refused while a lease is
--   held. It is the same mechanism a `force` reset uses, applied partially.
--
-- Additive and reversible (drop the new functions; the claim function can be
-- re-created from supabase_migration_opportunity_stages.sql).

-- ── 1. claim_opportunity_processing + p_retry_dead_letter ───────────────────

DROP FUNCTION IF EXISTS claim_opportunity_processing(TEXT, TEXT, INTEGER, BOOLEAN, TEXT);
DROP FUNCTION IF EXISTS claim_opportunity_processing(TEXT, TEXT, INTEGER, BOOLEAN, TEXT, BOOLEAN);

CREATE FUNCTION claim_opportunity_processing(
    p_source_url TEXT,
    p_title TEXT DEFAULT '',
    p_lease_seconds INTEGER DEFAULT 1800,
    p_force BOOLEAN DEFAULT FALSE,
    p_claim_token TEXT DEFAULT '',
    p_retry_dead_letter BOOLEAN DEFAULT FALSE
)
RETURNS TABLE (
    acquired BOOLEAN,
    state TEXT,
    attempt_count INTEGER,
    claim_token TEXT,
    pipeline_stage TEXT,
    draft_fail_count INTEGER
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH claimed AS (
        INSERT INTO opportunity_processing (
            source_url, title, state, attempt_count, lease_until, claim_token,
            pipeline_stage, checkpoint, draft_fail_count
        )
        VALUES (
            p_source_url, COALESCE(p_title, ''), 'processing', 1,
            NOW() + make_interval(secs => GREATEST(p_lease_seconds, 60)),
            p_claim_token,
            'discovered',
            '{}'::jsonb,
            0
        )
        ON CONFLICT (source_url) DO UPDATE
        SET
            title = EXCLUDED.title,
            state = 'processing',
            attempt_count = opportunity_processing.attempt_count + 1,
            last_error = NULL,
            updated_at = NOW(),
            lease_until = NOW() + make_interval(secs => GREATEST(p_lease_seconds, 60)),
            claim_token = p_claim_token,
            completed_at = NULL,
            -- force = full reprocess; otherwise RESUME is mandatory.
            pipeline_stage = CASE
                WHEN p_force THEN 'discovered'
                ELSE opportunity_processing.pipeline_stage
            END,
            checkpoint = CASE
                WHEN p_force THEN '{}'::jsonb
                ELSE opportunity_processing.checkpoint
            END,
            -- A dead-letter retry keeps the checkpoint but gets a fresh
            -- MAX_DRAFT_FAILURES budget; otherwise the count is preserved.
            draft_fail_count = CASE
                WHEN p_force THEN 0
                WHEN p_retry_dead_letter
                     AND opportunity_processing.state = 'dead_letter' THEN 0
                ELSE opportunity_processing.draft_fail_count
            END
        WHERE
            (
                opportunity_processing.state IN ('pending', 'failed')
                OR opportunity_processing.lease_until IS NULL
                OR opportunity_processing.lease_until <= NOW()
            )
            AND (
                p_force
                OR opportunity_processing.state NOT IN ('completed', 'dead_letter')
                OR (p_retry_dead_letter AND opportunity_processing.state = 'dead_letter')
            )
        RETURNING
            opportunity_processing.state,
            opportunity_processing.attempt_count,
            opportunity_processing.claim_token,
            opportunity_processing.pipeline_stage,
            opportunity_processing.draft_fail_count
    )
    SELECT TRUE, claimed.state, claimed.attempt_count, claimed.claim_token,
           claimed.pipeline_stage, claimed.draft_fail_count
    FROM claimed
    UNION ALL
    SELECT FALSE, p.state, p.attempt_count, '', p.pipeline_stage, p.draft_fail_count
    FROM opportunity_processing p
    WHERE p.source_url = p_source_url
      AND NOT EXISTS (SELECT 1 FROM claimed)
    LIMIT 1;
END;
$$;

-- ── 2. rewind_opportunity_stage ─────────────────────────────────────────────
-- Human "re-run from [stage]". Sets pipeline_stage back and removes the
-- checkpoint keys produced by later stages, so the ordinary resume path
-- re-runs exactly those stages. Refused while a lease is held.
--
-- Key ownership mirrors intelligence/pipeline_stages.OpportunityContext:
--   extract → full_text, title, opp_id, source_url, dedup_url
--   score   → analysis, recommendation, fit_score, win_prob,
--             airtable_record_id, client_intelligence, intelligence
--   draft   → matched_team_result, budget, proposal_sections, matrix,
--             submission_type
CREATE OR REPLACE FUNCTION rewind_opportunity_stage(
    p_source_url TEXT,
    p_to_stage   TEXT
)
RETURNS TABLE (rewound BOOLEAN, reason TEXT, pipeline_stage TEXT)
LANGUAGE plpgsql
AS $$
DECLARE
    v_row opportunity_processing%ROWTYPE;
    v_keep JSONB;
BEGIN
    IF p_to_stage NOT IN ('extracted', 'scored') THEN
        RETURN QUERY SELECT FALSE, 'to_stage must be extracted or scored (use force for discovered)'::TEXT, NULL::TEXT;
        RETURN;
    END IF;

    SELECT * INTO v_row FROM opportunity_processing WHERE source_url = p_source_url FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT FALSE, 'no ledger row'::TEXT, NULL::TEXT;
        RETURN;
    END IF;

    IF v_row.state = 'processing' AND v_row.lease_until IS NOT NULL AND v_row.lease_until > NOW() THEN
        RETURN QUERY SELECT FALSE, 'a worker holds the lease — cancel or wait first'::TEXT, v_row.pipeline_stage;
        RETURN;
    END IF;

    IF p_to_stage = 'extracted' THEN
        IF COALESCE(v_row.checkpoint->>'full_text', '') = '' THEN
            RETURN QUERY SELECT FALSE, 'checkpoint has no extracted text to re-score from'::TEXT, v_row.pipeline_stage;
            RETURN;
        END IF;
        v_keep := jsonb_strip_nulls(jsonb_build_object(
            'full_text',  v_row.checkpoint->'full_text',
            'title',      v_row.checkpoint->'title',
            'opp_id',     v_row.checkpoint->'opp_id',
            'source_url', v_row.checkpoint->'source_url',
            'dedup_url',  v_row.checkpoint->'dedup_url'
        ));
    ELSE  -- scored: keep analysis, drop draft artefacts
        IF v_row.checkpoint->'analysis' IS NULL THEN
            RETURN QUERY SELECT FALSE, 'checkpoint has no analysis to re-draft from'::TEXT, v_row.pipeline_stage;
            RETURN;
        END IF;
        v_keep := v_row.checkpoint
            - 'matched_team_result' - 'budget' - 'proposal_sections'
            - 'matrix' - 'submission_type';
    END IF;

    UPDATE opportunity_processing
    SET pipeline_stage   = p_to_stage,
        checkpoint       = v_keep,
        draft_fail_count = 0,
        state            = 'pending',
        lease_until      = NULL,
        claim_token      = '',      -- column is NOT NULL; '' is the ledger's unacquired value
        completed_at     = NULL,
        last_error       = NULL,
        updated_at       = NOW()
    WHERE source_url = p_source_url;

    RETURN QUERY SELECT TRUE, 'rewound'::TEXT, p_to_stage;
END;
$$;

-- ── 3. dashboard_triggers: cancel + live spend ──────────────────────────────

ALTER TABLE dashboard_triggers
    DROP CONSTRAINT IF EXISTS dashboard_triggers_status_check;
ALTER TABLE dashboard_triggers
    ADD CONSTRAINT dashboard_triggers_status_check
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'));

ALTER TABLE dashboard_triggers
    DROP CONSTRAINT IF EXISTS dashboard_triggers_trigger_kind_check;
ALTER TABLE dashboard_triggers
    ADD CONSTRAINT dashboard_triggers_trigger_kind_check
    CHECK (trigger_kind IN ('submit_url', 'draft_existing', 'retry_dead_letter',
                            'rerun_from_extracted', 'rerun_from_scored'));

ALTER TABLE dashboard_triggers ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;
ALTER TABLE dashboard_triggers ADD COLUMN IF NOT EXISTS cancelled_by        TEXT;
ALTER TABLE dashboard_triggers ADD COLUMN IF NOT EXISTS last_heartbeat_at   TIMESTAMPTZ;
-- Live figures from utils/observability — the same bucket the cap enforces.
ALTER TABLE dashboard_triggers ADD COLUMN IF NOT EXISTS spend_usd           NUMERIC(12,6);
ALTER TABLE dashboard_triggers ADD COLUMN IF NOT EXISTS spend_limit_usd     NUMERIC(12,6);
ALTER TABLE dashboard_triggers ADD COLUMN IF NOT EXISTS provider_calls      INTEGER;
ALTER TABLE dashboard_triggers ADD COLUMN IF NOT EXISTS spend_known         BOOLEAN;

-- Heartbeat now also carries spend. Same lease semantics as before.
DROP FUNCTION IF EXISTS heartbeat_dashboard_trigger(UUID, TEXT, INTEGER);
DROP FUNCTION IF EXISTS heartbeat_dashboard_trigger(UUID, TEXT, INTEGER, NUMERIC, NUMERIC, INTEGER, BOOLEAN);
CREATE FUNCTION heartbeat_dashboard_trigger(
    p_id             UUID,
    p_worker_token   TEXT,
    p_lease_seconds  INTEGER DEFAULT 3600,
    p_spend_usd      NUMERIC DEFAULT NULL,
    p_spend_limit    NUMERIC DEFAULT NULL,
    p_provider_calls INTEGER DEFAULT NULL,
    p_spend_known    BOOLEAN DEFAULT NULL
)
RETURNS TABLE (alive BOOLEAN, cancel_requested BOOLEAN)
LANGUAGE plpgsql
AS $$
DECLARE
    v_cancel TIMESTAMPTZ;
    v_rows INTEGER;
BEGIN
    UPDATE dashboard_triggers
    SET lease_until       = NOW() + make_interval(secs => GREATEST(p_lease_seconds, 60)),
        last_heartbeat_at = NOW(),
        spend_usd         = COALESCE(p_spend_usd, spend_usd),
        spend_limit_usd   = COALESCE(p_spend_limit, spend_limit_usd),
        provider_calls    = COALESCE(p_provider_calls, provider_calls),
        spend_known       = COALESCE(p_spend_known, spend_known)
    WHERE id = p_id
      AND worker_token = p_worker_token
      AND status = 'running'
    RETURNING dashboard_triggers.cancel_requested_at INTO v_cancel;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN QUERY SELECT v_rows > 0, v_cancel IS NOT NULL;
END;
$$;

-- Cancel. Queued → cancelled immediately (the worker never sees it).
-- Running → cancel_requested_at is set; the worker's next heartbeat sees it
-- and asks the pipeline to halt before its next model call. The function
-- reports which of the two happened so the UI can say so truthfully.
CREATE OR REPLACE FUNCTION cancel_dashboard_trigger(
    p_id           UUID,
    p_cancelled_by TEXT
)
RETURNS TABLE (outcome TEXT)
LANGUAGE plpgsql
AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT status INTO v_status FROM dashboard_triggers WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'not_found'::TEXT; RETURN;
    END IF;

    IF v_status = 'queued' THEN
        UPDATE dashboard_triggers
        SET status = 'cancelled', finished_at = NOW(),
            cancel_requested_at = NOW(), cancelled_by = p_cancelled_by,
            error_kind = 'CANCELLED', error_message = 'cancelled before it started'
        WHERE id = p_id;
        RETURN QUERY SELECT 'cancelled_before_start'::TEXT; RETURN;
    END IF;

    IF v_status = 'running' THEN
        UPDATE dashboard_triggers
        SET cancel_requested_at = COALESCE(cancel_requested_at, NOW()),
            cancelled_by = COALESCE(cancelled_by, p_cancelled_by)
        WHERE id = p_id;
        RETURN QUERY SELECT 'halt_requested'::TEXT; RETURN;
    END IF;

    RETURN QUERY SELECT ('already_' || v_status)::TEXT;
END;
$$;

-- finish() may now record 'cancelled' and the final spend.
DROP FUNCTION IF EXISTS finish_dashboard_trigger(UUID, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB);
DROP FUNCTION IF EXISTS finish_dashboard_trigger(UUID, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, NUMERIC, NUMERIC, INTEGER, BOOLEAN);
CREATE FUNCTION finish_dashboard_trigger(
    p_id            UUID,
    p_worker_token  TEXT,
    p_status        TEXT,
    p_error_kind    TEXT DEFAULT NULL,
    p_error_message TEXT DEFAULT NULL,
    p_execution_id  TEXT DEFAULT NULL,
    p_result        JSONB DEFAULT '{}'::jsonb,
    p_spend_usd     NUMERIC DEFAULT NULL,
    p_spend_limit   NUMERIC DEFAULT NULL,
    p_provider_calls INTEGER DEFAULT NULL,
    p_spend_known   BOOLEAN DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    IF p_status NOT IN ('succeeded', 'failed', 'cancelled') THEN
        RAISE EXCEPTION 'finish_dashboard_trigger: bad status %', p_status;
    END IF;

    UPDATE dashboard_triggers
    SET status         = p_status,
        finished_at    = NOW(),
        lease_until    = NULL,
        error_kind     = p_error_kind,
        error_message  = LEFT(COALESCE(p_error_message, ''), 2000),
        execution_id   = p_execution_id,
        result_summary = COALESCE(p_result, '{}'::jsonb),
        spend_usd      = COALESCE(p_spend_usd, spend_usd),
        spend_limit_usd = COALESCE(p_spend_limit, spend_limit_usd),
        provider_calls = COALESCE(p_provider_calls, provider_calls),
        spend_known    = COALESCE(p_spend_known, spend_known)
    WHERE id = p_id
      AND worker_token = p_worker_token
      AND status = 'running';
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows > 0;
END;
$$;

-- Aggregate dashboard spend over a trailing window. Cancelled and failed
-- runs count — cost that was incurred is cost that was incurred.
CREATE OR REPLACE FUNCTION dashboard_spend_window(p_hours INTEGER DEFAULT 24)
RETURNS TABLE (
    spent_usd       NUMERIC,
    runs            BIGINT,
    runs_unknown_cost BIGINT,
    window_start    TIMESTAMPTZ
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        COALESCE(SUM(spend_usd), 0)::NUMERIC,
        COUNT(*)::BIGINT,
        COUNT(*) FILTER (WHERE spend_known IS FALSE)::BIGINT,
        NOW() - make_interval(hours => GREATEST(p_hours, 1))
    FROM dashboard_triggers
    WHERE started_at IS NOT NULL
      AND started_at >= NOW() - make_interval(hours => GREATEST(p_hours, 1));
$$;
