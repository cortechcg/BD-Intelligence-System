-- Additive observed-fact columns on opportunities_cache for the Phase 3
-- market digest. Airtable is not the source of truth. Do not invent
-- Airtable fields.
--
-- Apply once in the Supabase SQL editor. This file does not run itself.
-- Python fail-opens (digest still runs from cache timestamps + Airtable
-- secondary fields, or an honest empty/insufficient digest) if these
-- columns are missing, same pattern as supabase_migration_content_hash.sql.

ALTER TABLE opportunities_cache
    ADD COLUMN IF NOT EXISTS thematic_areas TEXT[];

ALTER TABLE opportunities_cache
    ADD COLUMN IF NOT EXISTS locations TEXT[];

ALTER TABLE opportunities_cache
    ADD COLUMN IF NOT EXISTS donor TEXT;

ALTER TABLE opportunities_cache
    ADD COLUMN IF NOT EXISTS discovered_at DATE;

COMMENT ON COLUMN opportunities_cache.thematic_areas IS
    'Extracted thematic labels as stored on this opportunity. Empty/null = unknown. Not a taxonomy; do not invent Other/WASH buckets.';
COMMENT ON COLUMN opportunities_cache.locations IS
    'Extracted geography labels as stored. Empty/null = unknown. Not a region roll-up.';
COMMENT ON COLUMN opportunities_cache.donor IS
    'Extracted donor/funder name as stored. Cadence counts only after Phase 2 org match.';
COMMENT ON COLUMN opportunities_cache.discovered_at IS
    'Date this opportunity was stored/discovered (YYYY-MM-DD). Never a guessed posting date.';
