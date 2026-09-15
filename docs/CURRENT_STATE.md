# Current state

Updated: 2026-09-15. This is an implementation inventory, not a roadmap.
`FINAL_SYSTEM_AUDIT.md` contains the detailed evidence and scorecard.
Phase 0 golden-set ADR: `docs/adr/004-golden-evaluation-set.md`.
Phase 1 schema / content_hash / field-provenance ADR:
`docs/adr/005-schema-validation-content-hash-provenance.md`.
Phase 2 client/org ADR: `docs/adr/006-client-organizations.md`.
Phase 3 observed-data market digest ADR: `docs/adr/007-observed-market-digest.md`.
Phase 4 named past-work claim grounding ADR: `docs/adr/008-proposal-claim-grounding.md`.
Phase 5 cited competitor/relationship facts ADR: `docs/adr/009-competitor-relationship-intelligence.md`.
Phase 6 calibrated-win harness ADR: `docs/adr/010-calibrated-win-probability.md`.
Phase 7 durable pipeline stages ADR: `docs/adr/011-durable-pipeline-stages.md`.
Phase 8 DNS-pin / Playwright limits / CI audit / spend cap ADR:
`docs/adr/012-dns-pin-spend-cap.md`.

## What is operational

| Capability | Implementation | State |
|---|---|---|
| Discovery | RSS support (currently no configured feeds), Somali Jobs Playwright scraper, Assortis/ICA IMAP parser | Functional local sources; coverage is limited. |
| Fetch and extraction | HTML, PDF, DOCX, Google Drive packs, browser/PDF-link fallbacks | Quality gate rejects empty/corrupt/access-wall output. PDF text includes `----- PAGE N -----` markers from pdfplumber page index. |
| Download security | Public HTTP URL policy, per-hop redirect validation, TLS verification, bounded bytes/redirects/retries, DNS-pinned GET on the download path (`utils/dns_pinned_http.py`: resolve once, connect to that IP, reject private/mixed answers) | Playwright still uses browser DNS plus the request guard (not a pin-IP transport). Parser isolation is Chromium flags/timeouts, not an OS sandbox. |
| Deduplication | Canonical exact URL, `opportunity_processing` lease/claim ledger with Phase 7 `pipeline_stage` + checkpoint resume, and `opportunities_cache.content_hash` unique index | Migration files `supabase_migration_content_hash.sql` and `supabase_migration_opportunity_stages.sql` exist; **not applied** to a live project in this session. Client fail-opens (omits the hash column / skips lookup) if `content_hash` is missing. Same body on a different URL is terminal-skip unless `--submit-url` force. Stage columns missing: log CRITICAL, still attempt from `discovered` (do not drop BID/WATCH). When stage columns exist, crash mid-run resumes at the last persisted stage. |
| Pipeline reliability | `intelligence/pipeline_stages.py` + `opportunity_processing.pipeline_stage` | **Phase 7.** Stages: discovered → extracted → scored → drafted (agent); reviewed → outcome (human/Airtable, not autonomous submit). Kill-mid-run after `scored` resumes at draft (fetch/analyze not repeated). BID/WATCH empty drafts are retryable `failed` then `dead_letter` after 3 failures — not a silent skip. Consultancy FALSE still stops before CV/proposal tokens. Optional intel still fail-open. **Phase 8:** `MAX_RUN_COST_USD` (default 25; `$0` blocks the first `complete()`). Cap halt is retryable `failed` at the last persisted stage; it does not increment draft-fail / dead-letter. |
| Extraction | Claude JSON extraction with untrusted-document boundary, Pydantic schema on `parse_analysis_payload()`, one schema-repair retry, then explicit refuse | Missing `is_consultancy_contract` still defaults True. Schema-wrong types are not fail-opened into a structured record. Live Claude vs golden labels is still unmeasured. |
| Field provenance | `extraction_provenance` map: extracted field → source file / chunk / page-if-marked | VERIFIED only when the value is found in the tender text. Missing source or empty value → INSUFFICIENT DATA. Unlocated value → UNKNOWN. Page is never invented. Not a proposal claim graph (ADR 003 / ADR 008 own named past-work grounding). |
| Golden evaluation | `tests/golden/opportunities.json` (36 anonymized items) + offline parse/scorer harness | **Baseline (offline only, 2026-09-15, after Phase 7):** consultancy P/R/acc = 1.000 on recorded JSON through `parse_analysis_payload` (29 true / 7 false). Client/deadline exact = 1.000. Budget MAE = 0.000 on 1 labeled numeric ToR; null agreement = 1.000. Geography/thematic Jaccard = 1.000. Equal to Phase 0, 1, 2, 3, 4, 5, and 6 — no extraction regression. Scorer recommendation accuracy = 1.000 on 29 items with an expected band (7 vacancies skipped). EV INSUFFICIENT DATA rate = 1.000. **Not measured:** live Claude extraction, retrieval, or trusted P(win) calibration (golden outcomes UNKNOWN; live LOST labels = 0). |
| Bid intelligence | `scoring_model.json` + deterministic `bid_scorer.py` | FIT/WIN heuristic/strategic/risk/EV separated; LLM numbers are audit-only. **Phase 6:** `calibrated_win_probability` audit field is null / INSUFFICIENT DATA. Census 2026-09-15: **123 WON / 0 LOST** (golden 0/0/36; `won=False` is UNKNOWN; `win_loss_memory` table missing). Threshold is n_labeled≥30 **and** ≥10 per class. No model was fit. Heuristic WIN PROBABILITY remains official. |
| Capability | Semantic CV retrieval plus explicit geography/sector/language/years/skills overlay and live Airtable availability | Missing CV evidence and missing availability stay UNKNOWN — not inferred as a match or as 100% free. |
| Financial preparation | Explicit ToR effort × exact Airtable rate-card row only | Never a complete financial proposal without non-personnel cost evidence. |
| Proposal | Tender-aware drafting, style guides, past-proposal retrieval, named-claim grounding, review email/docx | **Phase 4.** Every named past-work claim is matched to a retrieved `proposal_embeddings` / static `CORTECH_PAST_WORK` chunk. Named client in a chunk → VERIFIED with that `chunk_id` (not `profile:cortech`). Invented name → `[NOT VERIFIED]` left in the draft. Generic boast without an entity → `INSUFFICIENT EVIDENCE` in the report only. `CORTECH_PROFILE` KEY CLIENTS is not assignment evidence. Empty retrieval fail-opens; claims without chunks are still NOT VERIFIED. Not a full source→proposal graph for every sentence. **Phase 7:** empty BID/WATCH drafts raise `DraftingError` and go retryable `failed` (stage stays `scored`); after 3 drafting failures → `dead_letter`. Not a silent skip. |
| Compliance | CV/financial/attachments/award-criterion status matrix | Award criteria are not programmatically proven satisfied. |
| Human control | Review email and Airtable record; no external submission | NO-BID is a recommendation on a `New` record, not final automation. Ledger `reviewed` / `outcome` are written only from human Airtable statuses (win/loss poll records `outcome`). Nothing submits to clients. |
| Client intelligence | `organizations` / aliases / observations in Supabase; normalize+exact/fuzzy matcher; cited roll-up in the review email | **Phase 2.** Matching is exact spaced/compact (VERIFIED) or fuzzy ≥ 0.95 with guards (INFERRED). Below threshold → new candidate / UNKNOWN, never a silent merge. Roll-up counts are code aggregations of stored rows (past opportunities, past proposals, win/loss memory, plus `known_client` name overlap from `bid_scorer.py`). Missing outcomes stay UNKNOWN — not zeros dressed as a win rate. Golden-set outcomes remain UNKNOWN. Migration `supabase_migration_organizations.sql` exists; **not applied** this session. Client fail-opens if tables are missing. No new Airtable fields (`check_schema.py` would reject an invented org-id column). Explicit alias file is empty — no UNICEF↔full-name merge by guess. |
| Market intelligence | Trailing 30/90-day frequencies of stored thematic labels, geography labels, and donor posting counts; weekly internal email | **Phase 3.** Code aggregation only (no Claude market narrative). `TREND_MIN_N = 10` dated stored rows in a window before any trend/percentage/cadence claim; n=2 renders “insufficient data for a trend”. Sample size and date range are inline. Labels are stored strings — no invented “Other WASH” / “East Africa” buckets. Donor cadence matches `opportunity.donor` against the Phase 2 organizations table only; empty/unapplied table → insufficient, donors not invented. Dates are `discovered_at` or cache `created_at`; never `submission_deadline`. Prefer `opportunities_cache` (optional fact columns in `supabase_migration_opportunity_facts.sql`, **not applied** this session); Airtable OPPORTUNITIES is secondary using existing fields. Fifth timer: `cortech-market.timer` → `main.py --run-market-digest`. SMTP mocked in tests; no live digest sent this session. |
| Outcome learning | Won/Lost lesson extraction and vector storage | Lessons are not scoring features. Phase 6 harness evaluates held-out Brier only when both classes exist; current n_lost=0 → sample too small to trust. |
| Competitor intelligence | Assortis `DataType=contract` pages that publish **Awarded Firm(s):**; stored only with URL + excerpt containing the name | **Phase 5.** Somali Jobs listings and empty RSS have no winner field — not stubbed. Silence (no labelled winner) stores nothing. Cortech-not-winning is not a competitor row. Names go through the Phase 2 org matcher; below threshold → new candidate / UNKNOWN. Migration `supabase_migration_award_relationships.sql` exists; **not applied** this session. Client fail-opens if tables are missing. |
| Relationship intelligence | Named JV/consortium/commissioned-partner edges in `data/proposals/` (verbatim excerpt + document/chunk citation) | **Phase 5.** Patterns only (`submitted by Cortech … in joint venture with`, `in partnership with … and commissioned by`, consortium leader/partner, named JV contributor). “We typically work with…”, sole-firm/no-JV, client-side consortia, and Lead Consultant person roles store nothing. Partner name must appear in the excerpt. Same org matcher. Same unapplied migration / fail-open. Optional review-email section when edges exist; market digest lists cited Assortis winners or an honest gap note. |

## Observed control points

- The CLI validates required Anthropic/OpenAI/Supabase configuration before a
  normal run. Supabase client creation is deferred until it is actually used.
- Airtable remains fail-open with a short rate-limit circuit breaker; a failed
  CRM write does not suppress the draft path.
- Every detailed bid recommendation carries a scoring-model version, factors,
  evidence, confidence, LLM audit values, and a `calibrated_win_probability`
  audit field that is currently INSUFFICIENT DATA (not the bid decision input).
- Review-email dynamic content is HTML escaped and its subject cannot include
  CR/LF header injection.
- Person-level capability results carry MATCH SCORE, WHY, EVIDENCE, and GAPS.
  Busy consultants are kept and labelled; education is never inferred.

## Known material gaps

This is not yet an account-management or executive intelligence product.
Canonical client/donor matching and cited roll-up exist (Phase 2). An
observed-data market digest exists (Phase 3) with thin-n refusal. Field-level
ToR provenance exists. Named past-work proposal claims resolve to retrieved
chunks or are tagged `[NOT VERIFIED]` (Phase 4). Phase 5 stores **cited**
Assortis award-firm rows and **cited** Cortech-submission relationship edges
only — not likely bidders, not a competitor graph, not partners inferred from
silence. Phase 6 shipped a versioned calibration **harness** that refuses to
emit a numeric P(win): labeled n is **123 WON / 0 LOST** (Phase 2 rules).
That fails the bar (n_labeled≥30 **and** ≥10 per class) because there are
zero Lost labels — a one-class fit would be fake calibration. Held-out Brier
is **sample too small to trust**. Heuristic WIN PROBABILITY was not replaced.
A small golden set exists for boolean-gate and scorer regression; live LLM
extraction accuracy against that set has not been run. Phase 7 shipped
durable extract/score/draft stages on the existing `opportunity_processing`
ledger (ADR 011): kill-mid-run after `scored` resumes at draft; BID/WATCH
drafting failures retry then dead-letter. Phase 8 (ADR 012) shipped
DNS-pinned downloads, Playwright timeout/heap flags (not an OS sandbox),
CI `pip-audit`, and an enforced per-run `complete()` spend cap. Hosted
stage-column apply still needs a Postgres URL or Dashboard SQL if the
service role is the only credential.

## Verification

```text
~/cortech-bd-agent/cortech/bin/python -m pytest tests/ -q
1 failed, 343 passed, 2 skipped, 2 warnings in 12.93s
```

The one failure is `test_hosted_opportunity_processing_stage_columns_exist`:
hosted `opportunity_processing` still lacks `pipeline_stage`, `checkpoint`,
and `draft_fail_count`. PostgREST cannot run DDL; apply
`supabase_migration_opportunity_stages.sql` via Dashboard SQL or
`scripts/apply_sql_migration.py` with `DATABASE_URL` /
`SUPABASE_ACCESS_TOKEN` / `SUPABASE_DB_PASSWORD`. The two skips are the
hosted stage-write and spend-cap kill-tests, which require those columns.

Golden extraction tests still pass at the Phase 0/1/2/3/4/5/6 baseline
(consultancy P/R/acc 1.000). There has been no live `--once` or
`--submit-url`. The `content_hash`, `organizations`, `opportunity_facts`,
`award_relationships`, and `opportunity_stages` migrations were not
applied (no Postgres URL / Management API token in the process environment).
Phase 8 code is in-tree (ADR 012); the live spend-cap kill-test on real
columns is blocked on the stages SQL.
