# ADR 006 — Canonical client/donor organizations (Phase 2)

## Status

Accepted

## Context

Phase 0 shipped a 36-item golden set (ADR 004). Phase 1 shipped schema-validated
extraction, `content_hash` uniqueness (migration file, not applied), and
field-level ToR provenance (ADR 005). This ADR is Phase 2 of the S-Tier upgrade:
**client/org views from observed records**. It does **not** start Phase 3
(market intelligence), Phase 5 (competitors), relationship graphs, or a
calibrated win model.

Scorecard / recommended-next-phase lines this ADR implements (quoted, not
paraphrased):

- §19 Client Intelligence **2/10**: “Client fields/retrieval context exist; no
  canonical account profile.”
- §20: “Build client and market views from those observed records before
  attempting competitors, relationships, or strategic recommendations”
- Scope of this phase: client/org views only. Not market (Phase 3). Not
  competitors (Phase 5).

Airtable remains the human CRM, not the source of truth. A new Airtable field
that is not already in `check_schema.py` (`OPPORTUNITIES.client` / `.donor`
already exist; there is no organization-id column) would fail bulk writes.
This phase therefore puts the canonical model in Supabase and surfaces the
roll-up on the review email. No new Airtable fields.

Golden-set outcomes are all `UNKNOWN`. They must not be dressed as wins.

## Decision

1. **Supabase `organizations` + `organization_aliases` +
   `organization_observations`** (additive migration
   `supabase_migration_organizations.sql`). Do not apply it to a hosted
   project from this session. The Python client fail-opens (empty index, no
   crash, review email still renders) if a table/column is missing, same
   pattern as `content_hash`.

2. **Matching, not guessing.** Resolve extracted `opportunity.client` and
   `opportunity.donor` against a normalized name index. No new LLM call.
   `rapidfuzz` is not a current dependency; matching uses stdlib
   `difflib.SequenceMatcher` plus documented normalization.

   Normalization (deterministic):

   - Unicode NFKC + casefold
   - control characters stripped; printable text only
   - punctuation → space; whitespace collapsed
   - length cap 200 characters on stored/display names
   - trailing legal-entity tokens stripped only when other tokens remain:
     `ltd`, `limited`, `inc`, `incorporated`, `llc`, `plc`, `gmbh`, `bv`,
     `sa`, `pty`, `co`, `corp`, `corporation`
   - compact form = spaced form with spaces removed

   Match order, first hit wins:

   | Method | Condition | Label | Confidence |
   |---|---|---|---|
   | empty / placeholder (`unknown client`, `n/a`, …) | no usable name | UNKNOWN | 0 |
   | `explicit_alias` | alias stored with `alias_source=explicit` | VERIFIED | 1.00 |
   | `exact_spaced` | identical normalized spaced form | VERIFIED | 1.00 |
   | `exact_compact` | identical compact form and compact length ≥ 6 | VERIFIED | 1.00 |
   | `fuzzy` | compact length ≥ 10, `abs(len diff) ≤ 2`, first spaced token equal, compact `SequenceMatcher.ratio() ≥ 0.95` | INFERRED | the ratio |
   | `new_candidate` | nothing at/above threshold | UNKNOWN vs existing orgs; create a **new candidate** entity | 0 |

   **Alias policy (honest):** production ships with an **empty** explicit-alias
   list (`intelligence/organization_aliases.json`). There is no implicit
   acronym expansion. `UNICEF` does not merge with `United Nations Children's
   Fund`. `DCA Kenya` does not merge with `DanChurchAid` unless an operator
   later stores that pair as `explicit`. Spacing/punctuation/case identity
   (`Acme Consulting` vs `ACME  CONSULTING`, `DanChurchAid` vs
   `Dan Church Aid` via compact form) is matching, not guessing.

   Below threshold: never force-match. Create a new candidate or leave the
   extracted name UNKNOWN. Two different golden-set clients (`Client A` vs
   `Client B`) stay two entities.

3. **Roll-up is aggregation of stored rows, not LLM inference.** Counts come
   from `organization_observations` plus, when those tables exist, already-stored
   `proposal_embeddings` (distinct assignment) and `win_loss_memory` rows whose
   client/donor names match the **same** matcher. Code numbers are official.

   Outcome rules:

   - `win_loss_memory.outcome` in `{Won, Lost, WON, LOST}` → WON/LOST VERIFIED
   - `proposal_embeddings.won is True` → WON VERIFIED
   - `proposal_embeddings.won is False` or missing → **UNKNOWN**, not Lost.
     Airtable’s won checkbox being unchecked is indistinguishable from “not
     filled.”
   - Golden-set items are not a live observation store; their outcomes stay
     UNKNOWN and are not ingested as wins.

   Missing outcomes increment an UNKNOWN count. They are not zeros presented
   as a win rate. The current opportunity is excluded from “before” counts.

   `known_client` from `bid_scorer.py` is attached as already-computed name
   overlap with `CORTECH_PROFILE` / `scoring_model.json` `known_clients`. It is
   labelled as name overlap, not a verified relationship (ADR 001 / scoring
   model).

4. **Review email** (`reporting/email_report.py`) shows grounded copy, HTML-
   escaped, cited to specific past `source_kind` + `source_id` + title:

   - “Cortech has bid on N opportunities from this client before, won X,
     lost Y”
   - If N=0, say so
   - If outcomes unknown, say so
   - Nothing submits autonomously

5. **Untrusted names.** Tender-extracted names are untrusted data. They are
   sanitized/length-capped in storage. `org_name_for_prompt()` wraps with
   `wrap_untrusted()` wherever a document-derived org name is placed in an LLM
   prompt. Matching itself does not call an LLM.

6. **No Airtable schema change.** Mentally running `check_schema.py`:
   OPPORTUNITIES already has `client` and `donor`; PAST_PROPOSALS already has
   `client`, `donor`, `won`. Inventing `organization_id` would be a new field
   and would fail `check_schema.py`. Airtable is not updated with org ids.

## Consequences

- Client Intelligence becomes a real, tested matcher + cited roll-up, not an
  account-management product. Live history is only as complete as applied
  migrations and stored proposal/win-loss rows. Unapplied migration → fail-open
  empty index → N=0 and UNKNOWN, which is honest.
- Compact-form identity will treat `DanChurchAid` and `Dan Church Aid` as one
  org. That is documented spacing identity, not an acronym table.
- Fuzzy matching is conservative (0.95, length window, first-token guard) so
  `Client A` / `Client B` and `Humanitarian Relief Agency` /
  `Humanitarian Relief Association` do not silently merge.
- Phase 3 (market views), competitors, relationship graphs, and a calibrated
  win model remain out of scope and are not stubbed.
- Golden-set offline extraction accuracy must stay at the Phase 0/1 baseline
  (consultancy P/R/acc 1.000 on recorded JSON). This phase does not change the
  analyzer schema.

Pytest (`./cortech/bin/python -m pytest tests/ -q`) on 2026-09-14:

```text
2 failed, 255 passed in 14.54s
```

The two failures are pre-existing `tests/test_grounding.py` cases, not Phase 2.
Golden-set extraction vs Phase 0/1 baseline: consultancy P/R/acc 1.000,
client/deadline exact 1.000, budget MAE 0.000, geography/thematic Jaccard 1.000.
Equal, not worse. `supabase_migration_organizations.sql` was **not** applied
to a live project in this session.
