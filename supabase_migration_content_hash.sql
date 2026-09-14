-- Additive: document-body identity on the successful-content cache.
-- opportunities_cache already upserts on source_url (exact URL). content_hash
-- is a second uniqueness key for the same tender body posted under another URL.
-- Multiple NULLs are allowed so rows written before this migration still load.
--
-- Apply once in the Supabase SQL editor. This file does not run itself.
-- Exact-URL completion remains opportunity_processing (workflow ledger).

ALTER TABLE opportunities_cache
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS opportunities_cache_content_hash_uidx
    ON opportunities_cache (content_hash)
    WHERE content_hash IS NOT NULL;

COMMENT ON COLUMN opportunities_cache.content_hash IS
    'SHA-256 of whitespace-normalized extracted text (utils.hashing.content_hash). Uniqueness identity for the document body, not a workflow lease.';
