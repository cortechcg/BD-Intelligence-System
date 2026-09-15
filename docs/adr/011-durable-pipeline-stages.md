# ADR 011 — Durable pipeline stages and resume (Phase 7)

## Status

Accepted

## Context

This is Phase 7 of the S-Tier BD Intelligence OS upgrade: reliability /
architecture debt. It does **not** start Phase 8 (security/cost/observability
hardening), DNS-pinned HTTP, a spend cap, a second workflow ledger, or
replacement of the official heuristic WIN PROBABILITY.

Quoted **before** any stage module, migration, or resume test was written:

From `docs/FINAL_SYSTEM_AUDIT.md` **§16 Remaining technical debt**:

> - Schema validation and one repair retry for analyzer JSON are in place;
>   remaining extraction debt is live-LLM evaluation against the golden set,
>   not a second parser.
> - Store content hash uniqueness (`supabase_migration_content_hash.sql` — file
>   present, not applied this session), then extraction version/freshness and a
>   fuller lifecycle. Field-level ToR provenance exists. Named past-work
>   proposal-claim grounding exists (Phase 4 / ADR 008); a full source → page
>   → chunk → every-sentence graph does not.
> - Implement DNS-pinned HTTP transport and document-parser isolation if the
>   threat model warrants it.
> - Break `main.process_opportunity()` and the large proposal writer into tested
>   stage services.
> - Add durable execution records, retries, and failure recovery.
> - Complete LLM usage accounting and a configurable spend cap.
> - Replace direct Airtable/Supabase imports with dependency injection where it
>   materially improves testing.

Phase 7 implements only the stage-service, durable-execution, and
retry/dead-letter bullets. Schema validation, content_hash, provenance, and
named-claim grounding are already shipped. DNS pin, spend cap, and a broader
DI rewrite are Phase 8 / later.

From §19 (pre-Phase-7 evidence):

> Architecture | 6 | Coherent modular single process; lacks durable workflow
> boundaries and knowledge layer.

> Reliability | 6 | Isolated source failures, quality gates, bounded
> downloads; no durable retry/state system.

From §20:

> **Phase 7 (reliability / architecture debt) was not started here.**

There is already a durable lease/claim table `opportunity_processing`
(`supabase_migration_opportunity_state.sql`) with lease states
`pending | processing | failed | completed`. That table is the workflow
ledger. A second competing ledger would split ownership, break exact-URL
dedup, and make resume optional. This ADR extends that table.

CURSOR.md remains in force: `is_consultancy_contract` defaults True when
missing; consultancy FALSE stops before CV/proposal tokens; Airtable
`typecast=True`; `log_agent_action` never raises; `discovered_at` is
`strftime("%Y-%m-%d")`; models come from `config.py`; nothing submits to
clients.

## Decision

1. **One ledger.** Extend `opportunity_processing`. Do not create
   `opportunity_executions`, `pipeline_runs`, or any other workflow table.
   `opportunities_cache` remains a successful-content cache, never the
   source of truth for whether a stage finished.

2. **Two vocabularies, mapped (not renamed).** The existing CHECK on
   `state` is a *lease* machine and is not a synonym of the §16 pipeline
   stages. Keep it. Add `pipeline_stage` for the requested names.

   | Lease `state` (existing) | Meaning |
   |---|---|
   | `pending` | Row exists, not claimed. Maps to pipeline `discovered`. |
   | `processing` | This worker holds the lease. Progress is `pipeline_stage`. |
   | `failed` | Retryable. `pipeline_stage` + `checkpoint` are preserved so the next claim **resumes**. |
   | `dead_letter` | **New.** Drafting for a BID/WATCH item exhausted retries. Not silently skipped; not auto-retried. Manual `--submit-url` (`force=True`) may reprocess from `discovered`. |
   | `completed` | Agent work finished (or a terminal skip). Human stages may still advance. |

   | `pipeline_stage` (new, exact names) | Who writes it | Resume starts at |
   |---|---|---|
   | `discovered` | Insert / force reset | extract |
   | `extracted` | Agent, after fetch+quality (+ content-hash identity check) | score |
   | `scored` | Agent, after analyze + `bid_scorer` + consultancy/NO-BID gates (+ Airtable New for BID/WATCH) | draft |
   | `drafted` | Agent, after a usable EOI/proposal | reconstruct result; do not re-fetch or re-analyze |
   | `reviewed` | Human / Airtable only | not an agent stage |
   | `outcome` | Human / Airtable Won or Lost (win/loss poll) | not an agent stage |

   `reviewed` and `outcome` are **not** autonomous submit. The agent never
   posts to a client portal. Airtable `Reviewing` after a draft is still
   `drafted` (ready for humans). A later human status of Bidding/Submitted
   may be recorded as `reviewed`; Won/Lost is recorded as `outcome` and
   may skip `reviewed` because that is how Airtable is used today.

3. **Stage services, surgical extraction.**
   `intelligence/pipeline_stages.py` holds `run_extract_stage`,
   `run_score_stage`, `run_draft_stage`, and `run_opportunity_pipeline`.
   `main.process_opportunity()` still owns the lease (claim / complete /
   fail / dead-letter). The proposal writer is not rewritten; a thin stage
   `draft_bid_or_watch_proposal()` validates that BID/WATCH output has
   client-facing prose and raises `DraftingError` instead of returning an
   empty dict that the orchestrator would treat as success.

4. **Resume is mandatory when the ledger can store stages.**
   After a successful stage, persist `pipeline_stage` + `checkpoint`
   (extracted text, analysis JSON, Airtable id, draft payload) while the
   worker still owns the lease. A crash mid-run leaves `failed` or an
   expired `processing` lease; the next claim **must not** reset
   `pipeline_stage` / `checkpoint` unless `force=True`.

   Ledger / column policy (this is the Phase 7 core path, not fail-open
   that loses resume):

   - **Table missing** (existing rule): bulk discovery refuses to treat
     URLs as new; `--submit-url` may proceed without resume. Logged
     CRITICAL. Same as ADR 005 / `opportunity_ledger_available()`.
   - **Table present, stage columns/RPC missing** (this additive
     migration not applied): log CRITICAL that resume is unavailable,
     **still attempt the opportunity** (do not drop a BID/WATCH item),
     process from `discovered`. This is not silent fail-open.
   - **Table present, columns present:** persist and resume are
     mandatory. A persist failure is logged; the in-memory run may
     still finish, but the next process must try to load whatever was
     last successfully persisted.

5. **Retry vs dead-letter vs fail-open.**
   - Fetch / analysis / quality failures stay **retryable `failed`**
     (existing). They do not dead-letter on first error.
   - **Drafting after BID/WATCH:** empty draft, writer exception, or
     unusable sections → `DraftingError` → `failed` (stage remains
     `scored` so analysis is not repeated). After
     `MAX_DRAFT_FAILURES = 3` drafting failures → `dead_letter`.
     Never complete, never email a hollow draft, never silently skip.
   - Optional intel stays fail-open and **must not** block the bid:
     organizations table missing, market facts missing, calibrated
     P(win) INSUFFICIENT DATA, Airtable CRM blip, budget INSUFFICIENT
     DATA.
   - Consultancy gate FALSE (default True when the field is missing)
     still stops before CV/proposal tokens and is **terminal complete**,
     not a draft dead-letter.
   - Deterministic NO-BID remains a drafting-cost gate on a `New`
     record, terminal complete, human confirmation required.

6. **Additive migration only.**
   `supabase_migration_opportunity_stages.sql` adds `pipeline_stage`,
   `checkpoint`, `draft_fail_count`, lease state `dead_letter`, and RPCs
   `persist_opportunity_stage`, `dead_letter_opportunity_processing`,
   `record_human_pipeline_stage`, and replaces `claim_opportunity_processing`
   so reclaim preserves checkpoint (force resets to `discovered`).
   **Not applied** to a hosted project from this session. No new Airtable
   fields (`check_schema.py` would reject them).

7. **Out of scope (Phase 8 / later).** Spend cap, complete token
   accounting productization, DNS-pinned transport, parser sandbox,
   replacing `bid_scorer`, fitting a win model, expanding competitors,
   rewriting the email dashboard, `--submit-url` live runs.

## Consequences

- Kill-mid-run: persist `scored`, simulate crash, reclaim, resume at
  draft. Fetch and Claude analysis must not run again.
- Operators must apply `supabase_migration_opportunity_state.sql` **and**
  `supabase_migration_opportunity_stages.sql` for resume. Until the
  additive file is applied, the agent still attempts work and logs that
  resume is off.
- Duplicate review emails are possible if the process dies after
  `drafted` persist but before the caller sends mail; that is safer than
  re-spending proposal tokens.
- Architecture/Reliability scores move only with this evidence, not to
  an 8–10 distributed workflow engine.

Pytest on 2026-09-15 after this phase (`~/cortech-bd-agent/cortech/bin/python -m pytest tests/ -q`):

```text
333 passed, 2 warnings in 17.10s
```

Golden-set extraction vs Phase 0–6 baseline: consultancy P/R/acc 1.000. Equal, not worse.
Phase 8 was not started.
