# Architecture (as implemented)

This is the system after the P0/P1 pass. It is still a **single Python process** with Airtable + Supabase + Anthropic. There is no microservice mesh, no chatbot, no competitor ranking graph.

## Runtime

```
systemd timers (or python main.py --once)
        │
        ▼
   main.run_pipeline()          execution_id set here
        │
        ├─ RSS (httpx timeout + URL canonicalization)
        ├─ Playwright scraper (Somali Jobs)
        └─ IMAP Assortis newsletter
                │
                ▼
        process_opportunity()
           1. claim opportunity_processing lease
           2. load pipeline_stage + checkpoint (resume if present)
           3. extract  (discovered → extracted)
           4. score    (analyze + bid_scorer + consultancy/NO-BID gates)
           5. draft    (CV, budget fail-open, EOI/proposal; empty draft → retry/dead-letter)
           6. complete lease at drafted; email humans
           Human Airtable only: reviewed → outcome
           Crash mid-run: next claim resumes at the last persisted stage.
           7. store_opportunity after durable complete (canonical URL upsert)
```

Nothing in this pipeline submits to a client.

## Modules that are real

| Path | Role |
|---|---|
| `main.py` | Orchestrator (lease + stage runner) |
| `intelligence/pipeline_stages.py` | Extract / score / draft stage services + resume |
| `config.py` | Constants, `CLAUDE_MODEL` / `CLAUDE_MODEL_PROPOSAL`, env validation |
| `monitors/` | Discovery |
| `processors/downloader.py` | Fetch + extract |
| `processors/document_quality.py` | Empty/corrupt rejection |
| `intelligence/analyzer.py` | LLM structured extraction (Pydantic schema + one schema retry) |
| `intelligence/bid_scorer.py` | Deterministic FIT/WIN/STRATEGIC/RISK/EV |
| `intelligence/scoring_model.json` | Versioned weights |
| `intelligence/win_calibration.py` | Phase 6 P(win) harness; null unless labeled n meets the bar |
| `intelligence/win_calibration_artifact.json` | Census + thresholds; `fitted: false` (v0.1.0) |
| `intelligence/cv_matcher.py` | Semantic match + explicit geography/sector/language/years/skills/availability overlay |
| `intelligence/compliance.py` | Submission compliance matrix |
| `intelligence/proposal_writer.py` | Drafting |
| `intelligence/grounding.py` | Named past-work claim → retrieved chunk; `[NOT VERIFIED]` inline (ADR 008) |
| `intelligence/organizations.py` | Canonical client/donor matching + observed-record roll-up (no LLM) |
| `intelligence/market_trends.py` | Observed-data 30/90-day digest over stored opportunities (no LLM; thin-n refusal) |
| `intelligence/competitors.py` | Assortis labelled Awarded Firm(s) → cited award rows (no likely-bidder inference) |
| `intelligence/relationships.py` | Named JV/consortium edges from Cortech past submissions (excerpt-gated) |
| `database/` | Airtable CRM, Supabase vectors, fail-open org persistence + opportunity fact columns + cited award/relationship facts |
| `utils/errors.py` | Error taxonomy used at call sites |
| `utils/urls.py` | Canonicalization + SSRF guard |
| `utils/untrusted.py` | Document-as-data wrapping |
| `utils/observability.py` | execution_id, stage logs, ESTIMATED cost |

## What is stored where

- **Supabase `opportunities_cache`**: canonical `source_url`, title, raw text, title embedding, and unique `content_hash` of the extracted body (`supabase_migration_content_hash.sql` applied 2026-09-17; 1210 existing rows remain NULL until rewritten). Dedup = exact canonical URL + content-hash identity + (Assortis) title near-dup. After `supabase_migration_opportunity_facts.sql` (applied 2026-09-17), optional `thematic_areas` / `locations` / `donor` / `discovered_at` support the observed-data market digest. `discovered_at` on this project is `timestamptz` (pre-existing; DATE add skipped). Occupancy of the new fact columns is 0/1210. The Supabase client is created lazily at first storage use, after configuration validation at the entry point.
- **Supabase `opportunity_processing`**: lease/claim ledger (`pending|processing|failed|completed|dead_letter`) plus Phase 7 `pipeline_stage` (`discovered → extracted → scored → drafted → reviewed → outcome`) and a JSONB checkpoint. Additive migration `supabase_migration_opportunity_stages.sql` (**applied** 2026-09-15). If the table is missing, bulk discovery still refuses to treat URLs as new. If the table exists but stage columns are missing, the agent logs CRITICAL and still attempts the opportunity from `discovered` (does not drop BID/WATCH). When columns exist, resume is mandatory. `reviewed` / `outcome` are human/Airtable transitions, not autonomous submit.
- **Supabase `organizations` / `organization_aliases` / `organization_observations`**: canonical client/donor entities and cited involvement. Matching is normalize+exact/fuzzy (ADR 006). `supabase_migration_organizations.sql` **applied** 2026-09-17; production occupancy 0. Airtable was not given new fields.
- **Supabase `award_observations` / `relationship_edges`**: Phase 5 cited Assortis award-firm rows and cited Cortech-submission relationship edges (ADR 009). `supabase_migration_award_relationships.sql` **applied** 2026-09-17 (FK to `organizations`); occupancy 0/0. Identity reuses `organizations`. Airtable was not given new fields.
- **Airtable OPPORTUNITIES**: human CRM. `relevance_score` / `win_probability` / `bid_recommendation` now hold **code** scores. Full factor breakdown lives inside `claude_analysis` JSON (`bid_intelligence`). No new Airtable fields were added (see `check_schema.py`).
- **Airtable AGENT_LOGS**: optional; circuit-breaker skip on 429. `cost_usd` only when a price row exists for the model. If 429s persist, bulk-delete AGENT_LOGS in the Airtable UI (or `python populate_airtable.py --prune-logs`); do not invent a second log system.

## Scoring data flow

LLM extracts locations, themes, languages, certs, client, deadline, budget, qualitative strengths/gaps.

`bid_scorer.compute_bid_intelligence()` turns those fields + optional CV coverage into dimension scores using `scoring_model.json`. The LLM's own `cortech_fit_score` is kept as `llm_cortech_fit_score` for audit and is not the gate. `calibrated_win_probability` is a separate audit field from `intelligence/win_calibration.py`; it is null / INSUFFICIENT DATA on current labeled n (123 WON / 0 LOST) and does not replace heuristic WIN PROBABILITY.

## Financial preparation

`budget_calculator.calculate_budget()` no longer calls an LLM or applies default
rates/percentages. It costs a role only when both `estimated_days_of_effort`
was explicitly extracted from the ToR and an exact `role_level` + location rate
exists in Airtable `RATE_CARDS`. It reports a verified personnel subtotal when
possible, but keeps `grand_total_usd` `null` until evidence for travel, tools,
workshops, overhead, contingency, and taxes is provided. The review email shows
the missing-input list instead of a made-up zero-dollar budget.

## Periodic jobs

`cortech-discovery`, `cortech-assortis`, `cortech-deadline`, `cortech-winloss`,
and `cortech-market` (weekly observed-data digest, `main.py --run-market-digest`).
The digest aggregates stored rows; it is not external market research.

## Not in this architecture

Competitor intelligence as a ranking/likely-bidder product, external market-research products, a full relationship graph / account CRM, executive-brief products, knowledge-graph services, a UI, or extra LLM agent loops. Those were out of scope and are not stubbed. Phase 5 stores only Assortis Awarded Firm(s) citations and named JV/consortium edges from `data/proposals/` — not inferred winners from silence. The Phase 3 digest is observed stored opportunities only. Phase 4 is named past-work claim grounding only — not a full source→proposal sentence graph. Phase 6 is a calibration *harness* that currently returns INSUFFICIENT DATA — not a trusted P(win) model. Phase 7 adds durable `pipeline_stage` resume on the existing `opportunity_processing` ledger (ADR 011). Phase 8 (ADR 012) adds DNS-pinned downloads, Playwright timeout/heap flags (not an OS sandbox), CI `pip-audit`, and an enforced per-run spend cap.
