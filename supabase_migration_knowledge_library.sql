-- Knowledge Library: past proposals and CVs uploaded by the BD team.
-- Apply after supabase_migration_dashboard_controls.sql.
-- Idempotent and additive. Does not call Airtable.
--
-- To reverse this migration, run:
-- DROP TABLE IF EXISTS public.library_events;
-- DROP TABLE IF EXISTS public.library_documents;
-- ALTER TABLE public.consultants DROP COLUMN IF EXISTS email;
-- ALTER TABLE public.consultants DROP COLUMN IF EXISTS created_by_email;
-- and restore dashboard_triggers_trigger_kind_check without library_ingest.

CREATE TABLE IF NOT EXISTS library_documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind TEXT NOT NULL CHECK (kind IN ('proposal', 'cv')),
  title TEXT NOT NULL DEFAULT '',
  original_filename TEXT NOT NULL,
  storage_path TEXT NOT NULL UNIQUE,
  mime_type TEXT NOT NULL,
  size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
  content_hash TEXT NOT NULL,
  uploaded_by_email TEXT NOT NULL,
  uploaded_by_name TEXT NOT NULL DEFAULT '',
  consultant_id UUID REFERENCES consultants(id),
  client TEXT,
  donor TEXT,
  country TEXT,
  sectors TEXT[] NOT NULL DEFAULT '{}',
  submission_date DATE,
  proposal_type TEXT CHECK (proposal_type IN ('technical', 'eoi', 'financial', 'other')),
  outcome TEXT NOT NULL DEFAULT 'unknown' CHECK (outcome IN ('won', 'lost', 'pending', 'unknown')),
  status TEXT NOT NULL DEFAULT 'uploaded'
         CHECK (status IN ('uploaded', 'queued', 'extracting', 'embedding', 'ready', 'failed')),
  error_kind TEXT,
  error_message TEXT,
  chunk_count INTEGER,
  extracted_chars INTEGER,
  embedding_ref TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  archived_at TIMESTAMPTZ,
  archived_by TEXT,
  CONSTRAINT cv_needs_consultant CHECK (kind <> 'cv' OR consultant_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS library_documents_live_hash_uidx
  ON library_documents (kind, content_hash) WHERE archived_at IS NULL;

CREATE INDEX IF NOT EXISTS library_documents_list_idx
  ON library_documents (kind, status, created_at DESC);

CREATE INDEX IF NOT EXISTS library_documents_consultant_idx
  ON library_documents (consultant_id);

CREATE TABLE IF NOT EXISTS library_events (
  id BIGSERIAL PRIMARY KEY,
  document_id UUID REFERENCES library_documents(id),
  actor_email TEXT NOT NULL,
  action TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}'::jsonb,
  at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE consultants ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE consultants ADD COLUMN IF NOT EXISTS created_by_email TEXT;

ALTER TABLE dashboard_triggers
    DROP CONSTRAINT IF EXISTS dashboard_triggers_trigger_kind_check;
ALTER TABLE dashboard_triggers
    ADD CONSTRAINT dashboard_triggers_trigger_kind_check
    CHECK (trigger_kind IN (
        'submit_url', 'draft_existing', 'retry_dead_letter',
        'rerun_from_extracted', 'rerun_from_scored', 'library_ingest'
    ));
