# Architecture (as implemented)

This is the system after the P0/P1 pass. It is still a **single Python process** with Airtable + Supabase + Anthropic. There is no microservice mesh, no chatbot, no competitor graph.

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
           1. fetch_and_extract (SSRF guard, quality gate)
           2. store_opportunity (canonical URL upsert)
           3. analyze_rfp (untrusted wrap + get_text)
           4. apply_bid_intelligence()   ← deterministic scores
           5. is_consultancy_contract (default True)
           6. NO-BID draft gate uses CODE recommendation, not the LLM number;
              record remains New for human confirmation
           7. Airtable create (existing fields; scores are the code scores)
           8. CV match + geography/sector/language/availability overlay
           9. apply_bid_intelligence() again with team coverage
          10. evidence-bound personnel costing / EOI or full proposal
          11. compliance_matrix (SATISFIED/PARTIAL/MISSING/UNKNOWN)
          12. email humans
```

Nothing in this pipeline submits to a client.

## Modules that are real

| Path | Role |
|---|---|
| `main.py` | Orchestrator |
| `config.py` | Constants, `CLAUDE_MODEL` / `CLAUDE_MODEL_PROPOSAL`, env validation |
| `monitors/` | Discovery |
| `processors/downloader.py` | Fetch + extract |
| `processors/document_quality.py` | Empty/corrupt rejection |
| `intelligence/analyzer.py` | LLM structured extraction (Pydantic schema + one schema retry) |
| `intelligence/bid_scorer.py` | Deterministic FIT/WIN/STRATEGIC/RISK/EV |
| `intelligence/scoring_model.json` | Versioned weights |
| `intelligence/cv_matcher.py` | Semantic match + explicit geography/sector/language/years/skills/availability overlay |
| `intelligence/compliance.py` | Submission compliance matrix |
| `intelligence/proposal_writer.py` | Drafting |
| `intelligence/organizations.py` | Canonical client/donor matching + observed-record roll-up (no LLM) |
| `intelligence/market_trends.py` | Observed-data 30/90-day digest over stored opportunities (no LLM; thin-n refusal) |
| `database/` | Airtable CRM, Supabase vectors, fail-open org persistence + opportunity fact columns |
| `utils/errors.py` | Error taxonomy used at call sites |
| `utils/urls.py` | Canonicalization + SSRF guard |
| `utils/untrusted.py` | Document-as-data wrapping |
| `utils/observability.py` | execution_id, stage logs, ESTIMATED cost |

## What is stored where

- **Supabase `opportunities_cache`**: canonical `source_url`, title, raw text, title embedding, and (after `supabase_migration_content_hash.sql`) unique `content_hash` of the extracted body. Dedup = exact canonical URL + content-hash identity + (Assortis) title near-dup. The column is fail-open if the migration is not applied. After `supabase_migration_opportunity_facts.sql`, optional `thematic_areas` / `locations` / `donor` / `discovered_at` support the observed-data market digest; missing columns fail-open. The Supabase client is created lazily at first storage use, after configuration validation at the entry point.
- **Supabase `organizations` / `organization_aliases` / `organization_observations`**: canonical client/donor entities and cited involvement. Matching is normalize+exact/fuzzy (ADR 006). Fail-open if `supabase_migration_organizations.sql` is not applied. Airtable was not given new fields.
- **Airtable OPPORTUNITIES**: human CRM. `relevance_score` / `win_probability` / `bid_recommendation` now hold **code** scores. Full factor breakdown lives inside `claude_analysis` JSON (`bid_intelligence`). No new Airtable fields were added (see `check_schema.py`).
- **Airtable AGENT_LOGS**: optional; circuit-breaker skip on 429. `cost_usd` only when a price row exists for the model. If 429s persist, bulk-delete AGENT_LOGS in the Airtable UI (or `python populate_airtable.py --prune-logs`); do not invent a second log system.

## Scoring data flow

LLM extracts locations, themes, languages, certs, client, deadline, budget, qualitative strengths/gaps.

`bid_scorer.compute_bid_intelligence()` turns those fields + optional CV coverage into dimension scores using `scoring_model.json`. The LLM's own `cortech_fit_score` is kept as `llm_cortech_fit_score` for audit and is not the gate.

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

Competitor intelligence, external market-research products, relationship graphs, executive-brief products, knowledge-graph services, a UI, or extra LLM agent loops. Those were out of scope and are not stubbed. The Phase 3 digest is observed stored opportunities only.
