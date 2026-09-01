# Current state

Updated: 2026-09-01. This is an implementation inventory, not a roadmap.
`FINAL_SYSTEM_AUDIT.md` contains the detailed evidence and scorecard.

## What is operational

| Capability | Implementation | State |
|---|---|---|
| Discovery | RSS support (currently no configured feeds), Somali Jobs Playwright scraper, Assortis/ICA IMAP parser | Functional local sources; coverage is limited. |
| Fetch and extraction | HTML, PDF, DOCX, Google Drive packs, browser/PDF-link fallbacks | Quality gate rejects empty/corrupt/access-wall output. |
| Download security | Public HTTP URL policy, per-hop redirect validation, TLS verification, bounded bytes/redirects/retries | DNS rebinding and browser/parser sandboxing remain open. |
| Deduplication | Canonical exact URL and title-vector near duplicate path | Content hash is logged, not a database identity field. |
| Extraction | Claude JSON extraction with untrusted-document boundary | One-shot JSON parsing; no formal schema repair/validation. |
| Bid intelligence | `scoring_model.json` + deterministic `bid_scorer.py` | FIT/WIN heuristic/strategic/risk/EV separated; LLM numbers are audit-only. |
| Capability | Supabase CV semantic retrieval plus explicit geography/language/years overlay | Missing CV evidence stays UNKNOWN. |
| Financial preparation | Explicit ToR effort × exact Airtable rate-card row only | Never a complete financial proposal without non-personnel cost evidence. |
| Proposal | Tender-aware drafting, style guides, past-proposal retrieval, review email/docx | No automated claim-to-chunk verification. |
| Compliance | CV/financial/attachments/award-criterion status matrix | Award criteria are not programmatically proven satisfied. |
| Human control | Review email and Airtable record; no external submission | NO-BID is a recommendation on a `New` record, not final automation. |
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

## Known material gaps

This is not yet a market, client, competitor, relationship, account, or
executive intelligence product. There is no canonical organization graph,
calibrated win-probability model, costed pursuit model, durable workflow state
machine, golden-data evaluation set, or full provenance chain from source page
to generated proposal claim.

## Verification

The local network-free suite passes 57 tests, including a mocked complete
orchestration slice and adversarial download/email cases. There has been no
live service or production tender run as part of this update.
