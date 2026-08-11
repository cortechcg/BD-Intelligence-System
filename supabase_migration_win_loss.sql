-- Run once in the Supabase SQL editor (same pattern as semantic dedup migration).
CREATE TABLE IF NOT EXISTS win_loss_memory (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    opportunity_id text NOT NULL,
    outcome text NOT NULL,
    client text,
    donor text,
    lessons jsonb,
    embedding vector(1536),
    recorded_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS win_loss_memory_embedding_idx
    ON win_loss_memory USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE OR REPLACE FUNCTION match_win_loss_memory(
    query_embedding vector(1536), match_threshold float, match_count int
)
RETURNS TABLE (opportunity_id text, outcome text, lessons jsonb, similarity float)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT win_loss_memory.opportunity_id, win_loss_memory.outcome, win_loss_memory.lessons,
           1 - (win_loss_memory.embedding <=> query_embedding) AS similarity
    FROM win_loss_memory
    WHERE win_loss_memory.embedding IS NOT NULL
        AND 1 - (win_loss_memory.embedding <=> query_embedding) > match_threshold
    ORDER BY similarity DESC LIMIT match_count;
END; $$;
