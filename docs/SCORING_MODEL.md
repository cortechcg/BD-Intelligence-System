# Scoring model 1.0.0

Weights live in `intelligence/scoring_model.json`. Changing that file does **not** rewrite historical Airtable/Supabase records; each result stores `score_version` and the weights used.

## Rule

The official FIT, WIN PROBABILITY, and BID/WATCH/NO-BID are calculated in `intelligence/bid_scorer.py`. Claude's numbers are advisory (`llm_audit`).

## Dimensions (kept separate)

| Dimension | Meaning | Missing inputs |
|---|---|---|
| **FIT** | Geography, thematic, language, eligibility, team coverage | Since v1.1.0 scored at `unknown_factor_value` (25) with its full weight — a penalty, never dropped. `worst_case`/`best_case` store FIT with every UNKNOWN at 0 / 100. If no factor is known, FIT is UNKNOWN |
| **WIN PROBABILITY** | Derived from FIT + deadline feasibility + known-client name overlap + eligibility | Same; not a guess at P(win) from vibes |
| **COMMERCIAL VALUE** | `estimated_budget_usd` if a positive number was extracted | `null` / UNKNOWN — not invented |
| **STRATEGIC VALUE** | Core thematic + priority geography + name on the profile client list | UNKNOWN factors dropped |
| **RISK** | Deadline pressure + team gaps + eligibility gaps (higher = riskier) | UNKNOWN dropped |
| **Expected Value** | `P(win)×contract_value − pursuit_cost − risk_adjustment` | **INSUFFICIENT DATA** unless all three USD inputs exist. Pursuit cost is not known at analysis time, so EV is almost always INSUFFICIENT DATA. That is correct. |

`known_client` is **name overlap with CORTECH_PROFILE**, labelled as such. It is not a verified current relationship.

Eligibility: credentials not on the profile are UNKNOWN, not a fabricated fail. The scorer will not invent ISO/PSEA/registration the company does not list.

## Recommendation thresholds (v1.1.0)

- BID only if the **worst case** FIT (every UNKNOWN factor at 0) is ≥ 70.
  Learning an unknown factor can only raise FIT, so a BID can never fall back
  to WATCH, and missing evidence never earns a BID. (The 2026-10 incident: the
  same tender scored 59 then 77 because v1.0.0 dropped UNKNOWN factors.)
- NO-BID if the penalised FIT < 45 **and** at least half the fit weight is
  known evidence (`no_bid_min_known_coverage`), or if eligibility is verified
  ineligible. A low score made of penalties alone is WATCH.
- WATCH otherwise (and when FIT is UNKNOWN).
- `is_consultancy_contract` remains a separate gate in `main.py` (default True)
- Tests: `tests/test_scoring_unknowns.py` (exhaustive single-unknown flip check).
  The golden set's labels carry only geography and thematic, so
  `tests/test_golden_scoring.py` asserts the scorer is never more aggressive
  than the human label; exact match is printed for information.

## Draft quality gate

`intelligence/quality_gate.py` scores the written draft (0–100, bar 70) against
the ToR's weighted criteria, else the ToR's criteria equally weighted (no
weight figure claimed), else the reading brief's criteria, else standard
criteria marked INFERRED. A criterion whose section rests on
[INSUFFICIENT EVIDENCE], a placeholder or an unverified claim earns zero.
Placeholders, unverified named past work, unsupported claims, leaked template
numbering and evaluation weights the ToR does not state are blockers. Failing
sections are rewritten and re-scored at most twice. Below 70 or with any
blocker the draft is BLOCKED: the review email subject starts with BLOCKED and
the dashboard never shows it as ready. Tests: `tests/test_quality_gate.py`.

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
