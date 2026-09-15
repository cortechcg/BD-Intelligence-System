# Current state

Updated: 2026-09-15. This is an implementation inventory, not a roadmap.
`FINAL_SYSTEM_AUDIT.md` contains the detailed evidence and scorecard.
Phase 0 golden-set ADR: `docs/adr/004-golden-evaluation-set.md`.
Phase 1 schema / content_hash / field-provenance ADR:
`docs/adr/005-schema-validation-content-hash-provenance.md`.
Phase 2 client/org ADR: `docs/adr/006-client-organizations.md`.
Phase 3 observed-data market digest ADR: `docs/adr/007-observed-market-digest.md`.

## What is operational

| Capability | Implementation | State |
|---|---|---|
| Discovery | RSS support (currently no configured feeds), Somali Jobs Playwright scraper, Assortis/ICA IMAP parser | Functional local sources; coverage is limited. |
| Fetch and extraction | HTML, PDF, DOCX, Google Drive packs, browser/PDF-link fallbacks | Quality gate rejects empty/corrupt/access-wall output. PDF text includes `----- PAGE N -----` markers from pdfplumber page index. |
| Download security | Public HTTP URL policy, per-hop redirect validation, TLS verification, bounded bytes/redirects/retries | DNS rebinding and browser/parser sandboxing remain open. |
| Deduplication | Canonical exact URL, `opportunity_processing` lease/claim ledger, and `opportunities_cache.content_hash` unique index | Migration file `supabase_migration_content_hash.sql` exists; **not applied** to a live project in this session. Client fail-opens (omits the column / skips lookup) if the column is missing. Same body on a different URL is terminal-skip unless `--submit-url` force. |
| Extraction | Claude JSON extraction with untrusted-document boundary, Pydantic schema on `parse_analysis_payload()`, one schema-repair retry, then explicit refuse | Missing `is_consultancy_contract` still defaults True. Schema-wrong types are not fail-opened into a structured record. Live Claude vs golden labels is still unmeasured. |
| Field provenance | `extraction_provenance` map: extracted field → source file / chunk / page-if-marked | VERIFIED only when the value is found in the tender text. Missing source or empty value → INSUFFICIENT DATA. Unlocated value → UNKNOWN. Page is never invented. Not a proposal claim graph (ADR 003 still owns past-work grounding). |
| Golden evaluation | `tests/golden/opportunities.json` (36 anonymized items) + offline parse/scorer harness | **Baseline (offline only, 2026-09-15, after Phase 3):** consultancy P/R/acc = 1.000 on recorded JSON through `parse_analysis_payload` (29 true / 7 false). Client/deadline exact = 1.000. Budget MAE = 0.000 on 1 labeled numeric ToR; null agreement = 1.000. Geography/thematic Jaccard = 1.000. Equal to Phase 0, 1, and 2 — no extraction regression. Scorer recommendation accuracy = 1.000 on 29 items with an expected band (7 vacancies skipped). EV INSUFFICIENT DATA rate = 1.000. **Not measured:** live Claude extraction, retrieval, or Won/Lost calibration (all outcomes UNKNOWN). |
| Bid intelligence | `scoring_model.json` + deterministic `bid_scorer.py` | FIT/WIN heuristic/strategic/risk/EV separated; LLM numbers are audit-only. |
| Capability | Semantic CV retrieval plus explicit geography/sector/language/years/skills overlay and live Airtable availability | Missing CV evidence and missing availability stay UNKNOWN — not inferred as a match or as 100% free. |
| Financial preparation | Explicit ToR effort × exact Airtable rate-card row only | Never a complete financial proposal without non-personnel cost evidence. |
| Proposal | Tender-aware drafting, style guides, past-proposal retrieval, claim-to-chunk grounding, review email/docx | Named past-work claims must match a retrieved chunk or they are tagged [NOT VERIFIED]. |
| Compliance | CV/financial/attachments/award-criterion status matrix | Award criteria are not programmatically proven satisfied. |
| Human control | Review email and Airtable record; no external submission | NO-BID is a recommendation on a `New` record, not final automation. |
| Client intelligence | `organizations` / aliases / observations in Supabase; normalize+exact/fuzzy matcher; cited roll-up in the review email | **Phase 2.** Matching is exact spaced/compact (VERIFIED) or fuzzy ≥ 0.95 with guards (INFERRED). Below threshold → new candidate / UNKNOWN, never a silent merge. Roll-up counts are code aggregations of stored rows (past opportunities, past proposals, win/loss memory, plus `known_client` name overlap from `bid_scorer.py`). Missing outcomes stay UNKNOWN — not zeros dressed as a win rate. Golden-set outcomes remain UNKNOWN. Migration `supabase_migration_organizations.sql` exists; **not applied** this session. Client fail-opens if tables are missing. No new Airtable fields (`check_schema.py` would reject an invented org-id column). Explicit alias file is empty — no UNICEF↔full-name merge by guess. |
| Market intelligence | Trailing 30/90-day frequencies of stored thematic labels, geography labels, and donor posting counts; weekly internal email | **Phase 3.** Code aggregation only (no Claude market narrative). `TREND_MIN_N = 10` dated stored rows in a window before any trend/percentage/cadence claim; n=2 renders “insufficient data for a trend”. Sample size and date range are inline. Labels are stored strings — no invented “Other WASH” / “East Africa” buckets. Donor cadence matches `opportunity.donor` against the Phase 2 organizations table only; empty/unapplied table → insufficient, donors not invented. Dates are `discovered_at` or cache `created_at`; never `submission_deadline`. Prefer `opportunities_cache` (optional fact columns in `supabase_migration_opportunity_facts.sql`, **not applied** this session); Airtable OPPORTUNITIES is secondary using existing fields. Fifth timer: `cortech-market.timer` → `main.py --run-market-digest`. SMTP mocked in tests; no live digest sent this session. |
| Outcome learning | Won/Lost lesson extraction and vector storage | Lessons are not yet model features. |

## Observed control points

- The CLI validates required Anthropic/OpenAI/Supabase configuration before a
  normal run. Supabase client creation is deferred until it is actually used.
- Airtable remains fail-open with a short rate-limit circuit breaker; a failed
  CRM write does not suppress the draft path.
- Every detailed bid recommendation carries a scoring-model version, factors,
  evidence, confidence, and LLM audit values in analysis data.
- Review-email dynamic content is HTML escaped and its subject cannot include
  CR/LF header injection.
- Person-level capability results carry MATCH SCORE, WHY, EVIDENCE, and GAPS.
  Busy consultants are kept and labelled; education is never inferred.

## Known material gaps

This is not yet a competitor, relationship, account-management, or
executive intelligence product. Canonical client/donor matching and cited
roll-up exist (Phase 2). An observed-data market digest exists (Phase 3)
with thin-n refusal; live history is only as complete as unapplied
`opportunity_facts` / `organizations` migrations and dated stored rows, so
windows will often be INSUFFICIENT DATA. There is still no competitor table
or calibrated win-probability model. Field-level ToR provenance exists;
there is still no full provenance chain from source page to **generated
proposal claim**. A small golden set exists for boolean-gate and scorer
regression; live LLM extraction accuracy against that set has not been run.
Phase 4 (proposal claim verification) was not started.

## Verification

```text
./cortech/bin/python -m pytest tests/ -q
2 failed, 278 passed in 14.62s
```

The two failures are pre-existing `tests/test_grounding.py` cases (claim
`chunk_id` / `[NOT VERIFIED]` vs `[INSUFFICIENT EVIDENCE]`). They were not
introduced by Phase 3 and were not changed here. Golden extraction tests still
pass at the Phase 0/1/2 baseline (consultancy P/R/acc 1.000). There has been no
live `--once`, `--submit-url`, `--run-assortis`, or `--run-market-digest`
execution as part of this update (digest tests mock SMTP). The `content_hash`,
`organizations`, and `opportunity_facts` migrations were not applied.
