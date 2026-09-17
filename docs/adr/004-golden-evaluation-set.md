# ADR 004 — Golden evaluation set (Phase 0)

## Status

Accepted

## Context

`docs/FINAL_SYSTEM_AUDIT.md` is explicit that this repo is a 6–8/10
opportunity-to-proposal pipeline, not a BD Intelligence OS, because nothing is
measured against labeled history.

Scorecard / recommended-next-phase lines this ADR implements:

- §15: “There is no labelled golden corpus or held-out extraction/retrieval
  benchmark, so no accuracy, calibration, precision, recall, or Brier score is
  claimed.”
- §19 Testing: “57 passing isolated tests plus mocked vertical slice; no
  staging/golden/evaluation suite.”
- §19 evidence notes more broadly: Opportunity / RAG / AI quality scores sit
  at 6 because extraction and retrieval are **not measured**. Bid Intelligence
  is 8 as a heuristic, “not calibrated.”
- §20: “First build a migration-backed opportunity/document/fact/provenance
  lifecycle **and a small golden dataset**. Then validate extraction and
  retrieval against it, … and only after enough structured outcomes exist,
  train and validate a calibrated win model.”

Phase 0 is the golden dataset and the offline harness. It does **not** add a
Supabase table, a calibrated win model, or any later-phase product.

## Decision

1. Keep a 20–40 item hand-labeled set at `tests/golden/opportunities.json`.
2. Source labels from local `data/proposals/` (assignment titles, geography,
   thematics visible in the documents) plus synthetic negatives and a few
   synthetic ToRs so numeric MAE is not undefined. Do **not** read `.env` or
   Airtable for this set.
3. Anonymize buyers as `Client A` / `UN-Agency A`. Keep public-tender-typical
   geography (Somalia, Kenya, Sudan, …).
4. Known-correct fields per item: `is_consultancy_contract`, client
   (anonymized or null), deadline, budget (number or null), thematic areas,
   geography, outcome (`WON` / `LOST` / `UNKNOWN`). Outcomes are `UNKNOWN`
   until a later human review of CRM history.
5. Include staff-vacancy negatives so precision/recall on the boolean gate is
   defined for both classes.
6. Offline tests feed **recorded analyzer JSON** through
   `intelligence.analyzer.parse_analysis_payload` (the production parse /
   fail-open path) and feed **label-constructed analysis dicts** through
   `intelligence.bid_scorer`. `complete()` is mocked; CI does not need
   `ANTHROPIC_API_KEY`.
7. Missing facts stay `null` / `UNKNOWN` / `INSUFFICIENT DATA`. Garbage
   analysis input must not crash the scorer.
8. Record baseline metrics in `docs/CURRENT_STATE.md` as what was actually
   measured (offline harness), not as live extraction accuracy.
9. Every non-null labeled client, ISO deadline, positive budget, geography
   token, and thematic must appear in `document_text`. Vacancy items must
   contain HR/staff language. Impossible dates, zero/boolean budgets,
   unknown `source_kind` values, duplicate ids, and non-array files fail
   the loader explicitly. Recorded JSON may include extra keys and comma-
   formatted numbers so `parse_analysis_payload` is not an identity test.

## Consequences

- Later phases can quote a number instead of “not measured.” That number is a
  **harness baseline**, not a claim that Claude extracts fields at 100%.
- Anonymized clients will not match `scoring_model.json` `known_clients`. FIT
  / recommendation tests therefore exercise geography and thematics, not name
  overlap.
- Won/Lost calibration remains blocked until outcomes are labeled.
- Phase 6 (calibrated win model) is still forbidden until this set has real
  outcomes and a live extraction pass. Phase 1 of the upgrade is schema
  validation / provenance, not a win model.
- Labeled client, deadline, budget, geography, and thematics must appear in
  `document_text` (or stay null because the text does not state them). A
  recorded JSON object that copies labels is not a substitute for grounded
  fixtures. Extra keys and comma-formatted budget strings in recorded JSON
  exercise parse/coerce, not identity copy.

Pytest on 2026-09-17 (`./cortech/bin/python -m pytest tests/ --ignore=tests/test_live_supabase_stages.py --override-ini='addopts=' -q --tb=line`):

```text
366 passed, 2 warnings in 17.30s
```

0 failed, 0 skipped. Hosted `tests/test_live_supabase_stages.py` (3 tests) were not run
in this session (live Supabase). Golden files:
`tests/test_golden_extraction.py` and `tests/test_golden_scoring.py` — 29 passed.
