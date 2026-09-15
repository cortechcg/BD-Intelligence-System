# ADR 007 — Observed-data market digest (Phase 3)

## Status

Accepted

## Context

Phase 0 shipped a 36-item golden set (ADR 004). Phase 1 shipped schema-validated
extraction, `content_hash` uniqueness (migration file, not applied), and
field-level ToR provenance (ADR 005). Phase 2 shipped canonical client/donor
organizations, a deterministic matcher, and a cited roll-up on the review
email (ADR 006; migration file not applied). This ADR is Phase 3 of the S-Tier
upgrade: **market views from opportunities already discovered and stored**.
It does **not** start Phase 4 (proposal claim verification), Phase 5
(competitors), relationship graphs, or a calibrated win model.

Scorecard / recommended-next-phase lines this ADR implements (quoted, not
paraphrased):

- §19 Market Intelligence **0/10**: “No observed-data trend product.”
- §20: “Build **market** views from those observed records before attempting
  competitors, relationships, or strategic recommendations. Phase 3 (market
  intelligence) was not started here.”
- Audit philosophy for this gap: a trend product must be *observed-data*
  reporting from opportunities already discovered and stored — not external
  market research. This system has no external market-research data source
  and must not pretend to.

Client views (Phase 2) come before market views; competitors/relationships
come later. Airtable remains the human CRM, not the source of truth. Mentally
running `check_schema.py`: OPPORTUNITIES already has `thematic_areas`,
`location`, `donor`, and `discovered_at`. Inventing a new Airtable field
(for example a trend-id or region-bucket column) would fail bulk writes.
This phase therefore aggregates stored rows in code and emails an internal
digest. No new Airtable fields. No public dashboard. No LLM market narrative.

## Decision

1. **Code aggregation, not Claude.** Frequencies, cadence counts, and
   geography shares are computed in `intelligence/market_trends.py` from
   stored records. No new LLM call. Missing → UNKNOWN / INSUFFICIENT DATA.
   A thin sample is never labelled INFERRED-as-trend.

2. **Source of truth and fail-open.** Prefer Supabase `opportunities_cache`
   (optional observed-fact columns in `supabase_migration_opportunity_facts.sql`,
   **not applied** from this session). Airtable OPPORTUNITIES is a secondary
   source using fields that already exist (`thematic_areas`, `location`,
   `donor`, `discovered_at`). Missing tables/columns → empty load, honest
   insufficient digest, no crash. Do not apply hosted migrations. Do not
   invent posting dates; use `discovered_at` or a real stored timestamp
   (`created_at` on cache rows). `submission_deadline` is not a discovery
   date. DATE values written to Airtable stay `strftime("%Y-%m-%d")`; this
   phase prefers not to write Airtable at all.

3. **Windows and sample-size rule (conservative).** Trailing **30** and **90**
   calendar-day windows, inclusive of `as_of`:
   `[as_of - (window_days - 1), as_of]`.

   - `TREND_MIN_N = 10` dated stored rows in a window is the minimum to call
     that window a **trend** (theme frequencies as percentages, geography
     distribution shift, donor posting **cadence**).
   - `n < 10` (including the n=2 failure mode) renders
     **“insufficient data for a trend”**. Evidence is INSUFFICIENT DATA,
     not VERIFIED-as-trend and not INFERRED-as-trend. Raw percentages,
     charts, and cadence intervals are withheld.
   - Every number that *is* shown states its sample size and date range
     inline. VERIFIED means a count of stored rows, e.g.
     `VERIFIED (n=12 stored rows, 2026-08-17 to 2026-09-15)`.

4. **Labels are stored strings, not a taxonomy.** Thematic and geography
   values are counted as they appear on the row after strip/casefold
   grouping. Do not invent buckets (“Other WASH”, “East Africa”) or dummy
   donors. Empty / placeholder / non-string values are skipped and not
   counted as a theme. Malformed rows are skipped, not coerced.

5. **Donor cadence only for organizations already in the Phase 2 table.**
   Match stored `donor` strings with `match_organization` against the loaded
   index (`persist=False`). New candidates and empty names are not cadence
   rows. If the organizations table is missing, unapplied, or empty →
   fail-open: “insufficient data”, do not invent donors from free text.

6. **Delivery is a fifth systemd timer**, same local `systemd --user`
   pattern as the existing four (`cortech-discovery`, `cortech-assortis`,
   `cortech-deadline`, `cortech-winloss`): `Type=oneshot`, `Persistent=true`,
   `main.py` CLI flag, Gmail SMTP then Resend, human-in-the-loop email to
   `EMAIL_RECIPIENTS`. Unit files:

   | Timer | Fires | Runs |
   |---|---|---|
   | `cortech-market.timer` | Weekly, Monday ~08:30 | `main.py --run-market-digest` |

   Service/timer templates: `deploy/systemd/cortech-market.service` and
   `cortech-market.timer`. Copy into `~/.config/systemd/user/` and
   `systemctl --user enable --now cortech-market.timer` when an operator
   wants the weekly send. This session does not enable the timer or send a
   live digest. Tests mock SMTP. Do not send a live digest during development.

## Consequences

- Market Intelligence becomes a real, tested observed-data digest with
  thin-n refusal, not a research product. Live history is only as complete
  as applied migrations and dated stored rows. Unapplied
  `opportunity_facts` / `organizations` migrations → fail-open empty or
  Airtable-only secondary fields → often INSUFFICIENT DATA, which is honest.
- Unapplied DB and empty history keep the scorecard well below 8–10.
- Phase 4 (proposal claim verification), competitors, relationship graphs,
  and a calibrated win model remain out of scope and are not stubbed.
- Golden-set offline extraction accuracy must stay at the Phase 0/1/2
  baseline (consultancy P/R/acc 1.000 on recorded JSON). This phase does
  not change the analyzer schema.

Pytest (`./cortech/bin/python -m pytest tests/ -q`) on 2026-09-15:

```text
2 failed, 278 passed in 14.62s
```

The two failures are pre-existing `tests/test_grounding.py` cases, not Phase 3.
Golden-set extraction vs Phase 0/1/2 baseline: consultancy P/R/acc 1.000,
client/deadline exact 1.000, budget MAE 0.000, geography/thematic Jaccard 1.000.
Equal, not worse. `supabase_migration_opportunity_facts.sql` was **not** applied
to a live project in this session. No live digest email was sent.
