# Scoring model 1.0.0

Weights live in `intelligence/scoring_model.json`. Changing that file does **not** rewrite historical Airtable/Supabase records; each result stores `score_version` and the weights used.

## Rule

The official FIT, WIN PROBABILITY, and BID/WATCH/NO-BID are calculated in `intelligence/bid_scorer.py`. Claude's numbers are advisory (`llm_audit`).

## Dimensions (kept separate)

| Dimension | Meaning | Missing inputs |
|---|---|---|
| **FIT** | Geography, thematic, language, eligibility, team coverage | Factor dropped and remaining weights renormalized; if none usable, FIT is UNKNOWN |
| **WIN PROBABILITY** | Derived from FIT + deadline feasibility + known-client name overlap + eligibility | Same; not a guess at P(win) from vibes |
| **COMMERCIAL VALUE** | `estimated_budget_usd` if a positive number was extracted | `null` / UNKNOWN — not invented |
| **STRATEGIC VALUE** | Core thematic + priority geography + name on the profile client list | UNKNOWN factors dropped |
| **RISK** | Deadline pressure + team gaps + eligibility gaps (higher = riskier) | UNKNOWN dropped |
| **Expected Value** | `P(win)×contract_value − pursuit_cost − risk_adjustment` | **INSUFFICIENT DATA** unless all three USD inputs exist. Pursuit cost is not known at analysis time, so EV is almost always INSUFFICIENT DATA. That is correct. |

`known_client` is **name overlap with CORTECH_PROFILE**, labelled as such. It is not a verified current relationship.

Eligibility: credentials not on the profile are UNKNOWN, not a fabricated fail. The scorer will not invent ISO/PSEA/registration the company does not list.

## Recommendation thresholds (v1.0.0)

- BID if FIT ≥ 70
- WATCH if FIT ≥ 45 (or FIT is UNKNOWN)
- NO-BID if FIT < 45
- `is_consultancy_contract` remains a separate gate in `main.py` (default True)

## Evidence labels

Each factor has `status`: VERIFIED (extracted field matched a list), INFERRED (partial), UNKNOWN (not extracted / not guessed).

## Capability matching (person-level)

`intelligence/cv_matcher.py` scores each ToR role independently of the bid FIT number.

| Factor | Source | Missing input |
|---|---|---|
| Semantic | pgvector similarity | Treated as 0 if the search row has no score |
| Geography | role `required_geographic_experience`, else opportunity locations | UNKNOWN — not inferred |
| Sector | role thematic/sector, else opportunity `thematic_areas`, vs CV `thematic_expertise` | UNKNOWN — not inferred |
| Language | role languages, else opportunity `language_requirements` | UNKNOWN — not inferred |
| Years | `years_experience_minimum` vs CV `years_experience` | UNKNOWN — not inferred |
| Skills | `required_skills` vs CV `key_skills` / `tools` | UNKNOWN — not inferred |
| Availability | live Airtable `availability_status` (optional `availability_percentage`) | UNKNOWN — **not** assumed Available / 100% |
| Education | ToR `required_education` | Always UNKNOWN — education is not on CV embedding metadata |

MATCH SCORE is the mean of factors that have a number. WHY / EVIDENCE / GAPS travel with each role and a team `capability_summary`. Busy consultants stay in the list. Missing credentials are never invented.

## What this is not

It is not a calibrated win-probability model. WIN PROBABILITY is a weighted heuristic on observed factors, 0–100, not a frequentist P(win).

Phase 6 added an audit field `calibrated_win_probability` on `bid_intelligence` (`intelligence/win_calibration.py`, artifact `intelligence/win_calibration_artifact.json` v0.1.0). It is **null / INSUFFICIENT DATA** until there are at least 30 distinct labeled WON+LOST outcomes **and** at least 10 of each class. A 2026-09-15 census found **123 WON and 0 LOST** (Phase 2: unchecked `won=False` is UNKNOWN, not Lost; golden set is 0/0/36). Held-out Brier is therefore **sample too small to trust**. The heuristic is still official. A later model may only replace it if it beats `score/100` on held-out Brier or log loss. LLM output is not this probability.
