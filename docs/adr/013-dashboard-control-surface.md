# ADR 013 — Dashboard control surface on Render (Phase 9 scope: control, not logic)

## Status

Accepted, **not deployed**. Built and verified locally against the hosted
Supabase project on 2026-09-21. No Render account or Google OAuth client was
available in the build environment; the deploy runbook is `docs/DASHBOARD.md`
§3 and the live-login check is explicitly outstanding.

## Context

The system ran only via systemd timers or the `main.py` CLI. The BD team asked
for a web surface to trigger processing on demand and watch it move through
`discovered → extracted → scored → drafted`. The brief's constraints carry
over unchanged: never invent data; no autonomous submission; reuse the
pipeline, don't reimplement it; Phase 8 spend cap applies; Google OAuth
restricted to `@cortechconsultinggroup.com`; every score shown with its
provenance.

Quoted before any code (`docs/ARCHITECTURE.md`, "Not in this architecture"):

> Competitor intelligence as a ranking/likely-bidder product, external
> market-research products, a full relationship graph / account CRM,
> executive-brief products, knowledge-graph services, **a UI**, or extra LLM
> agent loops.

This ADR adds the UI item and nothing else on that list.

Facts established before design decisions (all read-only against production):

* `opportunities_cache` 1212 rows; `opportunity_processing` 8 rows, 4 at
  `drafted`; `opportunities_cache.analysis_json` populated **0/1212** — so the
  score breakdown must come from `opportunity_processing.checkpoint`, which
  holds the full `bid_intelligence`.
* The `supabase_realtime` publication contains **zero tables**. Realtime
  would ack a subscription and deliver nothing.
* `DATABASE_URL` in `.env` gives working DDL access (PostgreSQL 17.6).
* The requesting user's own account is `@gmail.com`, i.e. a strict domain
  lock would exclude the person commissioning the tool.

## Decision

1. **Same entry point, not a parallel one.** Every dashboard action calls
   `main.submit_single_url()` — the function `main.py --submit-url`
   dispatches to. It gained two keyword-only parameters with the historical
   defaults (`force=True`, `retry_dead_letter=False`) and returns its result
   dict. `tests/test_dashboard_parity.py` compares CLI vs dashboard output
   for the same opportunity: equal in every field except the per-run
   `execution_id`, and pins that the exclusion list cannot quietly grow.

2. **A request queue, not a second ledger.** `dashboard_triggers` records
   that a human asked for a run, who, what it cost, and what became of the
   request. Pipeline state stays solely in `opportunity_processing`
   (ADR 011 §1). The queue is a table plus RPCs with the same lease/token
   discipline as `claim_opportunity_processing`; no broker.

3. **Two Render services, both stateless.** Web (`runtime: python`, 0.5c-512mb
   $7/mo) and worker (`runtime: docker`, 1c-2g $25/mo — Chromium and the PDF
   toolchain need the memory). The existing systemd discovery timers are
   **not** replaced; a Render Cron alternative is documented but off.

4. **Polling, not Realtime.** Because the publication is empty, and because
   polling through the authenticated backend keeps the service-role key
   server-side. 15 s on lists, 5 s while a job is in flight.

5. **Auth = Google OAuth, verified ID token, domain check, plus an explicit
   exception list.** `AUTH_ALLOWED_EMAILS` (default empty; full addresses
   only, never a second domain) is the documented accommodation for the
   off-domain team member, decided with the user on 2026-09-21. The
   allowlist is re-checked on every request, so removal is immediate.
   `email_verified` is required — otherwise an unverified
   `@cortechconsultinggroup.com` address would pass.

6. **Missing data renders as an em dash with its reason.** One Jinja macro
   (`value()`) is the sole render path for fields; `calibrated_win_probability`
   is shown *and empty* ("INSUFFICIENT DATA: 123 WON / 0 LOST"), never
   hidden; WIN PROBABILITY carries "heuristic (see docs/SCORING_MODEL.md)"
   inline via the same primitive so a layout change cannot separate them.
   The design rationale, including why UNKNOWN is grey and never red and why
   BID/WATCH/NO-BID get weight and shape rather than hue, is
   `docs/DASHBOARD_DESIGN.md`.

7. **Interactive controls (addendum), each mapped or declined explicitly:**

   | Ask | Decision |
   |---|---|
   | Bulk "Draft selected" | One `dashboard_triggers` row per URL; the request only inserts rows; the worker drains them |
   | Live sub-step progress | **Declined.** The pipeline persists stage boundaries only; sub-steps exist in stdout logs. Stage + heartbeat + live spend are shown and the UI says why nothing finer is |
   | Live spend vs cap | Worker heartbeat publishes `utils.observability`'s run bucket — the very dict the cap enforces against — every 10 s. Needed a process-level registry (`latest_run_spend_snapshot`) because the bucket is a ContextVar invisible to the heartbeat thread |
   | Retry dead-letter from checkpoint | **Amends ADR 011 §2**, which allowed only `force` (from `discovered`) for `dead_letter`. One optional parameter `p_retry_dead_letter` on the *same* claim RPC keeps the checkpoint and resets `draft_fail_count`; default FALSE is byte-identical to the previous function. Not a second claim path |
   | Cancel | Reuses the one cooperative halt point the pipeline has — `assert_under_spend_cap()` before every `complete()` — via `request_run_halt()`. `RunHaltRequested` subclasses `SpendCapError` so ADR 012 §4's handler applies unchanged: retryable, stage kept, no draft-fail increment. Cannot interrupt an in-flight call; the UI copy says so and a test pins copy to mechanism |
   | Re-run from stage | `rewind_opportunity_stage(url, extracted|scored)` edits `pipeline_stage` and strips exactly the checkpoint keys later stages own (mirrors `OpportunityContext.to_checkpoint`), then the ordinary resume path runs. Refused under an active lease |
   | Per-user finish email | **Declined.** `send_proposal_email()` has no per-recipient hook; a second channel would drift. The existing email goes to `EMAIL_RECIPIENTS` as for cron; browser notification + `requested_by` on the row cover the individual |
   | Aggregate cap | **New control** — the pipeline had none; each `--submit-url` is its own `$25` run. `DASHBOARD_AGGREGATE_CAP_USD` over a trailing window, enforced by the worker at claim time against recorded spend (finished, failed and cancelled). Unknown-cost runs count as over, as with the per-run cap |

8. **Additive migrations, applied.** `supabase_migration_dashboard_triggers.sql`
   and `supabase_migration_dashboard_controls.sql`, both idempotent, both
   applied via `scripts/apply_sql_migration.py` on 2026-09-21. The controls
   file replaces `claim_opportunity_processing` with a superset signature;
   the seven pre-existing hosted stage tests pass against it.

9. **Dependencies.** `fastapi`, `starlette`, `uvicorn`, `jinja2`,
   `itsdangerous`, `python-multipart` (+ `click`, `MarkupSafe`). No OAuth
   framework (httpx + PyJWT were already present), no ORM, no task queue.
   `pip-audit` clean after bumping `anyio`, `soupsieve`, `pydantic`, whose
   CVEs post-date ADR 012's scan.

## Consequences

* `main.py` changed in one function (`submit_single_url`: two keyword-only
  args, a return value) and one signature (`process_opportunity` gains
  `retry_dead_letter`). `utils/errors.py` gained `RunHaltRequested` and
  `ErrorType.CANCELLED`; `utils/observability.py` gained a process-level
  spend registry and `request_run_halt()`. No scoring, drafting, extraction
  or discovery code changed. Golden extraction/scoring baseline unchanged.
* A dead-lettered opportunity can now be resumed by a human without paying
  for extraction and analysis again. Automatic behaviour is unchanged.
* Cancelled and cap-halted runs both leave `state=failed`; the dashboard
  distinguishes them by the recorded reason. Cost incurred before a cancel
  is recorded and counts toward the aggregate cap.
* The review email is sent from dashboard runs exactly as from CLI runs,
  to `EMAIL_RECIPIENTS`. There is one switch for that, not two.
* Deploy and live login remain to be done with real credentials. Until then
  the OAuth restriction is proven by 37 unit tests over the pure decision
  function, not by a browser.
* Found, not fixed (needs a decision): `database/market_store.py` selects a
  non-existent `created_at`, so the Phase 3 digest has **zero dated rows** in
  production and always reports insufficient data. See `docs/DASHBOARD.md` §7.

Verification 2026-09-21: offline `494 passed` (py3.12 and py3.14); hosted
`16 passed` across the three live files; `pip-audit` clean; golden 30 passed.
