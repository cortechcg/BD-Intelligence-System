-- Additive: cited Assortis award-winner observations and cited
-- relationship edges from Cortech past submissions (Phase 5 / ADR 009).
-- Reuses organizations for identity. Do not invent Airtable fields.
--
-- Apply once in the Supabase SQL editor AFTER
-- supabase_migration_organizations.sql. This file does not run itself.
-- Python fail-opens (empty lists, no crash) if these relations are missing.

CREATE TABLE IF NOT EXISTS award_observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid REFERENCES organizations(id) ON DELETE SET NULL,
    observed_name TEXT NOT NULL,
    match_method TEXT NOT NULL,
    match_confidence NUMERIC NOT NULL DEFAULT 0,
    match_status TEXT NOT NULL
        CHECK (match_status IN ('VERIFIED', 'INFERRED', 'UNKNOWN')),
    evidence_status TEXT NOT NULL DEFAULT 'VERIFIED'
        CHECK (evidence_status = 'VERIFIED'),
    opportunity_title TEXT,
    source_url TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'award_notice'
        CHECK (source_kind IN ('award_notice')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS award_observations_url_name_uidx
    ON award_observations (source_url, observed_name);

CREATE INDEX IF NOT EXISTS award_observations_org_idx
    ON award_observations (organization_id);

CREATE TABLE IF NOT EXISTS relationship_edges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid REFERENCES organizations(id) ON DELETE SET NULL,
    observed_name TEXT NOT NULL,
    relationship_kind TEXT NOT NULL
        CHECK (relationship_kind IN (
            'joint_venture', 'consortium', 'subcontractor', 'in_association'
        )),
    cortech_role TEXT,
    document_name TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    match_method TEXT NOT NULL,
    match_confidence NUMERIC NOT NULL DEFAULT 0,
    match_status TEXT NOT NULL
        CHECK (match_status IN ('VERIFIED', 'INFERRED', 'UNKNOWN')),
    evidence_status TEXT NOT NULL DEFAULT 'VERIFIED'
        CHECK (evidence_status = 'VERIFIED'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS relationship_edges_identity_uidx
    ON relationship_edges (document_name, observed_name, relationship_kind);

CREATE INDEX IF NOT EXISTS relationship_edges_org_idx
    ON relationship_edges (organization_id);

COMMENT ON TABLE award_observations IS
    'VERIFIED Assortis Awarded Firm(s) only. Requires source_url + excerpt containing the name. Silence (no winner field) stores nothing.';
COMMENT ON TABLE relationship_edges IS
    'VERIFIED JV/consortium/subcontract/in-association edges from Cortech past submissions. Requires document + chunk + excerpt containing the named counterpart. Typical-partner language is not stored.';
