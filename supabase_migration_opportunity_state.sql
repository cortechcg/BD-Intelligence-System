-- Durable, lease-based processing state. opportunities_cache stores successful
-- source material/search vectors; it is deliberately not the workflow ledger.

CREATE TABLE IF NOT EXISTS opportunity_processing (
    source_url TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'processing', 'failed', 'completed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ,
    -- The worker that acquired the lease. Finalizers must present this value;
    -- an old worker may wake after its lease expires and must not complete or
    -- fail a newer worker's claim.
    claim_token TEXT NOT NULL DEFAULT '',
    completed_at TIMESTAMPTZ
);

-- Safe for installations that created the table from an earlier revision of
-- this migration before claim-token ownership was added.
ALTER TABLE opportunity_processing
    ADD COLUMN IF NOT EXISTS claim_token TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS opportunity_processing_retriable_idx
    ON opportunity_processing (state, lease_until, updated_at);

-- Atomically claim one opportunity. A live lease prevents concurrent workers
-- from duplicating paid analysis; expired leases recover after a crash.
CREATE OR REPLACE FUNCTION claim_opportunity_processing(
    p_source_url TEXT,
    p_title TEXT DEFAULT '',
    p_lease_seconds INTEGER DEFAULT 1800,
    p_force BOOLEAN DEFAULT FALSE,
    p_claim_token TEXT DEFAULT ''
)
RETURNS TABLE (acquired BOOLEAN, state TEXT, attempt_count INTEGER, claim_token TEXT)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH claimed AS (
        INSERT INTO opportunity_processing (
            source_url, title, state, attempt_count, lease_until, claim_token
        )
        VALUES (
            p_source_url, COALESCE(p_title, ''), 'processing', 1,
            NOW() + make_interval(secs => GREATEST(p_lease_seconds, 60)),
            p_claim_token
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
            completed_at = NULL
        WHERE
            -- Manual ``force`` may reprocess a completed item, but it must
            -- never steal a non-expired lease from another worker.
            (
                opportunity_processing.state IN ('pending', 'failed')
                OR opportunity_processing.lease_until IS NULL
                OR opportunity_processing.lease_until <= NOW()
            )
            AND (
                p_force
                OR opportunity_processing.state <> 'completed'
            )
        RETURNING opportunity_processing.state, opportunity_processing.attempt_count,
                  opportunity_processing.claim_token
    )
    SELECT TRUE, claimed.state, claimed.attempt_count, claimed.claim_token FROM claimed
    UNION ALL
    SELECT FALSE, p.state, p.attempt_count, ''
    FROM opportunity_processing p
    WHERE p.source_url = p_source_url
      AND NOT EXISTS (SELECT 1 FROM claimed)
    LIMIT 1;
END;
$$;

CREATE OR REPLACE FUNCTION complete_opportunity_processing(
    p_source_url TEXT,
    p_claim_token TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    UPDATE opportunity_processing
    SET state = 'completed', updated_at = NOW(), completed_at = NOW(),
        lease_until = NULL, last_error = NULL
    WHERE source_url = p_source_url
      AND state = 'processing'
      AND claim_token = p_claim_token;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;

CREATE OR REPLACE FUNCTION fail_opportunity_processing(
    p_source_url TEXT,
    p_error TEXT DEFAULT NULL,
    p_claim_token TEXT DEFAULT ''
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    UPDATE opportunity_processing
    SET state = 'failed', updated_at = NOW(), lease_until = NULL,
        last_error = LEFT(COALESCE(p_error, 'processing did not complete'), 2000)
    WHERE source_url = p_source_url
      AND state = 'processing'
      AND claim_token = p_claim_token;
    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count = 1;
END;
$$;
