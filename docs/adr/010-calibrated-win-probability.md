# ADR 010 — Calibrated win probability (Phase 6)

## Status

Accepted

## Context

This is Phase 6 of the S-Tier BD Intelligence OS upgrade. It does **not**
start Phase 7 (reliability / architecture debt), a durable state machine,
or replacement of the official heuristic WIN PROBABILITY.

Quoted **before** any predictor, artifact coefficients, or tests were
written:

From `docs/SCORING_MODEL.md`:

> It is not a calibrated win-probability model. There is no historical
> win/loss regression yet. WIN PROBABILITY is a weighted heuristic on
> observed factors, 0–100, not a frequentist P(win).

From the same file, the WIN PROBABILITY dimension:

> **WIN PROBABILITY** | Derived from FIT + deadline feasibility +
> known-client name overlap + eligibility | Same; not a guess at P(win)
> from vibes

From `docs/FINAL_SYSTEM_AUDIT.md` §20 (recommended next phase, as of
the pre-Phase-6 audit):

> Next: apply the hash, organizations, opportunity-facts, and
> award-relationships migrations, a **live** extraction (and later
> retrieval) pass against the golden set, a human approval/outcome
> schema, and only after enough structured outcomes exist, train and
> validate a calibrated win model. **Phase 6 (calibrated win model) was
> not started here.**

From §19 Bid Intelligence (pre-Phase-6 evidence):

> Deterministic, versioned scoring with factors/evidence/audit values
> and tests proving LLM NO-BID cannot force an official NO-BID. Gap:
> win score is heuristic, not calibrated.

ADR 001 remains in force: Claude’s numbers are audit-only; official
FIT / WIN / BID|WATCH|NO-BID come from `intelligence/bid_scorer.py` +
`intelligence/scoring_model.json`. Changing a later artifact must not
rewrite historical stored scores.

Phase 0 golden set (ADR 004): all 36 outcomes are `UNKNOWN`. Anonymized
names do not hit `known_clients`. Those rows are **not** training labels.

Phase 2 outcome rules (ADR 006) remain in force and were applied to the
census below:

> - `win_loss_memory.outcome` in `{Won, Lost, WON, LOST}` → WON/LOST VERIFIED
> - `proposal_embeddings.won is True` → WON VERIFIED
> - `proposal_embeddings.won is False` or missing → **UNKNOWN**, not Lost.
>   Airtable’s won checkbox being unchecked is indistinguishable from “not
>   filled.”
> - Golden-set items are not a live observation store; their outcomes stay
>   UNKNOWN and are not ingested as wins.

§20 is explicit that a calibrated win model comes **after** golden set
and org/market views, and only after enough structured outcomes exist.
Phases 0–5 are done. This phase is the one most at risk of fake
calibration: fitting on a handful of rows, or on a one-class WON pile
with zero LOST labels, and calling the result P(win).

### Census (read-only, 2026-09-15)

Stores were counted with existing clients, fail-open, **without**
modifying `.env` or printing secrets. `won=False` / missing checkbox is
UNKNOWN, not Lost. No Won/Lost labels were invented.

| Source | What was counted | WON | LOST | UNKNOWN |
|---|---|---:|---:|---:|
| `tests/golden/opportunities.json` | 36 items | 0 | 0 | 36 |
| `output/learning/*.json` | 5 draft-memory files | 0 | 0 | 5 (no outcome field) |
| `data/proposals/` | 117 PDF/DOCX files | — | — | no sidecar metadata |
| Supabase `win_loss_memory` | table missing (`PGRST205` / schema cache) | 0 | 0 | fail-open empty |
| Airtable `OPPORTUNITIES` | 1 row, status `Reviewing` | 0 | 0 | 1 |
| Airtable `PAST_PROPOSALS` | 216 rows; `won` field present on 84 | 84 | 0 | 132 (checkbox absent) |
| Supabase `proposal_embeddings` | 270 distinct `airtable_proposal_id` | 123 | 0 | 147 (`won is False`) |

Dedup: every PAST_PROPOSALS id appears in `proposal_embeddings`. Union of
distinct WON assignment ids = **123**. Union of distinct LOST ids = **0**.

Labeled n used for the bar: **n_labeled = 123 WON + 0 LOST**. The WON
count exceeds 30; the LOST count is zero. A binary P(win) fit on
positives only is degenerate (it would emit ~1.0). Unchecked `won=False`
must not be recoded as Lost to manufacture a negative class.

## Decision

1. **Threshold (conservative, documented).** A numeric
   `calibrated_win_probability` is computed only when **all** of:
   - `n_labeled >= MIN_LABELED_N` (30 distinct WON+LOST outcomes)
   - `n_won >= MIN_PER_CLASS` and `n_lost >= MIN_PER_CLASS` (10 each)

   Why 30: logistic regression on a handful of rows is not calibration
   (the scorecard example). Why both classes: Brier / log loss for a
   win/loss model is not defined in a useful way with `n_lost = 0`; a
   one-class fit is the fake-calibration failure mode this phase must
   refuse. Held-out evaluation further splits the sample, so 10 per
   class is still a floor, not a comfortable size.

   **Current census fails the bar** because `n_lost = 0`, even though
   `n_won = 123`. Outcome: **INSUFFICIENT DATA**. No model is fit. No
   sklearn pickle. No coefficients. This is a successful Phase 6.

2. **Versioned artifact, does not rewrite history.**
   `intelligence/win_calibration_artifact.json` (`artifact_version`
   `0.1.0`) records the census, the thresholds, `fitted: false`,
   `coefficients: null`, and the held-out note
   “sample too small to trust”. It is separate from
   `scoring_model.json` `score_version` `1.0.0`. Changing either file
   does not rewrite stored Airtable/Supabase scores.

3. **Harness, not a replacement.** `intelligence/win_calibration.py`
   attaches an audit field `calibrated_win_probability` on
   `bid_intelligence`. Value is `null` and status is
   `INSUFFICIENT DATA` unless the bar is met (injected labeled rows in
   tests, or a later artifact that actually meets the bar). The
   predictor uses existing **deterministic bid_scorer factors**
   (FIT, deadline feasibility, known_client, eligibility) plus Phase 2
   organization history counts when those are present. LLM must not
   invent the probability. Code is official for this audit field.

4. **Do not replace heuristic WIN PROBABILITY.**
   `bid_scorer.py` WIN PROBABILITY (0–100 weighted heuristic) remains
   the official bid-decision input. Airtable `win_probability` continues
   to store that heuristic. Promotion would require a held-out Brier or
   log loss that **beats** the heuristic (`score/100` as a probability)
   on real labeled WON/LOST data. That comparison is not available:
   sample too small to trust. Even the test-only fit path must not flip
   the official source.

5. **Fail-open.** Malformed factors, missing org history, missing
   stores, or a malformed artifact → `calibrated_win_probability` stays
   null. No crash. Golden-set outcomes are never treated as WON/LOST.

6. **Held-out metrics.** When injected labeled rows meet the bar, an
   offline split computes Brier (and precision/recall at 0.5 if the
   test fold has both classes). When the bar is not met, the metric
   object is `null` with note “sample too small to trust” — including
   the production census of 123 WON / 0 LOST.

7. **No new Airtable fields.** Mentally running `check_schema.py`:
   `OPPORTUNITIES.win_probability` already holds the heuristic.
   Inventing `calibrated_win_probability` as an Airtable column would
   fail bulk writes. The audit field lives in analysis JSON only.

8. **Phase 7 is out of scope.** No execution state machine, retry
   queue, or orchestration split.

## Consequences

- Reviewers may see a null calibrated field next to a numeric heuristic
  WIN score. That is the product. The heuristic is still not a
  frequentist P(win).
- `n_won = 123` must not be advertised as “enough data.” There are
  **zero** verified Lost outcomes in local stores under Phase 2 rules.
- Bid Intelligence scorecard stays a heuristic (8): a harness exists;
  a trusted calibrated model does not.
- Golden-set extraction/scoring must not move.
- Phase 7 is not started here.

Pytest on 2026-09-15 (`~/cortech-bd-agent/cortech/bin/python -m pytest tests/ -q`):

```text
319 passed, 2 warnings in 16.95s
```

Golden-set extraction vs Phase 0–5 baseline: consultancy P/R/acc 1.000. Equal, not worse.
Held-out Brier on production labels: sample too small to trust (0 LOST).
