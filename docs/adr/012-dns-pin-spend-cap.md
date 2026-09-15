# ADR 012 — DNS-pinned downloads, Playwright limits, CI audit, spend cap (Phase 8)

## Status

Accepted (implementation in-tree). Hosted `opportunity_processing` stage
columns must be present for the live spend-cap kill-test; applying
`supabase_migration_opportunity_stages.sql` is an operator step when the
process has no Postgres URL / Management API token.

## Context

Phase 7 shipped durable `pipeline_stage` resume (ADR 011). Phase 8 is the
security / cost / observability hardening called out in
`docs/FINAL_SYSTEM_AUDIT.md` §11–13 and §16:

- DNS-pinned HTTP transport on the download path (resolve once, connect to
  that IP, reject private ranges; no rebinding between check and connect).
- Resource-limited Playwright (timeouts and Chromium flags). This is **not**
  a full OS sandbox.
- Dependency vulnerability scanning in CI (`pip-audit`).
- Token/cost accounting already recorded at `complete()`; **enforce** a
  per-run spend cap that **halts further LLM `complete()` calls** once hit.

Quoted before this work:

> Remaining risks: DNS rebinding is not pinned at the network transport
> layer; Playwright executes a full browser against public pages; document
> parsers are not sandboxed; there is no dependency vulnerability scan in
> CI.

> Cost tracking remains incomplete … there is no enforced per-run or
> per-opportunity monetary budget.

> Complete LLM usage accounting and a configurable spend cap. **Phase 8 /
> later — not started.**

CURSOR.md remains in force: `is_consultancy_contract` defaults True when
missing; `typecast=True`; `log_agent_action` never raises; DATE fields
`strftime("%Y-%m-%d")`; models from `config.py`; `bid_scorer` heuristic is
not replaced; wrap_untrusted stays on untrusted document prompts.

## Decision

1. **Download path DNS pin.** `utils/dns_pinned_http.py` resolves the hop
   once, rejects any non-public address in the answer, connects to the
   chosen IP, and uses the original hostname for `Host` / TLS SNI.
   `processors/downloader.py` uses this for each hop instead of letting
   httpx re-resolve. Redirects still run `assert_safe_redirect` before the
   next pin.

2. **Playwright limits, honestly.** `prepare_browser_page()` sets default
   and navigation timeouts. Launch args add a V8 heap cap, renderer process
   limit, `--disable-dev-shm-usage`. `--no-sandbox` remains because this
   host cannot run a namespaced Chromium sandbox. Document parsers
   (pdfplumber / python-docx) stay in-process. Not gVisor, not a cgroup
   jail, not a proof against a malicious PDF.

3. **CI `pip-audit`.** `.github/workflows/ci.yml` installs requirements,
   runs `pip-audit -r requirements.txt`, then pytest.

4. **Enforced spend cap.** `MAX_RUN_COST_USD` (default 25, zero is a valid
   hard stop). `complete()` calls `assert_under_spend_cap()` **before**
   `messages.create()`. After a priced call, spent USD is added from
   `CLAUDE_PRICING_PER_MTOK`. Unknown usage or unknown model prices halt
   further calls rather than inventing a cost. `SpendCapError` is retryable
   on the Phase 7 ledger: last persisted `pipeline_stage` is kept, lease
   becomes `failed` with the error recorded, draft-fail count is **not**
   incremented (this is not a hollow BID/WATCH draft).

5. **Kill-test definition of done.** Cap `$0.00` (or `$0.01` after one
   tiny priced call). No further provider `create()` calls. Hosted
   `opportunity_processing.pipeline_stage` is the last successful stage
   (`scored` in the seeded-draft case). Resume/claim continues from that
   stage on the same real row.

## Consequences

- Operators apply `supabase_migration_opportunity_stages.sql` via
  `scripts/apply_sql_migration.py` (needs `DATABASE_URL` /
  `SUPABASE_DB_PASSWORD` / `SUPABASE_ACCESS_TOKEN`) or the Dashboard SQL
  editor. PostgREST cannot run DDL. The service role key is not a Postgres
  password.
- Playwright and PDF/DOCX parsers are still a local process with a full
  browser. Treat public tender URLs as hostile; the pin covers the
  **download** path, not every Playwright subresource DNS lookup (those
  still use the URL policy + request guard).
- A `$0` cap blocks analysis as well as drafting if the run starts from
  `discovered`. The live kill-test seeds `scored` so the halt is mid-draft.
- Scores in the audit scorecard move only with the evidence below, not to
  an 8–10 security product.
