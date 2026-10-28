-- Win/loss lesson memory (intelligence/learning.py). Idempotent: safe to run
-- any number of times in the BD project (cnmynhgjkxkojhlereoj).
--   python scripts/apply_sql_migration.py supabase_migration_win_loss.sql
-- Until it exists, PostgREST answers PGRST205 for public.win_loss_memory.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.win_loss_memory (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    opportunity_id text NOT NULL,
    outcome text NOT NULL,
    client text,
    donor text,
    lessons jsonb,
    embedding vector(1536),
    recorded_at timestamptz DEFAULT now()
);

-- Server-side service role only; anon/authenticated get nothing.
ALTER TABLE public.win_loss_memory ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS win_loss_memory_embedding_idx
    ON public.win_loss_memory USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE OR REPLACE FUNCTION public.match_win_loss_memory(
    query_embedding vector(1536), match_threshold float, match_count int
)
RETURNS TABLE (opportunity_id text, outcome text, lessons jsonb, similarity float)
LANGUAGE plpgsql
SET search_path = public, extensions
AS $$
BEGIN
    RETURN QUERY
    SELECT w.opportunity_id, w.outcome, w.lessons,
           1 - (w.embedding <=> query_embedding) AS similarity
    FROM public.win_loss_memory w
    WHERE w.embedding IS NOT NULL
      AND 1 - (w.embedding <=> query_embedding) > match_threshold
    ORDER BY similarity DESC
    LIMIT match_count;
END;
$$;

-- PostgREST must see the new table without a restart.
NOTIFY pgrst, 'reload schema';
