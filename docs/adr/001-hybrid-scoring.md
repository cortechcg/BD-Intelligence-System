# ADR 001 — Hybrid bid scoring

## Status

Accepted

## Context

`analyze_rfp()` returned `cortech_fit_score`, `win_probability`, and `bid_recommendation` from Claude. Those numbers were stored on Airtable and gated the NO-BID path. An LLM number is not a scoring model: it is unstable, unversioned, and a prompt-injection target.

## Decision

1. Claude continues to extract structured fields and qualitative strengths/gaps.
2. `intelligence/bid_scorer.py` calculates FIT / WIN / STRATEGIC / RISK from those fields plus `intelligence/scoring_model.json`.
3. LLM numbers are stored as `llm_*` audit fields only.
4. `score_version` and weights are persisted with the result so a later weight change does not rewrite history.
5. Expected Value is INSUFFICIENT DATA unless P(win), contract value, and pursuit cost are all known. Do not invent pursuit cost.

## Consequences

- Recommendation can disagree with Claude; that is intended.
- WIN PROBABILITY is still a heuristic, not a calibrated P(win).
- Airtable field names are unchanged (`relevance_score`, etc.) so `check_schema.py` stays valid.
