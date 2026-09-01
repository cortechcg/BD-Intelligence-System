# Final system audit (this pass)

Date: 2026-09-01. Scores are 0–10 against a full BD Intelligence OS, not against “did we add files”. ≥8 requires cited evidence in this repo. Most domains are below 8 on purpose.

## Scorecard

| Domain | Score | Evidence or gap |
|---|---|---|
| Discovery (RSS/scrape/IMAP) | 6 | OBSERVED: three sources still run; one failed source does not kill `run_pipeline`. Gap: still a handful of portals, not a coverage model. |
| Document fetch/extract | 7 | OBSERVED: PDF/DOCX/HTML/Drive + Playwright; quality gate + DOCUMENT_ERROR; SSRF guard. Gap: SSL-verify-off retry remains; DNS rebinding unsolved. |
| LLM extraction | 7 | OBSERVED: existing analyzer schema including consultancy gate + award vs assignment criteria. Untrusted wrap added. Gap: still one-shot JSON, no schema validator beyond `json.loads`. |
| **Hybrid bid scoring** | 8 | OBSERVED: `intelligence/bid_scorer.py` + `scoring_model.json` v1.0.0; wired in `process_opportunity`; tests prove LLM `12`/`NO-BID` becomes code BID when Somalia/MEL/UNICEF; EV is INSUFFICIENT DATA without pursuit cost. Gap: WIN is not a calibrated P(win). |
| Capability matching | 6 | OBSERVED: semantic search retained; `score_capability_match` adds geography/years and refuses to infer education. Gap: not a full requirements-traceability engine; languages on ToR roles often absent from the analyzer schema. |
| Proposal grounding | 6 | OBSERVED: past-work retrieval already ranked; source metadata now explicit; `[NOT VERIFIED]` instruction added. Gap: no automated claim-to-chunk verifier. |
| Compliance matrix | 5 | OBSERVED: new `build_compliance_matrix` written to existing Airtable field; financial = MISSING honestly; award criteria not mixed with DAC. Gap: almost all award rows are UNKNOWN — we do not score the draft against each criterion programmatically. |
| Dedup / idempotency | 7 | OBSERVED: canonical URL + identity keys; Assortis semantic near-dup unchanged. Gap: `content_hash` is logged, not a Supabase column (no silent schema invent). |
| Observability / cost | 6 | OBSERVED: `execution_id`, stage logs, Anthropic usage when present, ESTIMATED cost from config table else UNKNOWN. Gap: not all Claude calls in `proposal_writer` go through `record_usage`; proposal token totals in AGENT_LOGS were already hardcoded estimates and remain coarse. |
| Config | 7 | OBSERVED: `require_env()` at CLI start; `.env.example` placeholders; models only from config. Gap: import of `supabase_client` still constructs a client at import time. |
| Security | 6 | OBSERVED: secrets stripped from `.env.example`; SSRF allowlist; path basename; untrusted wrap; `get_text()` regressions fixed in analyzer/budget/supabase/populate. Gap: no DNS pin, SSL fallback still disables verify, Playwright is a full browser. |
| Tests | 7 | OBSERVED: 33 unit tests, mocked-by-construction (no live APIs), covering URL/dedup keys, dates, empty docs, injection wrap, scorer, capability education UNKNOWN, compliance. Gap: no integration test of `process_opportunity` with mocked Claude. |
| Win/loss learning | 5 | OBSERVED: `intelligence/learning.py` unchanged and still used. Gap: lessons are not yet an input to the scorer. |
| Competitor / relationship / KG / exec brief / UI | 0 | Not built. Intentionally. |
| Maintainability | 6 | OBSERVED: no new frameworks; scoring is a pure function + JSON. Gap: `proposal_writer.py` remains a very large module. |
| Business value (this pass) | 7 | A reviewer can now see WHY/RISKS/GAPS/evidence and a versioned score instead of an opaque Claude integer. EV honesty (INSUFFICIENT DATA) is more useful than a fake ROI. |

**Overall (as a BD pipeline, not an OS): 6.5.**  
**Overall (as a “70-section Intelligence OS”): ~3.** The second number is the honest one if the bar is the full directive. Shipping empty domain packages would have raised the second number and lowered truth.

## What was preserved

CVs, `data/proposals/`, style guides, embeddings scripts, Airtable/Supabase clients, consultancy gate default True, permissive `quick_relevance_check`, fail-open Airtable, fail-fast Airtable retry, `CLAUDE_MODEL` / `CLAUDE_MODEL_PROPOSAL`, human-in-the-loop email.

## What this pass changed (summary)

P0: secrets example file, SSRF, untrusted prompts, `get_text` regressions, `typecast=True` on opportunity updates, RSS timeout + wrapped logs, document quality, error_type at call sites, execution_id, env fail-loud, tests.

P1: hybrid scoring wired; capability overlay; compliance matrix statuses.

## Remaining P1–P4 gaps (not fake-closed)

- P1: calibrated P(win) from real won/lost rows; pursuit-cost model from actual draft effort; content_hash column if a migration is applied on purpose.
- P1: claim-level proposal grounding (quote → source chunk).
- P2: more portals, donor intelligence used in scoring, availability as a hard constraint.
- P3: competitor/account intelligence — do not stub.
- P4: Airtable field for `score_version` if humans want it as a column (today it is inside `claude_analysis`). Run `check_schema.py` before adding it.
