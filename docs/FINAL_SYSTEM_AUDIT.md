# Final system audit

Date: 2026-09-01

## 1. Executive summary

This repository is a local, single-process BD pipeline, not yet a complete BD
Intelligence OS. It reliably supports the narrow operational loop of discovery,
document extraction, structured tender analysis, deterministic bid scoring, CV
matching, proposal drafting, human review email, and basic win/loss lesson
capture.

This upgrade focused on defects that could cause unsafe retrieval, fabricated
financial figures, misleading lifecycle state, or unobservable startup failure:

- document redirects are checked hop by hop; downloads now have finite size,
  timeout, and redirect limits; TLS verification is never disabled;
- the budget path no longer asks an LLM to estimate effort or silently applies
  default rates and percentage add-ons;
- incomplete financial inputs reach reviewers as `PARTIAL` or `INSUFFICIENT
  DATA`, never as a zero-valued “budget draft”;
- automated NO-BID is a drafting-cost gate only; the CRM record remains `New`
  for human confirmation;
- Airtable preserves a supplied lifecycle status; Supabase initialization is
  lazy and fails explicitly at the storage boundary;
- review-email content is HTML escaped and subject text strips line breaks;
- the suite now has 57 passing, network-free tests, including a mocked
  end-to-end orchestration slice.

No external production run was made during this audit. No live tender, client,
consultant, rate card, or outcome was invented or modified.

## 2. Existing architecture

`main.py` orchestrates the local/scheduled workflow. Discovery comes from RSS
(currently disabled), a Playwright scraper, and the Assortis/ICA IMAP
newsletter. `processors/downloader.py` extracts HTML, PDF, DOCX, and Google
Drive packs. Claude performs structured extraction; `bid_scorer.py` converts
extracted facts into versioned deterministic scores. Supabase provides vector
storage/retrieval and Airtable is the human-facing CRM. Proposal drafting and
email delivery remain human-review workflows; nothing is submitted externally
by the agent.

## 3. Final architecture

```text
sources -> discovery filters -> safe fetch/extract -> quality gate
        -> LLM fact extraction (untrusted document boundary)
        -> deterministic bid intelligence -> CV retrieval/matching
        -> evidence-bound financial preparation -> proposal/compliance
        -> Airtable review record + review email -> human decision/outcome
```

The design remains modular in one Python process. No empty microservices or
unimplemented “intelligence domains” were added.

## 4. Major changes

| Area | Change | Result |
|---|---|---|
| Download security | Manual checked redirects, byte cap, configured timeout and redirect cap | An external listing cannot redirect the HTTP client to a literal private/metadata address; oversized streams stop. |
| Transport security | Removed retry with `verify=False` | Broken certificates fail loudly rather than weakening TLS. |
| Financial integrity | Replaced LLM effort estimation/default budget maths | Only explicit ToR effort × exact rate-card rate is costed. No grand total without evidence. |
| Human-in-the-loop | NO-BID remains `New` in CRM | Recommendation may save drafting cost but cannot silently become a final business decision. |
| CRM integrity | `create_opportunity()` preserves caller status | Lifecycle transitions are no longer overwritten during create. |
| Configuration | Lazy Supabase client | Imports do not accidentally become configuration validation or client construction. |
| Email security | HTML escaping and CR/LF-safe subject text | Untrusted content cannot alter rendered review email or add a header. |
| Regression coverage | Added download, budget, lifecycle, email, Supabase, and pipeline-slice tests | Critical behavior is repeatable without live APIs. |

## 5. Files changed in this upgrade

- `.env.example`, `config.py`
- `processors/downloader.py`
- `intelligence/analyzer.py`, `intelligence/budget_calculator.py`
- `database/airtable_client.py`, `database/supabase_client.py`
- `main.py`, `reporting/email_report.py`
- `README.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md`
- `tests/test_airtable_lifecycle.py`, `tests/test_budget_calculator.py`,
  `tests/test_downloader_security.py`, `tests/test_email_safety.py`,
  `tests/test_pipeline_slice.py`, `tests/test_supabase_client.py`

## 6. Intelligence domains

| Domain | Actual state |
|---|---|
| Opportunity | Functional discovery, canonical URL dedup, extraction, quality gates, deterministic bid recommendation. |
| Capability | Semantic CV retrieval with explicit geography/language/years overlays; missing education remains unknown. |
| Bid | Functional deterministic fit/win/strategic/risk dimensions; factors, evidence, weights, and score version stored in analysis JSON. Phase 6 adds a null `calibrated_win_probability` audit field (123 WON / 0 LOST → INSUFFICIENT DATA). Heuristic WIN remains official. |
| Proposal | Drafting uses tender text, past proposal retrieval, style guides, compliance output, human review, and a named past-work claim verifier (`intelligence/grounding.py`). Unsupported names are tagged `[NOT VERIFIED]`. Not a full sentence-level source graph. |
| Outcome | Airtable Won/Lost polling and lesson embedding exist. Phase 6 harness will not emit P(win) until Lost labels exist (current n_lost=0). Lessons are not a scored-model input. |
| Client | Canonical org matching + cited observed-record roll-up in the review email (Phase 2). Migration unapplied; live history often UNKNOWN. Not an account CRM. |
| Market/competitor/relationship/executive | Market: observed digest only (Phase 3). Competitor: Assortis labelled Awarded Firm(s) citations (Phase 5); Somali Jobs/RSS have no winner field and were not stubbed. Relationship: cited JV/consortium edges from Cortech past submissions (Phase 5). Not likely-bidder inference, not an account CRM, not executive briefs. |

## 7. Data model and provenance

Operational records currently span Airtable opportunities/consultants/rate
cards/past proposals/logs and Supabase opportunity cache, document storage,
CV/proposal embeddings, win/loss memory, and (after
`supabase_migration_organizations.sql`) canonical `organizations` /
`organization_aliases` / `organization_observations`. Important bid decisions
retain `score_version`, factor values, factor evidence, LLM audit values, and
the recommendation inside `claude_analysis` / `bid_intelligence`.

Current provenance is adequate for source URL, document cache, extracted
analysis, score factors, retrieved proposal metadata, unique `content_hash`
(when `supabase_migration_content_hash.sql` is applied), field-level ToR
provenance (`extraction_provenance`: value → source file/chunk, page only
if a PAGE marker exists), org-name match status (VERIFIED exact /
INFERRED fuzzy / UNKNOWN new-candidate), and **named past-work proposal
claims** (Phase 4: claim → retrieved `proposal_embeddings` / static past-work
chunk, else `[NOT VERIFIED]`), **Assortis award-firm citations** (Phase 5:
Awarded Firm(s) → URL + excerpt), and **relationship edges** from Cortech
past submissions (document + chunk + excerpt). It is not yet a full source →
page → chunk → every generated sentence graph. Hosted `content_hash`,
`organizations`, and `award_relationships` migrations were not applied in
these sessions.

## 8. AI architecture

Claude is used for extraction, interpretation, proposal drafting, and lesson
extraction. Documents are wrapped as untrusted data. Numeric bid decisions are
deterministic, and the original LLM numbers are audit-only. The budget engine
does not call an LLM. Model IDs, timeout, retries, and estimated pricing are
configuration-driven.

Remaining gap: live Claude extraction vs the golden set is unmeasured.
Named past-work claims in drafts are grounded to retrieved chunks (Phase 4);
that is not a full source→proposal graph for every sentence. Extraction JSON
is schema-validated (Pydantic) with one repair retry then an explicit refuse.

## 9. RAG architecture

CV and historical proposal retrieval use Supabase vectors. Proposal retrieval
deduplicates assignment chunks and preserves project metadata; writer context
also includes tender documents, donor intelligence, and win/loss lessons when
available. Named past-work claims in the draft are matched to those retrieved
chunks after writing (ADR 008). This is hybrid in intent but lacks a measured
lexical/metadata ranking evaluation set.

## 10. Bid scoring architecture

`intelligence/scoring_model.json` version `1.0.0` supplies documented weights.
`bid_scorer.py` computes geography, thematic, language, eligibility, optional
team capacity, deadline, known-client-name overlap, strategic value, risk, and
confidence. It separates FIT, heuristic WIN score, COMMERCIAL VALUE,
STRATEGIC VALUE, RISK, and expected value.

The win score is explicitly **not** a calibrated probability. Expected value
stays `INSUFFICIENT DATA` without verified contract value, pursuit cost, and
risk adjustment. Phase 6 (`intelligence/win_calibration.py`) attaches
`calibrated_win_probability` as an audit field. A 2026-09-15 census found
123 WON and **0 LOST** under Phase 2 rules, so the field is null /
INSUFFICIENT DATA and held-out Brier is “sample too small to trust.” The
heuristic was not replaced. Financial preparation now costs only exact
effort/rate inputs and deliberately returns no grand total without additional
sourced costs.

## 11. Security review

Strengths: placeholders-only environment example, untrusted document wrapping,
literal private-address blocking, per-hop redirect validation, byte limits,
path-safe document names, TLS verification, HTML escaping for review email,
and tests for injection and redirect behavior.

Remaining risks: Playwright still does its own DNS for subresources (request
guard is check-then-fetch, not pin-IP); document parsers are in-process;
Airtable and Supabase use service credentials in a local process. Download
GETs are DNS-pinned (resolve once, connect to that IP). CI runs `pip-audit`;
`requirements.txt` was bumped 2026-09-15 (`cryptography` 50.0.0, `h2` 4.4.1,
`pillow` 12.3.0, `pytest` 9.0.3) and re-scan found no known vulnerabilities.

## 12. Reliability review

Source failures are isolated in the orchestration flow, Airtable is fail-open
with a short circuit breaker, document quality rejects corrupt/empty output,
and configuration/storage failure is explicit. Download limits reduce resource
exhaustion. Phase 7 added durable `pipeline_stage` resume and BID/WATCH
drafting dead-letter. Remaining weaknesses are a large orchestration function
and no distributed retry queue. Playwright resource limits are timeouts/heap
flags, not an OS sandbox.

## 13. Cost review

The pipeline caps opportunities per run, uses URL dedup and embeddings, logs
known-model usage costs as estimates, and removed a budget-stage LLM call.
`complete()` records provider usage and **refuses further Anthropic
`messages.create()` calls** once `MAX_RUN_COST_USD` is reached (zero is a
valid hard stop). Unknown usage/unknown model prices halt rather than
inventing a cost. Spend-cap halt is a retryable ledger `failed` at the last
persisted `pipeline_stage`, not a silent BID/WATCH drop.

## 14. Testing results

Executed locally on 2026-09-01:

```text
./cortech/bin/python -m pytest -q
57 passed

./cortech/bin/python -m compileall -q config.py main.py processors intelligence database reporting utils
success
```

The new mocked vertical slice covers fetch → quality → cache → LLM analysis →
deterministic BID override → financial status → proposal → compliance → CRM
reviewing status. Tests make no live API calls.

Phase 0 grounding re-run locally on 2026-09-17; Phases 1–8 hardening the same day:

```text
./cortech/bin/python -m pytest tests/ --ignore=tests/test_live_supabase_stages.py --override-ini='addopts=' -q --tb=line
366 passed, 2 warnings in 17.30s
```

0 failed, 0 skipped (hosted live tests not run this session). Golden
extraction/scoring: 29 passed. Recorded-JSON parse vs labels: consultancy
P/R/acc 1.000 (29 true / 7 false). Budget MAE 0.000 on 2 labeled numeric
ToRs. Labels are now required to appear in `document_text`.

Phase 1 re-run locally on 2026-09-14:

```text
./cortech/bin/python -m pytest tests/ -q
2 failed, 237 passed in 14.66s
```

The two failures remain the pre-existing grounding cases. Golden extraction
metrics on recorded JSON through the schema-validating `parse_analysis_payload`
are unchanged from Phase 0 (consultancy P/R/acc 1.000).

Phase 5 re-run locally on 2026-09-15:

```text
~/cortech-bd-agent/cortech/bin/python -m pytest tests/ -q
307 passed in 16.00s
```

Golden extraction metrics are unchanged from Phase 0 (consultancy P/R/acc 1.000).

Phase 6 re-run locally on 2026-09-15:

```text
~/cortech-bd-agent/cortech/bin/python -m pytest tests/ -q
319 passed, 2 warnings in 16.95s
```

Golden extraction metrics are unchanged from Phase 0 (consultancy P/R/acc 1.000).
Held-out win-model Brier on production labels: sample too small to trust
(123 WON / 0 LOST).

## 15. Evaluation and regression results

Regression cases added and passing:

- a private-address redirect is blocked before a second request;
- public redirects work when every hop passes validation;
- streamed content over the configured cap fails;
- transient download failures retry with bounded backoff; invalid/oversized
  inputs do not retry;
- no effort/rate input produces no made-up cost;
- explicit effort and rate-card rows produce a traceable personnel subtotal;
- incomplete budget email has no fake `$0` total and escapes hostile HTML;
- create preserves lifecycle state;
- a deterministic NO-BID stays a human-reviewable `New` record;
- Supabase client construction is deferred and missing config fails explicitly;
- thin labeled n and one-class WON piles do not emit a numeric calibrated P(win);
- `won=False` stays UNKNOWN, not Lost;
- malformed factors / missing org history fail-open to a null calibrated field;
- heuristic WIN PROBABILITY remains the bid-decision input.
- DNS-pinned download connects to the resolved public IP; a second DNS
  answer cannot retarget `connect`;
- `$0` spend cap blocks `complete()` before `messages.create()`; a later
  `complete()` is also blocked;
- spend-cap during draft leaves in-memory ledger `pipeline_stage=scored`,
  `state=failed`, not `dead_letter`.

Phase 8 pytest (2026-09-15, after hosted stages SQL + dependency bumps;
reconfirmed same day after the documentation pass):

```text
~/cortech-bd-agent/cortech/bin/python -m pytest tests/ --override-ini='addopts=' -q --tb=line
346 passed, 2 warnings in 33.45s
```

0 failed, 0 skipped. `check_supabase_stages.py` exit 0. All three tests in
`tests/test_live_supabase_stages.py` passed. Hosted E2E SELECT sequence:
discovered → extracted → scored → drafted. Hosted spend-cap kill-test:
`$0` cap, `messages.create()` count 0, row `scored`/`failed` (resumable),
resume `drafted`/`completed`. Prior to the Dashboard SQL apply the same
suite was 1 failed, 343 passed, 2 skipped.

Phase 0 (2026-09-14, grounding pass 2026-09-17) added `tests/golden/` — 36
anonymized items with an offline harness. Recorded-JSON parse vs labels:
consultancy precision/recall 1.000 (29 true / 7 false). Scorer
recommendation accuracy 1.000 on the 29 items with an expected BID/WATCH/
NO-BID band. Budget MAE 0.000 on 2 labeled numeric ToRs. Labeled fields must
appear in `document_text` or stay null. That is a **harness baseline**, not
live Claude extraction accuracy, not retrieval quality, and not a calibrated
Brier score. All golden-set outcomes are UNKNOWN (Airtable was not read for
that set). Phase 6 counted live stores separately: 123 WON / 0 LOST;
held-out Brier is sample too small to trust.

## 16. Remaining technical debt

- Schema validation and one repair retry for analyzer JSON are in place;
  remaining extraction debt is live-LLM evaluation against the golden set,
  not a second parser.
- Store content hash uniqueness (`supabase_migration_content_hash.sql` — file
  present, not applied this session), then extraction version/freshness and a
  fuller lifecycle. Field-level ToR provenance exists. Named past-work
  proposal-claim grounding exists (Phase 4 / ADR 008); a full source → page
  → chunk → every-sentence graph does not.
- Implement DNS-pinned HTTP transport and document-parser isolation if the
  threat model warrants it. **Phase 8:** download-path DNS pin is implemented
  (`utils/dns_pinned_http.py`) with tests that a second resolve cannot
  retarget connect. Playwright/parser isolation is timeout + Chromium flags,
  **not** an OS sandbox.
- ~~Break `main.process_opportunity()` and the large proposal writer into tested
  stage services.~~ **Phase 7:** `intelligence/pipeline_stages.py`
  (`run_extract_stage` / `run_score_stage` / `run_draft_stage`) plus
  `draft_bid_or_watch_proposal()`; unit-tested without the live pipeline.
- ~~Add durable execution records, retries, and failure recovery.~~ **Phase 7:**
  same `opportunity_processing` ledger (not a second table) plus
  `pipeline_stage` + `checkpoint` (`supabase_migration_opportunity_stages.sql`,
  **applied** to hosted Supabase via Dashboard SQL on 2026-09-15).
  Kill-mid-run after `scored` resumes at draft.
  BID/WATCH empty drafts retry then `dead_letter` (3 failures). Optional intel
  still fail-open. `reviewed` / `outcome` are human/Airtable only.
- ~~Complete LLM usage accounting and a configurable spend cap.~~ **Phase 8:**
  `MAX_RUN_COST_USD` is enforced at `complete()` before `messages.create()`.
  Unit and **hosted** kill-tests: `$0` cap, provider `create()` count stays 0,
  ledger stays `scored` / `failed` (not `dead_letter`); resume writes `drafted`.
- Replace direct Airtable/Supabase imports with dependency injection where it
  materially improves testing. StageDeps covers the opportunity pipeline;
  a repo-wide DI rewrite was not in scope.

## 17. Remaining product gaps

- Account-management depth beyond matcher + cited roll-up (applied migration,
  filled Won/Lost history, human-checked aliases). Executive briefings.
  Competitor coverage beyond Assortis Awarded Firm(s); relationship coverage
  beyond named JV/consortium language already in `data/proposals/`. Market
  views exist as an observed digest (Phase 3) but live windows are thin until
  migrations are applied and dated rows accumulate.
- Calibrated win probability based on sufficient historical Won **and Lost**
  labels (Phase 6 harness exists; current n_lost=0 so no trusted model).
- Verified pursuit cost and full financial-pricing workflow.
- Formal partner discovery beyond cited edges already in Cortech submissions.
- Claim-to-source compliance verification for **non-past-work** sentences,
  and attachment/page-limit QA. Named past-work claims are grounded (Phase 4).
- Human approvals, outcome reason codes, and structured lessons feeding scoring.

## 18. Production-readiness assessment

The system is suitable for supervised local operation as a tender intelligence
and draft-assistance pipeline. It is **not** ready to be represented as a
complete BD Intelligence OS or to make autonomous bid/submission decisions.
Production use requires human review of every recommendation, proposal claim,
and financial figure; regular credential rotation; backup and monitoring of
Supabase/Airtable; and staged testing before deploying source or prompt changes.

## 19. Final scorecard

Scores are against the requested BD Intelligence OS standard, not “does the
script run.” Scores of 8 include evidence; lower scores state the main gap.

| Domain | Score | Evidence / concrete gap |
|---|---:|---|
| Architecture | 7 | Coherent modular single process with tested extract/score/draft stage services and durable workflow boundaries on the existing `opportunity_processing` ledger (`pipeline_stage` + checkpoint, ADR 011). Hosted columns exist (`check_supabase_stages.py` exit 0). Kill-mid-run after `scored` resumes at draft rather than re-fetching (proven on the hosted row). Still one process; knowledge layer incomplete. Not 8. |
| Reliability | 7 | Isolated source failures, quality gates, bounded downloads, plus durable stage resume and BID/WATCH drafting retry → `dead_letter` after 3 failures (not a silent skip). Consultancy FALSE still stops before CV/proposal tokens. Optional intel (orgs, market facts, calibrated P(win) INSUFFICIENT DATA) still fail-open. Hosted `opportunity_stages` SQL is applied. Gap: no distributed retry queue. Not 8. |
| Data Quality | 7 | Canonical URL, extraction checks, explicit unknowns, hosted `opportunities_cache.content_hash` unique index (`opportunities_cache_content_hash_uidx`, 2026-09-17 query), field-level ToR provenance, org-name matcher with hosted `organizations` tables (occupancy 0 after probe cleanup). Gap: no freshness model; 1210 existing cache rows have NULL `content_hash` (not back-hashed). Not 8. |
| AI Quality | 7 | Untrusted boundaries, deterministic score boundary, Pydantic extraction schema with one repair retry then explicit fail, LLM-supplied provenance stripped and recomputed, named past-work claim verifier with fail-closed tagging (Phase 4). Gap: live Claude extraction vs golden set is unmeasured; not a full sentence-level claim graph. |
| RAG Quality | 6 | Vector retrieval with metadata and dedup; no measured hybrid retrieval evaluation. |
| Opportunity Intelligence | 6 | Discovery, dedup, extraction, score; limited sources and stale-detection model. |
| Market Intelligence | 4 | Observed-data digest over stored opportunities (Phase 3, ADR 007): trailing 30/90-day thematic and geography frequencies plus donor posting counts for organizations already in the Phase 2 table; weekly internal email (`cortech-market.timer`). Every shown number carries sample size and date range. n&lt;10 (tested at n=2) is “insufficient data for a trend”, never INFERRED-as-trend; no invented buckets (“Other WASH”, “East Africa”) or dummy donors. Code aggregation only — no Claude market narrative, no external research source. `supabase_migration_opportunity_facts.sql` **is applied**; occupancy **0/1210** on thematic_areas, locations, donor. Live digest remains INSUFFICIENT DATA until enough dated fact rows exist. Not a dashboard and not 8–10. |
| Client Intelligence | 5 | Canonical org table + normalize/exact/fuzzy matcher + cited review-email roll-up from stored rows (Phase 2, ADR 006). Exact match is VERIFIED; fuzzy ≥ 0.95 with length/first-token guards is INFERRED; below threshold is a new candidate / UNKNOWN, never a silent merge. Counts are code aggregations; unknown outcomes stay UNKNOWN (golden set is all UNKNOWN — not faked as wins). `supabase_migration_organizations.sql` **is applied** (2026-09-17). Production occupancy is **0** organizations (probe rows deleted). Explicit alias list is empty; this is not an account-management or relationship product. |
| Competitor Intelligence | 4 | Assortis `DataType=contract` pages already in the ICA newsletter HTML publish a labelled **Awarded Firm(s):** block; extraction stores VERIFIED rows only with `source_url` + excerpt containing the name (`intelligence/competitors.py`, ADR 009). Tests: fabricated winner not on the page is not stored; a tender page with no winner field stores nothing; injection without the label is not a winner; two firms are not merged below the Phase 2 matcher threshold. Somali Jobs (`/tenders/` listings) and empty RSS have no winner field and were **not stubbed**. `supabase_migration_award_relationships.sql` **is applied**; `award_observations` occupancy **0**. Not a ranking, not likely bidders, not 8–10. |
| Capability Intelligence | 6 | Semantic + explicit overlay; no complete requirement traceability/availability control. |
| Relationship Intelligence | 5 | Named JV/consortium/commissioned-partner edges extracted from Cortech’s own `data/proposals/` with document + chunk + verbatim excerpt (`intelligence/relationships.py`, ADR 009). Real counterparts on disk include SPI, IBF Expertise, BK Plus Europe, FFTA, and SFERE. Partner name not in the excerpt is not stored; “typically work with”, sole-firm/no-JV, client-side consortia, and Lead Consultant person roles store nothing. Same org matcher. `relationship_edges` table **exists** (occupancy **0**). Optional cited section on the review email; not an account-management graph. Not 9. |
| Bid Intelligence | 8 | Deterministic, versioned scoring with factors/evidence/audit values and tests proving LLM NO-BID cannot force an official NO-BID. Phase 6 harness exists (`win_calibration.py` + versioned artifact v0.1.0) but **no model was fit**: census 123 WON / 0 LOST, bar is n≥30 and ≥10 per class, held-out Brier is “sample too small to trust.” Heuristic WIN PROBABILITY remains official. Not 9. |
| Proposal Intelligence | 8 | Named past-work verifier is real (Phase 4, ADR 008): every “Cortech has done X before” named-client claim must resolve to a retrieved proposal chunk (`chunk_id` like `proposal:recARCH`) or stay in the draft tagged `[NOT VERIFIED]`. Invented clients are not passed through clean; empty retrieval / malformed sections / adversarial text / verifier crash do not crash and do not silent-accept (`fail_closed_ground_sections`). Generic boasts without an entity are `INSUFFICIENT EVIDENCE` in the report only (ADR 003). Gap: this is **not** a source→proposal graph for every sentence, and assignment details beyond named-entity presence are not proven. Not 10. |
| Outcome Intelligence | 6 | Win/loss lesson storage exists. Phase 6 adds an offline held-out evaluator that only emits Brier when both classes meet the bar; production census has 0 LOST so the calibrated field is INSUFFICIENT DATA. Lessons still do not update the official heuristic. Harness exists; model not trusted. |
| Security | 8 | Per-hop SSRF validation, DNS-pinned download GET (resolve once, connect to that IP, reject private/mixed DNS), capped downloads, TLS, input boundaries, escaped email, CI `pip-audit` with patched `cryptography` 50.0.0 / `h2` 4.4.1 / `pillow` 12.3.0 / `pytest` 9.0.3 (re-scan clean). Playwright/parser still not an OS sandbox. Not 9. |
| Observability | 7 | Execution/stage/cost hooks plus enforced per-run spend cap at `complete()`. No metrics backend. |
| Testing | 7 | Golden set of 36 items plus offline parse/scorer metrics (Phase 0), with labels grounded in `document_text`, loader failure tests, and labeled-field provenance VERIFIED (Phase 1). Schema/retry/provenance failure tests including LLM-supplied provenance stripped, non-finite budget refuse, and `log_agent_action` raise on refuse (Phase 1). Org matcher/roll-up/email failure-mode tests including golden-set format variants of the same client (Phase 2). Observed-digest thin-n / empty-store / malformed-row / fail-open / `submission_deadline` is not a discovery date (Phase 3). Named past-work grounding tests including a fabricated-claim writer injection and verifier-crash fail-closed (Phase 4). Phase 5 award/relationship tests. Phase 6 calibration tests. Phase 7 stage-unit + kill-mid-run resume + drafting dead-letter tests. Phase 8 DNS-pin / spend-cap (`CLAUDE_MODEL` from config) / Playwright-limit / CI `pip-audit` with hosted live tests ignored. Offline suite 2026-09-17: **370 passed**, 0 failed, 0 skipped. Hosted: `test_live_supabase_migrations.py` 4 passed, `test_live_supabase_stages.py` 3 passed. Still no staging environment or live-LLM extraction evaluation. |
| Cost Efficiency | 7 | Dedup, capped run, configured model cost, removed budget LLM call, plus `MAX_RUN_COST_USD` enforced at `complete()` (unit and hosted `$0` kill-test: no `messages.create()`; hosted row left `scored`/`failed` then resumed to `drafted`). Not 9. |
| UX | 5 | Useful emails/Airtable review; no dedicated intelligence UI/action queue. |
| Business Value | 7 | Safer opportunity triage, explainable scoring, and grounded financial handoff; organizational intelligence remains incomplete. |

## 20. Recommended next phase

Phase 0 delivered the small golden dataset (`tests/golden/`, ADR 004). Phase 1
delivered schema-validated extraction, `content_hash` uniqueness (**applied**
to hosted `opportunities_cache` 2026-09-17; 1210 existing rows remain NULL
hash), and minimal field-level ToR provenance (ADR 005). Phase 2 delivered
canonical client/donor organizations, a deterministic matcher, and a cited
roll-up on the review email (ADR 006; **tables applied**, production occupancy
0). Phase 3 delivered an observed-data market digest from stored opportunities
(ADR 007; fact columns **applied**; 0/1210 thematic/location/donor occupancy;
thin-n refusal). Phase 4 delivered named past-work claim grounding (ADR 008):
generated “Cortech has done X” claims resolve to a retrieved chunk or are
tagged `[NOT VERIFIED]`. Phase 5 delivered cited Assortis Awarded Firm(s)
observations and cited relationship edges from Cortech past submissions
(ADR 009; `award_relationships` **applied**; occupancy 0/0). Phase 6 delivered
a versioned calibration harness (ADR 010) and **refused to fit a model**:
labeled n is 123 WON / 0 LOST under Phase 2 rules; held-out Brier is “sample
too small to trust”; heuristic WIN PROBABILITY was not replaced. Phase 7
delivered durable pipeline stages on the existing `opportunity_processing`
ledger (ADR 011): extract → score → draft resume, BID/WATCH drafting
dead-letter, kill-mid-run test. `opportunity_stages` SQL **was applied** to
hosted Supabase (Dashboard SQL, 2026-09-15). Next: a **live** extraction (and
later retrieval) pass against the golden set, a human approval/outcome schema
that records Lost as well as Won, and only after both classes meet the bar,
train and validate a calibrated win model against the heuristic on held-out
data. `win_loss_memory` is still absent. **Phase 8** (ADR 012) is complete:
download-path DNS pin, Playwright timeout/heap flags (not an OS sandbox),
CI `pip-audit` (scan clean after bumps listed in
`docs/SECURITY.md`), enforced `MAX_RUN_COST_USD` at `complete()`, and the
hosted spend-cap kill-test on the real `opportunity_processing` row.
