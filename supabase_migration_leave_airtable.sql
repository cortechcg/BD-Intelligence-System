-- Replace the Airtable CRM with Supabase. Does not call Airtable.
-- Apply after the opportunity_processing stage migrations.
-- Idempotent: safe to run more than once.

CREATE TABLE IF NOT EXISTS consultants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name_key TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    role_title TEXT NOT NULL DEFAULT '',
    years_experience NUMERIC,
    education TEXT NOT NULL DEFAULT '',
    seniority_level TEXT NOT NULL DEFAULT '',
    based_in TEXT NOT NULL DEFAULT '',
    day_rate_usd NUMERIC,
    thematic_expertise JSONB NOT NULL DEFAULT '[]'::jsonb,
    geographic_experience JSONB NOT NULL DEFAULT '[]'::jsonb,
    languages JSONB NOT NULL DEFAULT '[]'::jsonb,
    tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    key_skills TEXT NOT NULL DEFAULT '',
    key_assignments TEXT NOT NULL DEFAULT '',
    availability_status TEXT,
    availability_percentage NUMERIC,
    cv_text TEXT NOT NULL DEFAULT '',
    cv_last_updated DATE,
    legacy_ids TEXT[] NOT NULL DEFAULT '{}',
    embedding_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS consultants_name_key_uidx
    ON consultants (name_key)
    WHERE name_key <> '';

CREATE TABLE IF NOT EXISTS rate_cards (
    role_level TEXT NOT NULL,
    location TEXT NOT NULL,
    day_rate_usd NUMERIC NOT NULL,
    per_diem_usd NUMERIC,
    last_updated DATE,
    PRIMARY KEY (role_level, location)
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    log_id TEXT NOT NULL,
    logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action_type TEXT NOT NULL DEFAULT '',
    opportunity_id TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    tokens_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    cost_usd NUMERIC
);

ALTER TABLE opportunity_processing
    ADD COLUMN IF NOT EXISTS crm_status TEXT;

ALTER TABLE opportunity_processing
    ADD COLUMN IF NOT EXISTS submission_deadline DATE;

ALTER TABLE opportunity_processing
    ADD COLUMN IF NOT EXISTS crm_fields JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE opportunity_processing
    DROP CONSTRAINT IF EXISTS opportunity_processing_crm_status_check;

ALTER TABLE opportunity_processing
    ADD CONSTRAINT opportunity_processing_crm_status_check
    CHECK (
        crm_status IS NULL
        OR crm_status IN ('New', 'Reviewing', 'Bidding', 'Won', 'Lost', 'No-bid')
    );

-- One person per normalized name. Embedding rows stay; their Airtable ids
-- are kept on legacy_ids so CV match can still find them.
INSERT INTO consultants (
    name_key, full_name, role_title, years_experience, seniority_level,
    based_in, day_rate_usd, thematic_expertise, geographic_experience,
    languages, tools, key_skills, availability_status, cv_text, legacy_ids
)
SELECT
    grouped.name_key,
    grouped.full_name,
    COALESCE(grouped.role_title, ''),
    grouped.years_experience,
    COALESCE(grouped.seniority_level, ''),
    COALESCE(grouped.based_in, ''),
    grouped.day_rate_usd,
    COALESCE(grouped.thematic_expertise, '[]'::jsonb),
    COALESCE(grouped.geographic_experience, '[]'::jsonb),
    COALESCE(grouped.languages, '[]'::jsonb),
    COALESCE(grouped.tools, '[]'::jsonb),
    COALESCE(grouped.key_skills, ''),
    grouped.availability_status,
    COALESCE(grouped.cv_text, ''),
    grouped.legacy_ids
FROM (
    SELECT
        lower(regexp_replace(coalesce(consultant_name, ''), '[^a-zA-Z0-9]+', ' ', 'g')) AS name_key,
        (array_agg(consultant_name ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS full_name,
        (array_agg(role_title ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS role_title,
        (array_agg(
            CASE
                WHEN coalesce(metadata->>'years_experience', '') ~ '^[0-9]+(\.[0-9]+)?$'
                THEN (metadata->>'years_experience')::numeric
            END
            ORDER BY length(coalesce(content_chunk, '')) DESC
        ))[1] AS years_experience,
        (array_agg(metadata->>'seniority_level' ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS seniority_level,
        (array_agg(metadata->>'based_in' ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS based_in,
        (array_agg(
            CASE
                WHEN coalesce(metadata->>'day_rate_usd', '') ~ '^[0-9]+(\.[0-9]+)?$'
                THEN (metadata->>'day_rate_usd')::numeric
            END
            ORDER BY length(coalesce(content_chunk, '')) DESC
        ))[1] AS day_rate_usd,
        (array_agg(metadata->'thematic_expertise' ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS thematic_expertise,
        (array_agg(metadata->'geographic_experience' ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS geographic_experience,
        (array_agg(metadata->'languages' ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS languages,
        (array_agg(metadata->'tools' ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS tools,
        (array_agg(metadata->>'key_skills' ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS key_skills,
        (array_agg(metadata->>'availability_status' ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS availability_status,
        (array_agg(content_chunk ORDER BY length(coalesce(content_chunk, '')) DESC))[1] AS cv_text,
        COALESCE(
        array_agg(DISTINCT airtable_consultant_id)
            FILTER (WHERE airtable_consultant_id IS NOT NULL AND airtable_consultant_id <> ''),
        '{}'::text[]
    ) AS legacy_ids
    FROM cv_embeddings
    GROUP BY lower(regexp_replace(coalesce(consultant_name, ''), '[^a-zA-Z0-9]+', ' ', 'g'))
) AS grouped
WHERE btrim(grouped.name_key) <> ''
ON CONFLICT (name_key) WHERE name_key <> '' DO NOTHING;

INSERT INTO rate_cards (role_level, location, day_rate_usd, per_diem_usd, last_updated) VALUES
    ('Team Leader', 'Nairobi', 2700, 150, CURRENT_DATE),
    ('Team Leader', 'Addis Ababa', 2500, 180, CURRENT_DATE),
    ('Team Leader', 'Mogadishu', 3000, 350, CURRENT_DATE),
    ('Team Leader', 'London', 3500, 300, CURRENT_DATE),
    ('Team Leader', 'Remote', 2200, 0, CURRENT_DATE),
    ('Senior Specialist', 'Nairobi', 1900, 150, CURRENT_DATE),
    ('Senior Specialist', 'Addis Ababa', 1800, 180, CURRENT_DATE),
    ('Senior Specialist', 'Mogadishu', 2200, 350, CURRENT_DATE),
    ('Senior Specialist', 'London', 2500, 300, CURRENT_DATE),
    ('Senior Specialist', 'Remote', 1600, 0, CURRENT_DATE),
    ('Mid Specialist', 'Nairobi', 1150, 150, CURRENT_DATE),
    ('Mid Specialist', 'Addis Ababa', 1100, 180, CURRENT_DATE),
    ('Mid Specialist', 'Mogadishu', 1400, 350, CURRENT_DATE),
    ('Mid Specialist', 'London', 1600, 300, CURRENT_DATE),
    ('Mid Specialist', 'Remote', 950, 0, CURRENT_DATE),
    ('Junior Researcher', 'Nairobi', 500, 100, CURRENT_DATE),
    ('Junior Researcher', 'Addis Ababa', 450, 120, CURRENT_DATE),
    ('Junior Researcher', 'Mogadishu', 650, 200, CURRENT_DATE),
    ('Junior Researcher', 'Remote', 400, 0, CURRENT_DATE),
    ('Field Researcher', 'Nairobi', 385, 80, CURRENT_DATE),
    ('Field Researcher', 'Addis Ababa', 350, 80, CURRENT_DATE),
    ('Field Researcher', 'Mogadishu', 500, 150, CURRENT_DATE),
    ('Translator', 'Nairobi', 155, 60, CURRENT_DATE),
    ('Translator', 'Addis Ababa', 140, 60, CURRENT_DATE),
    ('Translator', 'Mogadishu', 200, 100, CURRENT_DATE)
ON CONFLICT (role_level, location) DO NOTHING;
