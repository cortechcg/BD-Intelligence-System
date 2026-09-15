-- Phase 7: durable pipeline_stage + checkpoint on the EXISTING
-- opportunity_processing ledger. Do not create a second workflow table.
-- Apply AFTER supabase_migration_opportunity_state.sql.
-- This file does not run itself. Do not invent Airtable fields.

-- Lease `state` stays pending/processing/failed/completed (+ dead_letter).
-- Pipeline progress uses exact names: discovered → extracted → scored →
-- drafted → reviewed → outcome.

ALTER TABLE opportunity_processing
    ADD COLUMN IF NOT EXISTS pipeline_stage TEXT NOT NULL DEFAULT 'discovered';

ALTER TABLE opportunity_processing
    ADD COLUMN IF NOT EXISTS checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE opportunity_processing
    ADD COLUMN IF NOT EXISTS draft_fail_count INTEGER NOT NULL DEFAULT 0
        CHECK (draft_fail_count >= 0);

ALTER TABLE opportunity_processing
    DROP CONSTRAINT IF EXISTS opportunity_processing_pipeline_stage_check;

ALTER TABLE opportunity_processing
    ADD CONSTRAINT opportunity_processing_pipeline_stage_check
    CHECK (pipeline_stage IN (
        'discovered', 'extracted', 'scored', 'drafted', 'reviewed', 'outcome'
    ));

-- Inline column CHECK from the original migration is named
-- opportunity_processing_state_check on Postgres.
ALTER TABLE opportunity_processing
    DROP CONSTRAINT IF EXISTS opportunity_processing_state_check;

ALTER TABLE opportunity_processing
    ADD CONSTRAINT opportunity_processing_state_check
    CHECK (state IN ('pending', 'processing', 'failed', 'completed', 'dead_letter'));

CREATE INDEX IF NOT EXISTS opportunity_processing_stage_idx
    ON opportunity_processing (pipeline_stage, state);

-- Return type gains pipeline_stage + draft_fail_count, so REPLACE is not enough.
DROP FUNCTION IF EXISTS claim_opportunity_processing(TEXT, TEXT, INTEGER, BOOLEAN, TEXT);

CREATE FUNCTION claim_opportunity_processing(
    p_source_url TEXT,
    p_title TEXT DEFAULT '',
    p_lease_seconds INTEGER DEFAULT 1800,
    p_force BOOLEAN DEFAULT FALSE,
    p_claim_token TEXT DEFAULT ''
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
            draft_fail_count = CASE
                WHEN p_force THEN 0
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

DROP FUNCTION IF EXISTS fail_opportunity_processing(TEXT, TEXT, TEXT);

CREATE FUNCTION fail_opportunity_processing(
    p_source_url TEXT,
    p_error TEXT DEFAULT NULL,
    p_claim_token TEXT DEFAULT '',
    p_increment_draft_fail BOOLEAN DEFAULT FALSE
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    UPDATE opportunity_processing
    SET state = 'failed',
        updated_at = NOW(),
        lease_until = NULL,
        last_error = LEFT(COALESCE(p_error, 'processing did not complete'), 2000),
        draft_fail_count = CASE
            WHEN p_increment_draft_fail THEN opportunity_processing.draft_fail_count + 1
            ELSE opportunity_processing.draft_fail_count
        END
    WHERE source_url = p_source_url
      AND state = 'processing'
      AND claim_token = p_claim_token;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

CREATE OR REPLACE FUNCTION persist_opportunity_stage(
    p_source_url TEXT,
    p_claim_token TEXT,
    p_pipeline_stage TEXT,
    p_checkpoint JSONB DEFAULT '{}'::jsonb
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    IF p_pipeline_stage NOT IN (
        'discovered', 'extracted', 'scored', 'drafted', 'reviewed', 'outcome'
    ) THEN
        RETURN FALSE;
    END IF;
    UPDATE opportunity_processing
    SET pipeline_stage = p_pipeline_stage,
        checkpoint = COALESCE(p_checkpoint, '{}'::jsonb),
        updated_at = NOW()
    WHERE source_url = p_source_url
      AND state = 'processing'
      AND claim_token = p_claim_token;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

CREATE OR REPLACE FUNCTION dead_letter_opportunity_processing(
    p_source_url TEXT,
    p_claim_token TEXT,
    p_error TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    UPDATE opportunity_processing
    SET state = 'dead_letter',
        updated_at = NOW(),
        lease_until = NULL,
        last_error = LEFT(COALESCE(p_error, 'drafting dead-lettered'), 2000)
    WHERE source_url = p_source_url
      AND state = 'processing'
      AND claim_token = p_claim_token;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

-- Human-in-the-loop only. Never called to submit to a client.
CREATE OR REPLACE FUNCTION record_human_pipeline_stage(
    p_source_url TEXT,
    p_pipeline_stage TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    IF p_pipeline_stage NOT IN ('reviewed', 'outcome') THEN
        RETURN FALSE;
    END IF;
    UPDATE opportunity_processing
    SET pipeline_stage = p_pipeline_stage,
        updated_at = NOW()
    WHERE source_url = p_source_url
      AND pipeline_stage IN ('drafted', 'reviewed', 'outcome')
      AND (
          (p_pipeline_stage = 'reviewed' AND pipeline_stage IN ('drafted', 'reviewed'))
          OR (p_pipeline_stage = 'outcome' AND pipeline_stage IN ('drafted', 'reviewed', 'outcome'))
      );
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

COMMENT ON COLUMN opportunity_processing.pipeline_stage IS
    'Agent/human progress: discovered → extracted → scored → drafted → reviewed → outcome. Lease ownership stays in state.';
COMMENT ON COLUMN opportunity_processing.checkpoint IS
    'JSONB resume payload (extracted text, analysis, airtable id, draft). Preserved on failed reclaim unless force.';
COMMENT ON COLUMN opportunity_processing.draft_fail_count IS
    'BID/WATCH drafting failures. At 3, state becomes dead_letter.';
