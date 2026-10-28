-- Security Advisor: "RLS disabled in public" on the Knowledge Library tables.
-- The dashboard reaches them only with the service role (bypasses RLS) from
-- the server. Enabling RLS with no policy denies the anon/authenticated keys.
-- Idempotent.
ALTER TABLE IF EXISTS public.library_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.library_events ENABLE ROW LEVEL SECURITY;
