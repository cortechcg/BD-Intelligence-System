-- Additive: canonical client/donor organizations and observed involvement.
-- Airtable is not the source of truth. Do not invent Airtable fields.
--
-- Apply once in the Supabase SQL editor. This file does not run itself.
-- Python fail-opens (empty index, no crash) if these relations are missing,
-- same pattern as supabase_migration_content_hash.sql.

CREATE TABLE IF NOT EXISTS organizations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    normalized_compact TEXT NOT NULL DEFAULT '',
    entity_kind TEXT NOT NULL DEFAULT 'unknown'
        CHECK (entity_kind IN ('client', 'donor', 'both', 'unknown')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS organizations_normalized_name_uidx
    ON organizations (normalized_name);

-- Compact identity only when the compact form is long enough to be
-- distinguishing. Short strings (e.g. "un") must not collide globally.
CREATE UNIQUE INDEX IF NOT EXISTS organizations_normalized_compact_uidx
    ON organizations (normalized_compact)
    WHERE length(normalized_compact) >= 6;

CREATE TABLE IF NOT EXISTS organization_aliases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    alias_raw TEXT NOT NULL,
    alias_normalized TEXT NOT NULL,
    alias_compact TEXT NOT NULL DEFAULT '',
    alias_source TEXT NOT NULL DEFAULT 'observed'
        CHECK (alias_source IN ('observed', 'explicit')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS organization_aliases_normalized_uidx
    ON organization_aliases (alias_normalized);

CREATE UNIQUE INDEX IF NOT EXISTS organization_aliases_compact_uidx
    ON organization_aliases (alias_compact)
    WHERE length(alias_compact) >= 6;

CREATE INDEX IF NOT EXISTS organization_aliases_org_idx
    ON organization_aliases (organization_id);

CREATE TABLE IF NOT EXISTS organization_observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('client', 'donor')),
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('opportunity', 'past_proposal', 'win_loss')),
    source_id TEXT NOT NULL,
    title TEXT,
    outcome TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (outcome IN ('WON', 'LOST', 'UNKNOWN')),
    outcome_status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (outcome_status IN ('VERIFIED', 'INFERRED', 'UNKNOWN')),
    observed_name TEXT NOT NULL,
    match_method TEXT NOT NULL,
    match_confidence NUMERIC NOT NULL DEFAULT 0,
    match_status TEXT NOT NULL
        CHECK (match_status IN ('VERIFIED', 'INFERRED', 'UNKNOWN')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS organization_observations_identity_uidx
    ON organization_observations (organization_id, source_kind, source_id, role);

CREATE INDEX IF NOT EXISTS organization_observations_org_idx
    ON organization_observations (organization_id);

COMMENT ON TABLE organizations IS
    'Canonical client/donor entities. Matching is normalize+exact/fuzzy; below-threshold names become new candidate rows, never force-merged.';
COMMENT ON TABLE organization_aliases IS
    'Observed spellings and optional human-checked explicit aliases. Empty explicit list in code until an operator adds pairs.';
COMMENT ON TABLE organization_observations IS
    'Involvement of an org on a stored opportunity, past proposal, or win/loss row. Outcomes stay UNKNOWN unless actually stored as Won/Lost.';
