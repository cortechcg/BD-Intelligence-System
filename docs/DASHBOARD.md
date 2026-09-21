# Cortech BD dashboard

A control surface over the existing pipeline. It lets the BD team trigger
opportunity processing on demand, follow a run through
`discovered → extracted → scored → drafted`, retry, cancel, or re-run from a
stage, and see the portfolio — all against the real Supabase tables. It is
not a new system: every action is a call into the same functions `main.py`
already uses, and every number on screen is a real column or a real absence.

Design reasoning: `docs/DASHBOARD_DESIGN.md`. Decision record: `docs/adr/013-dashboard-control-surface.md`.

---

## 1. What it does, and how each action maps onto the pipeline

| UI action | What actually runs | Ledger effect |
|---|---|---|
| **Submit a URL** | `main.submit_single_url(url)` — the exact function `python main.py --submit-url` dispatches to, same defaults | `force=True`: row reset to `discovered`, full pipeline |
| **Draft this / Draft selected** | `main.submit_single_url(url, force=False)` | Phase 7 resume from the persisted `pipeline_stage`/`checkpoint` |
| **Retry** (dead-lettered) | `main.submit_single_url(url, force=False, retry_dead_letter=True)` | Claims the `dead_letter` row **without** resetting it; `draft_fail_count` → 0; resumes from checkpoint (`scored`) |
| **Re-run from scored** | `rewind_opportunity_stage(url, "scored")` then `submit_single_url(url, force=False)` | Drops draft artefacts from the checkpoint, keeps analysis; re-drafts only |
| **Re-run from extracted** | `rewind_opportunity_stage(url, "extracted")` then `submit_single_url(url, force=False)` | Keeps extracted text, drops analysis and draft; re-scores then re-drafts |
| **Cancel** | `utils.observability.request_run_halt()` via the worker heartbeat | Pipeline's own spend-cap check raises `RunHaltRequested` before the next model call; row left `failed` at the last persisted stage with checkpoint intact; retryable |

`submit_single_url` gained two keyword-only arguments (`force`, `retry_dead_letter`) whose defaults are the historical CLI behaviour, and now returns its result dict. That is the entire change to `main.py`. `tests/test_dashboard_parity.py` proves the CLI and the dashboard produce identical pipeline results for the same opportunity — 17 of 18 result fields byte-equal, the 18th being the per-run `execution_id`.

**Nothing here can act outward.** There is no route that sends email to a client, submits to a portal, or writes `reviewed`/`outcome` — those remain human Airtable transitions (ADR 011 §2). `tests/test_dashboard_views.py` pins the POST route list and asserts the web app never imports the email senders.

### The review email

`submit_single_url` sends the internal review email to `EMAIL_RECIPIENTS` at `drafted`, exactly as the CLI does. The dashboard does **not** add a per-user email: `send_proposal_email()` has no per-recipient hook, and a second channel would drift from the first. The triggering user gets that email iff they are on `EMAIL_RECIPIENTS` (normally the whole team). They also get a browser notification from the job page if the tab is open, and their address is on the `dashboard_triggers` row as the audit trail. Leave `EMAIL_RECIPIENTS` empty to suppress the email for dashboard *and* cron runs alike — there is deliberately no separate switch.

### What the live view can and cannot show

Stage-level progress is real: the detail and job pages re-read `opportunity_processing.pipeline_stage` every 5 s while a job is in flight. **Sub-step progress ("drafting section 3 of 8") is not shown** because the pipeline does not persist it — sub-steps go to loguru stdout only, and inventing them would violate the first rule of this repo. Live **spend** is real: the worker's heartbeat publishes the run's own `utils/observability` bucket (the same one the cap is enforced against) every 10 s.

---

## 2. Architecture

```
Browser ──HTTPS──▶ cortech-bd-web (FastAPI, stateless)
                    │  Google OAuth (httpx + PyJWT), signed cookie (itsdangerous)
                    │  reads: opportunity_processing, opportunities_cache,
                    │         organizations*, proposal_embeddings, dashboard_triggers
                    │  writes: dashboard_triggers only (enqueue / cancel)
                    ▼
                 Supabase (existing project) ◀── dashboard_triggers is the queue
                    ▲
                    │  claim / heartbeat(spend, cancel?) / finish
cortech-bd-worker ──┘  (Docker: Chromium + PDF toolchain)
   └── main.submit_single_url(...)  ← identical to `main.py --submit-url`
```

* **Web service** — stateless. Every piece of real state is in Supabase, so it can restart or scale freely. It never fetches a tender or calls a model.
* **Worker** — a separate Render Background Worker. Claims one `dashboard_triggers` row at a time (`FOR UPDATE SKIP LOCKED`), runs it, finalises it. Lease + token discipline mirrors `claim_opportunity_processing`. A crashed worker's job is reclaimed after `DASHBOARD_WORKER_LEASE_SECONDS`.
* **Queue** — a Supabase table plus RPCs (`supabase_migration_dashboard_triggers.sql`, `supabase_migration_dashboard_controls.sql`). No broker. A partial unique index refuses a second in-flight request for the same URL at the database, not merely in the UI.
* **Realtime vs polling** — polling. Verified on 2026-09-21: the project's `supabase_realtime` publication exists but contains **zero tables** (`select * from pg_publication_tables` returns only `realtime.messages_*`), so a `postgres_changes` subscription acks `SUBSCRIBED` and never delivers a row. Polling through the authenticated backend also keeps the service-role key off the browser entirely. If Realtime is wanted later: `alter publication supabase_realtime add table opportunity_processing, dashboard_triggers;` — and note the anon key would then need RLS policies it does not have today.
* **Scheduler** — **the existing systemd timers keep running as-is.** The dashboard adds manual triggering; it does not replace scheduled discovery. A commented Render Cron block in `render.yaml` shows the alternative; enabling it *and* leaving the timers on would run discovery twice and double-spend against the cap, so it is opt-in and documented, not on.

### Spend caps — two of them

1. **Per run** — `MAX_RUN_COST_USD` (ADR 012), enforced inside `complete()` before every provider call. A dashboard run is an ordinary run; it gets no exemption. A cap halt leaves the ledger `failed` at the last stage, retryable, not counted as a draft failure.
2. **Aggregate** — `DASHBOARD_AGGREGATE_CAP_USD` over `DASHBOARD_AGGREGATE_WINDOW_HOURS` (default 100 USD / 24 h). **This is a control the pipeline did not have:** cron's `$25` covers one whole discovery run, but each `--submit-url` — hence each dashboard job — is its own run with a fresh `$25`. Forty clicks on *Draft selected* would otherwise be forty caps. The gate is enforced by the **worker** at claim time against spend actually recorded on trigger rows (finished, failed *and* cancelled); the UI only mirrors it. Unknown-cost runs count as over, the same rule the per-run cap applies. `tests/test_dashboard_controls.py::test_bulk_trigger_of_forty_jobs_is_bounded_by_the_aggregate_cap` drives forty queued jobs through the worker and asserts the cut-off.

### Cancel — what it really does

Rendered verbatim in the UI (`dashboard/app.py::CANCEL_COPY`) and asserted against the code by `test_cancel_copy_matches_the_mechanism`:

> Cancel is cooperative, not instant. A job that has not started is withdrawn immediately and the worker never sees it. A job that is running is flagged; the worker's next heartbeat (every 10s) asks the pipeline to halt before its next model call. The model call already in flight finishes — up to ANTHROPIC_TIMEOUT_SECONDS — and its cost is incurred and recorded. The opportunity keeps its last saved stage and checkpoint and can be resumed. Cancelled spend still counts toward the aggregate cap.

Mechanism: `RunHaltRequested` subclasses `SpendCapError`, so the pipeline's one existing cooperative halt point and one existing handler (ADR 012 §4) do the work. No second interruption mechanism was added.

---

## 3. Deploy runbook (Render)

**Status: not deployed by me.** This environment had no Render account, no Render API key, and no Google Cloud OAuth client. Everything below was verified locally against the real Supabase project; the last two steps require your credentials.

### 3.1 Apply the migrations (done on 2026-09-21 — verify, don't re-run blindly)

Both are applied to the hosted project and are idempotent:

```
python scripts/apply_sql_migration.py supabase_migration_dashboard_triggers.sql
python scripts/apply_sql_migration.py supabase_migration_dashboard_controls.sql
```

Check: `python -m pytest tests/test_live_dashboard_queue.py -o addopts=""` → 9 passed.

### 3.2 Google Cloud — OAuth client

1. console.cloud.google.com → APIs & Services → Credentials → *Create credentials* → *OAuth client ID* → **Web application**.
2. Authorised redirect URI: `https://<your-render-web-url>/auth/callback` — exactly, no trailing slash. Add `http://127.0.0.1:8000/auth/callback` too if you want local login.
3. OAuth consent screen: **Internal** user type if the Google Workspace is `cortechconsultinggroup.com` (then only workspace accounts can even reach the consent screen — a second wall in front of the app's own check). External works too; the app rejects off-domain emails regardless.
4. Copy client ID and secret into Render as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

### 3.3 Render — Blueprint

1. Render dashboard → *New* → *Blueprint* → connect this repo → it reads `render.yaml`.
2. Fill the `sync: false` secrets on **both** services (Supabase, Anthropic, OpenAI, Airtable; email on the worker; Google on the web).
3. Set `PUBLIC_BASE_URL` on the web service to the URL Render assigns (`https://cortech-bd-web.onrender.com` unless renamed). It must match step 3.2 exactly.
4. Deploy. Web health check is `/healthz`.

### 3.4 Verify login end to end

1. Open the web URL → redirected to Google → sign in with an `@cortechconsultinggroup.com` account → land on the Queue.
2. Sign in with a personal Gmail → expect **HTTP 403** with the text *Access denied — `<addr>` is not on @cortechconsultinggroup.com and is not in AUTH_ALLOWED_EMAILS.* (The unit test for this decision is `tests/test_dashboard_auth.py::test_non_domain_email_is_rejected`, eight variants including suffix/prefix/subdomain attacks.)
3. Submit a real tender URL → land on `/job/<id>` → watch it move through the stages → download the draft. The worker log on Render shows `claimed trigger … finished: succeeded`.

### 3.5 Off-domain team member

Per the decision taken on 2026-09-21: access is `@cortechconsultinggroup.com` **or** an explicit entry in `AUTH_ALLOWED_EMAILS` (comma-separated full addresses; default empty). The list lives in the Render env, visible in review — it is not a second domain and cannot become one. Removing an address locks that person out on their next request, not when their cookie expires.

### 3.6 Cost (read from render.com/pricing on 2026-09-21)

| Item | Plan | Monthly |
|---|---|---|
| Workspace | Hobby | $0 (Pro is $25/mo and is not needed) |
| `cortech-bd-web` | `starter` = 0.5 CPU / 512 MB | **$7** |
| `cortech-bd-worker` | `standard` = 1 CPU / 2 GB | **$25** — Chromium + pdfplumber + the 512 MB V8 heap cap do not fit in 512 MB; workers have no free tier |
| **Total** | | **$32/mo** + LLM spend, which the two caps bound |

Cheaper option: web on `free` ($0) → **$25/mo**, but the web spins down after 15 idle minutes and the first request each time waits ~1 minute. For a tool used a few times a day that is a real annoyance; $7 is the recommendation. Prices are pro-rated per second; Render renamed plans in August 2026 (`starter`→`0.5c-512mb`, `standard`→`1c-2g`), old names still work.

---

## 4. Environment variables

**Required (web):** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AIRTABLE_API_KEY`, `AIRTABLE_BASE_ID` (validated at import by `config.require_env`), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `DASHBOARD_SESSION_SECRET`, `PUBLIC_BASE_URL`.

**Required (worker):** the full pipeline environment — everything cron has, including `MAX_RUN_COST_USD` and the email settings.

**Optional:** `AUTH_ALLOWED_DOMAIN` (default `cortechconsultinggroup.com`), `AUTH_ALLOWED_EMAILS` (default empty), `DASHBOARD_AGGREGATE_CAP_USD` (100), `DASHBOARD_AGGREGATE_WINDOW_HOURS` (24), `DASHBOARD_BULK_MAX` (40), `DASHBOARD_HEARTBEAT_SECONDS` (10), `DASHBOARD_POLL_MS` (15000), `DASHBOARD_TRIGGERS_ENABLED` (true), `DASHBOARD_COOKIE_SECURE` (true — false only for local http), `DASHBOARD_SESSION_MAX_AGE` (43200), `DASHBOARD_WORKER_POLL_SECONDS` (5), `DASHBOARD_WORKER_LEASE_SECONDS` (3600).

---

## 5. Local run

```
export DASHBOARD_SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... PUBLIC_BASE_URL=http://127.0.0.1:8000
export DASHBOARD_COOKIE_SECURE=false
uvicorn dashboard.app:app --port 8000            # web
python -m dashboard.worker                        # worker, second terminal
```

Design probe (Phase 1 screen from real rows, no server, no auth):
`python dashboard/design/build_preview.py` → `dashboard/design/queue_preview.html` (git-ignored: it contains real tender titles).

---

## 6. Verification record (2026-09-21)

* Offline suite: **494 passed, 0 failed** on Python 3.12 (`.venv`) and 3.14 (`cortech/`), up from 370 at the last recorded baseline. Dashboard tests: `test_dashboard_parity.py` 10, `test_dashboard_auth.py` 37, `test_dashboard_views.py` 32, `test_dashboard_controls.py` 25.
* Hosted: `test_live_supabase_stages.py` + `test_live_supabase_migrations.py` **7 passed** against the *replaced* `claim_opportunity_processing` (backward compatible); `test_live_dashboard_queue.py` **9 passed**; zero probe rows left behind.
* `pip-audit -r requirements.txt`: **No known vulnerabilities found** — required bumping `anyio` 4.14.1→4.14.2, `soupsieve` 2.8.4→2.9.0, `pydantic` 2.13.4→2.13.5 (+core), all of which had CVEs published after ADR 012's clean scan, plus pinning `starlette` 1.3.1 / `python-multipart` 0.0.31 for the new code.
* Live HTTP smoke against the real database: enqueue → `/job/<id>` 200 → duplicate enqueue lands on the existing job → cancel-before-start recorded with the requester's identity → bad CSRF 403 → all pages 200.
* Golden extraction/scoring baseline: unchanged.

**Not done, and why:** no Render deploy, no live Google login — no credentials in this environment (decision 2026-09-21: build + verify locally, hand over the runbook above).

---

## 7. Found while building — not fixed, needs a decision

**The Phase 3 market digest is silently degraded in production.** `database/market_store.py` selects `created_at` from `opportunities_cache`, a column that does not exist (the table has `discovered_at` and `processed_at`). Both the full and the fallback select fail, so the digest loads rows with **no dates at all** and every window reports "insufficient data for a trend (n=0 < 10)" — despite `discovered_at` being populated on 1210/1210 rows. This affects the weekly `cortech-market` email today, not only the dashboard. It is a one-line fix (`_CACHE_WITH_CREATED = "id, source_url, title, discovered_at"`, and drop `created_at` from `_CACHE_FULL`) but it changes what the weekly email says, so it was not made as part of a control-surface phase.
