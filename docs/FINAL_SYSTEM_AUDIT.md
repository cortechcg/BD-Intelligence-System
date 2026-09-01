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
| Bid | Functional deterministic fit/win/strategic/risk dimensions; factors, evidence, weights, and score version stored in analysis JSON. |
| Proposal | Drafting uses tender text, past proposal retrieval, style guides, compliance output, and human review. Claim-level verification is not present. |
| Outcome | Airtable Won/Lost polling and lesson embedding exist; lessons are not yet a scored-model input. |
| Market/client/competitor/relationship/executive | Not operational as dedicated, evidence-backed products. No fake foundation was added. |

## 7. Data model and provenance

Operational records currently span Airtable opportunities/consultants/rate
cards/past proposals/logs and Supabase opportunity cache, document storage,
CV/proposal embeddings, and win/loss memory. Important bid decisions retain
`score_version`, factor values, factor evidence, LLM audit values, and the
recommendation inside `claude_analysis` / `bid_intelligence`.

Current provenance is adequate for source URL, document cache, extracted
analysis, score factors, and retrieved proposal metadata. It is not yet a full
source → page → chunk → claim graph. `content_hash` is logged but not a
database uniqueness field.

## 8. AI architecture

Claude is used for extraction, interpretation, proposal drafting, and lesson
extraction. Documents are wrapped as untrusted data. Numeric bid decisions are
deterministic, and the original LLM numbers are audit-only. The budget engine
does not call an LLM. Model IDs, timeout, retries, and estimated pricing are
configuration-driven.

Remaining gap: extraction is JSON parsing after a single model call, not a
fully schema-validated structured-output contract; proposal claims do not yet
have automated source-chunk verification.

## 9. RAG architecture

CV and historical proposal retrieval use Supabase vectors. Proposal retrieval
deduplicates assignment chunks and preserves project metadata; writer context
also includes tender documents, donor intelligence, and win/loss lessons when
available. This is hybrid in intent but lacks a measured lexical/metadata
ranking evaluation set and claim-level grounding checks.

## 10. Bid scoring architecture

`intelligence/scoring_model.json` version `1.0.0` supplies documented weights.
`bid_scorer.py` computes geography, thematic, language, eligibility, optional
team capacity, deadline, known-client-name overlap, strategic value, risk, and
confidence. It separates FIT, heuristic WIN score, COMMERCIAL VALUE,
STRATEGIC VALUE, RISK, and expected value.

The win score is explicitly **not** a calibrated probability. Expected value
stays `INSUFFICIENT DATA` without verified contract value, pursuit cost, and
risk adjustment. Financial preparation now costs only exact effort/rate inputs
and deliberately returns no grand total without additional sourced costs.

## 11. Security review

Strengths: placeholders-only environment example, untrusted document wrapping,
literal private-address blocking, per-hop redirect validation, byte limits,
path-safe document names, TLS verification, HTML escaping for review email,
and tests for injection and redirect behavior.

Remaining risks: DNS rebinding is not pinned at the network transport layer;
Playwright executes a full browser against public pages; document parsers are
not sandboxed; there is no dependency vulnerability scan in CI; Airtable and
Supabase use service credentials in a local process.

## 12. Reliability review

Source failures are isolated in the orchestration flow, Airtable is fail-open
with a short circuit breaker, document quality rejects corrupt/empty output,
and configuration/storage failure is explicit. Download limits reduce resource
exhaustion. Remaining weaknesses are a large orchestration function, no durable
execution state machine, no retry queue/dead-letter store, and no integration
test against staging Airtable/Supabase.

## 13. Cost review

The pipeline caps opportunities per run, uses URL dedup and embeddings, logs
known-model usage costs as estimates, and removed a budget-stage LLM call. Cost
tracking remains incomplete because some proposal calls retain coarse token
logging; there is no enforced per-run or per-opportunity monetary budget.

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
- Supabase client construction is deferred and missing config fails explicitly.

There is no labelled golden corpus or held-out extraction/retrieval benchmark,
so no accuracy, calibration, precision, recall, or Brier score is claimed.

## 16. Remaining technical debt

- Replace single-shot LLM JSON parsing with schema validation and repair/retry
  policy.
- Store content hash, extraction/version/provenance, freshness, and lifecycle
  fields in deliberate database migrations.
- Implement DNS-pinned HTTP transport and document-parser isolation if the
  threat model warrants it.
- Break `main.process_opportunity()` and the large proposal writer into tested
  stage services.
- Add durable execution records, retries, and failure recovery.
- Complete LLM usage accounting and a configurable spend cap.
- Replace direct Airtable/Supabase imports with dependency injection where it
  materially improves testing.

## 17. Remaining product gaps

- Canonical organizations, client/account profiles, market trends, competitors,
  evidence-backed relationships, and executive briefings.
- Calibrated win probability based on sufficient historical Won/Lost data.
- Verified pursuit cost and full financial-pricing workflow.
- Formal partner discovery and relationship evidence.
- Claim-to-source compliance verification and attachment/page-limit QA.
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
| Architecture | 6 | Coherent modular single process; lacks durable workflow boundaries and knowledge layer. |
| Reliability | 6 | Isolated source failures, quality gates, bounded downloads; no durable retry/state system. |
| Data Quality | 6 | Canonical URL, extraction checks, explicit unknowns; no canonical entity/freshness model. |
| AI Quality | 6 | Untrusted boundaries and deterministic score boundary; no schema/claim verifier. |
| RAG Quality | 6 | Vector retrieval with metadata and dedup; no measured hybrid retrieval evaluation. |
| Opportunity Intelligence | 6 | Discovery, dedup, extraction, score; limited sources and stale-detection model. |
| Market Intelligence | 0 | No observed-data trend product. |
| Client Intelligence | 2 | Client fields/retrieval context exist; no canonical account profile. |
| Competitor Intelligence | 0 | Not implemented. |
| Capability Intelligence | 6 | Semantic + explicit overlay; no complete requirement traceability/availability control. |
| Relationship Intelligence | 0 | Not implemented. |
| Bid Intelligence | 8 | Deterministic, versioned scoring with factors/evidence/audit values and tests proving LLM NO-BID cannot force an official NO-BID. Gap: win score is heuristic, not calibrated. |
| Proposal Intelligence | 6 | Tender/past-proposal context and compliance output; no claim verification. |
| Outcome Intelligence | 5 | Win/loss lesson storage exists; lessons do not update scoring. |
| Security | 7 | Per-hop SSRF validation, capped downloads, TLS, input boundaries, escaped email; no DNS pin/parser sandbox. |
| Observability | 6 | Execution/stage/cost hooks; incomplete proposal token recording and no metrics backend. |
| Testing | 7 | 57 passing isolated tests plus mocked vertical slice; no staging/golden/evaluation suite. |
| Cost Efficiency | 7 | Dedup, capped run, configured model cost, removed budget LLM call; no enforced spend cap. |
| UX | 5 | Useful emails/Airtable review; no dedicated intelligence UI/action queue. |
| Business Value | 7 | Safer opportunity triage, explainable scoring, and grounded financial handoff; organizational intelligence remains incomplete. |

## 20. Recommended next phase

First build a migration-backed opportunity/document/fact/provenance lifecycle
and a small golden dataset. Then validate extraction and retrieval against it,
add a human approval/outcome schema, and only after enough structured outcomes
exist, train and validate a calibrated win model. Build client and market views
from those observed records before attempting competitors, relationships, or
strategic recommendations.
