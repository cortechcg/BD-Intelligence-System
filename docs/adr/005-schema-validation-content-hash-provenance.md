# ADR 005 — Schema-validated extraction, content_hash uniqueness, field provenance (Phase 1)

## Status

Accepted

## Context

Phase 0 shipped a 36-item golden set and `parse_analysis_payload()` (ADR 004).
This ADR is Phase 1 of the S-Tier upgrade: extraction reliability after that
golden set. It does **not** start Phase 2 (organizations, win model, competitor
tables).

Scorecard / gap lines this ADR implements (quoted, not paraphrased):

- §19 AI Quality **6/10**: “Untrusted boundaries and deterministic score
  boundary; no schema/claim verifier.”
- §8: “extraction is JSON parsing after a single model call, not a fully
  schema-validated structured-output contract.”
- §19 Data Quality **6/10**: “Canonical URL, extraction checks, explicit
  unknowns; no canonical entity/freshness model.”
- §7: “`content_hash` is logged but not a database uniqueness field.”
- `docs/CURRENT_STATE.md`: “there is no … full provenance chain from source
  page to generated proposal claim.”
- §16: “Replace single-shot LLM JSON parsing with schema validation and
  repair/retry policy.” / “Store content hash, extraction/version/provenance,
  freshness, and lifecycle fields in deliberate database migrations.”
- §20 (after Phase 0): next is “a migration-backed opportunity/document/fact/
  provenance lifecycle” and a **live** extraction pass against the golden set.
  Phase 1 is the schema + `content_hash` uniqueness + *minimal* field-level
  provenance prerequisite. Live Claude vs golden labels is still unmeasured.

ADR 003 (provenance philosophy) remains in force: UNKNOWN is honest; do not
infer missing education/certs; scoring factors stay VERIFIED / INFERRED /
UNKNOWN; past-work claims stay a retrieved-chunk problem for
`intelligence/grounding.py`. This ADR does **not** replace ADR 003 with a claim
graph. It adds extraction-field → source-chunk pointers so a later Phase 4
claim verifier has somewhere real to look.

## Decision

1. **Pydantic contract for analyzer output**, derived from fields the pipeline
   already consumes (`bid_scorer.py`, `main.py`, `proposal_writer.py`,
   `cv_matcher.py`, `budget_calculator.py`, `compliance.py`, `tender_reader.py`).
   No new business fields. `parse_analysis_payload()` remains the single parse
   path (Phase 0); it validates after JSON parse rather than growing a second
   parser.
2. **Missing `is_consultancy_contract` defaults True** (CURSOR.md / ADR 002) as
   a schema default. A present non-boolean is a schema failure, not a silent
   True. Null `bid_analysis` is treated as a missing object (consultancy
   default), not as a pass-through of garbage types.
3. **Validation failure → retry once** via `complete()` with
   `CLAUDE_MODEL` from `config.py`. The repair user message is: the last
   response did not match the schema because X; fix and resend. Untrusted
   tender text stays in `wrap_untrusted()` on the original user turn. After a
   second failure, **fail explicitly** (`analyze_rfp` returns `{}`; the
   orchestrator skips). Do not emit a schema-wrong structured record.
4. **`content_hash` uniqueness in Supabase** on `opportunities_cache` (additive
   migration). Airtable is not the source of truth; no new Airtable fields.
   Exact-URL completion remains the workflow ledger
   (`opportunity_processing`). Same document body on a different URL is a
   content-identity hit, fail-open if the column is missing.
5. **Minimal field provenance**, computed from the source text after a valid
   parse — not LLM-invented page numbers. Status vocabulary aligns with ADR
   003: `VERIFIED` (value located in a source chunk), `UNKNOWN` (extracted but
   not found in the text), `INSUFFICIENT DATA` (no value or no source text).
   `page` is recorded only when a `----- PAGE N -----` marker is actually in
   the extracted text (PDF extractor writes these). Missing page → `null`,
   never a guessed number. This is **not** a source → page → chunk → proposal
   claim graph.

Schema fields (sourced from current readers, not invented):

- `opportunity`: title, client, donor, reference_number, submission_deadline,
  project_location, lots_or_sites, target_groups, project_duration,
  estimated_budget_usd, currency
- `requirements`: technical, thematic_areas, geographic_experience,
  language_requirements, certifications, methodology_requirements
- `team_requirements[]`: role, level, years_experience_minimum, required_skills,
  required_geographic_experience, required_education, estimated_days_of_effort,
  must_be_local, local_country
- `deliverables[]`: name, description, timeline
- `evaluation_criteria[]`: criterion, weight_percent, description
- `assignment_evaluation_framework[]`: criterion, description
- `submission_requirements`: technical_proposal_page_limit,
  prescribed_proposal_sections, cvs_required, past_work_samples_required,
  references_required, financial_proposal_required
- `bid_analysis`: is_consultancy_contract (default True), submission_type,
  cortech_fit_score, win_probability, bid_recommendation, effort_required,
  key_strengths, key_gaps, recommended_external_partners, rationale, priority

`extraction_provenance` is computed metadata alongside those fields, not an
LLM business field.

Pytest on 2026-09-14 (`./cortech/bin/python -m pytest tests/ -q`):

```text
2 failed, 237 passed in 14.66s
```

The two failures are pre-existing `tests/test_grounding.py` cases, not Phase 1.
Golden-set extraction vs Phase 0 baseline: consultancy P/R/acc 1.000 (29/7),
client/deadline exact 1.000, budget MAE 0.000, geography/thematic Jaccard 1.000.
Equal, not worse.

`supabase_migration_content_hash.sql` was **not** applied to a live project in
this session (no local Supabase; hosted credentials were not used).

## Consequences

- Golden-set offline accuracy must stay at or above the Phase 0 baseline
  (consultancy P/R/acc 1.000 on recorded JSON). Live Claude accuracy is still
  not claimed.
- AI Quality does not jump to 8: schema + retry is real; live extraction and
  proposal claim verification are not.
- Data Quality still has no canonical organization/freshness model. `content_hash`
  uniqueness is identity for document bodies, not an entity graph.
- `supabase_migration_content_hash.sql` must be applied in the Supabase SQL
  editor before uniqueness is enforced in a given project. This session does
  not apply it to production.
