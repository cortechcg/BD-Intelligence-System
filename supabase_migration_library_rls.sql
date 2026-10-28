-- Supabase advisor fixes for the BD project (cnmynhgjkxkojhlereoj).
-- Idempotent: safe to re-run. Applied 2026-10-28.
--
-- Security: "RLS disabled in public" on the Knowledge Library tables.
-- The dashboard reaches them only with the service role (bypasses RLS) from
-- the server; nothing in the repo uses the anon key. Enabling RLS with no
-- policy denies the anon/authenticated keys.
ALTER TABLE IF EXISTS public.library_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.library_events ENABLE ROW LEVEL SECURITY;

-- Security: "Function search_path mutable". Pin every public function this
-- project owns (extension functions excluded) to public, whatever its
-- signature. pgvector lives in public, so match_* still resolve <=>.
DO $$
DECLARE f regprocedure;
BEGIN
    FOR f IN
        SELECT p.oid::regprocedure
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.prokind = 'f'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
          AND NOT EXISTS (
              SELECT 1 FROM unnest(COALESCE(p.proconfig, '{}')) x WHERE x LIKE 'search_path=%'
          )
    LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = public', f);
    END LOOP;
END $$;

-- Performance: "Unindexed foreign key".
DO $$
BEGIN
    IF to_regclass('public.library_events') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS library_events_document_id_idx
            ON public.library_events (document_id);
    END IF;
END $$;

-- Performance: "Duplicate index". opportunities_cache_source_url_key (the
-- UNIQUE constraint) already indexes source_url.
DROP INDEX IF EXISTS public.opportunities_cache_source_url_idx;

NOTIFY pgrst, 'reload schema';
