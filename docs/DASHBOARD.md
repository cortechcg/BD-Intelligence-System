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
* **Scheduler — verified state on 2026-09-21, and a decision for you.**
  The README says discovery runs on this machine via `systemd --user` timers.
  **It does not.** `systemctl --user list-timers --all` shows no `cortech-*`
  unit; none exist system-wide; there is no crontab entry; `cron.log` was last
  written **2026-08-27**. The unit files lived outside the repo and are gone.
  Since late August the pipeline has run only when someone typed
  `main.py --once` — or when a misconfigured deploy booted bare `main.py`
  (the 2026-09-21 incident, §"Incident" below). The dashboard **does not**
  replace scheduled discovery; its worker consumes `dashboard_triggers` only
  and has no schedule. What to do about scheduled discovery is therefore an
  open decision with three honest options: (i) reinstall the systemd timers
  on this machine (`deploy/systemd/` has the market-digest templates; the
  other four need recreating); (ii) a Render Cron Job running
  `python main.py --once` (commented block at the end of `render.yaml`; that
  makes the Render bill and Anthropic spend grow with it, which is the
  Railway history); (iii) leave discovery manual. **Never** run bare
  `python main.py` anywhere for this — it now refuses to start outside an
  interactive terminal (§"Incident").

### Spend caps — two of them

1. **Per run** — `MAX_RUN_COST_USD` (ADR 012), enforced inside `complete()` before every provider call. A dashboard run is an ordinary run; it gets no exemption. A cap halt leaves the ledger `failed` at the last stage, retryable, not counted as a draft failure.
2. **Aggregate** — `DASHBOARD_AGGREGATE_CAP_USD` over `DASHBOARD_AGGREGATE_WINDOW_HOURS` (default 100 USD / 24 h). **This is a control the pipeline did not have:** cron's `$25` covers one whole discovery run, but each `--submit-url` — hence each dashboard job — is its own run with a fresh `$25`. Forty clicks on *Draft selected* would otherwise be forty caps. The gate is enforced by the **worker** at claim time against spend actually recorded on trigger rows (finished, failed *and* cancelled); the UI only mirrors it. Unknown-cost runs count as over, the same rule the per-run cap applies. `tests/test_dashboard_controls.py::test_bulk_trigger_of_forty_jobs_is_bounded_by_the_aggregate_cap` drives forty queued jobs through the worker and asserts the cut-off.

### Cancel — what it really does

Rendered verbatim in the UI (`dashboard/app.py::CANCEL_COPY`) and asserted against the code by `test_cancel_copy_matches_the_mechanism`:

> Cancel is cooperative, not instant. A job that has not started is withdrawn immediately and the worker never sees it. A job that is running is flagged; the worker's next heartbeat (every 10s) asks the pipeline to halt before its next model call. The model call already in flight finishes — up to ANTHROPIC_TIMEOUT_SECONDS — and its cost is incurred and recorded. The opportunity keeps its last saved stage and checkpoint and can be resumed. Cancelled spend still counts toward the aggregate cap.

Mechanism: `RunHaltRequested` subclasses `SpendCapError`, so the pipeline's one existing cooperative halt point and one existing handler (ADR 012 §4) do the work. No second interruption mechanism was added.

---

## Incident 2026-09-21 — a hand-made Render Web Service ran the pipeline

A Render Web Service was created for this repo **by hand** (not via the
Blueprint), runtime Docker, with no Start Command. It therefore ran the
Dockerfile's default `CMD ["python", "main.py"]`. Bare `main.py` is the
always-on scheduler: it ran a full discovery pass on boot — 15 opportunities
claimed, **1 drafted** (WHH feasibility study, BID 83/76, Airtable
`recqhH8Sv3tvm16UN`, status *Reviewing*), review emails sent to
`EMAIL_RECIPIENTS` — and would have repeated every 6 hours. This is the same
failure that got the project pulled off Railway (README). The service was
suspended.

What changed so it cannot recur:

1. **Dockerfile default is now the worker** (`python -m dashboard.worker`),
   which consumes the queue and idles. A hand-made Docker service with no
   Start Command can no longer run discovery.
2. **Bare `main.py` refuses to start its scheduler** unless it is in an
   interactive terminal with none of `PORT`, `RENDER`, `RENDER_SERVICE_ID`,
   `RAILWAY_ENVIRONMENT`, `DYNO` set (`main.refuse_headless_scheduler`, exit
   2, clear message). `--once`, `--submit-url` and `--run-*` are untouched.
   Override for a deliberate local loop only: `CORTECH_ALLOW_SCHEDULER=1`.
3. **Every long-lived process logs an `IDENTITY:` line** in its first
   seconds (`cortech-bd-web` / `cortech-bd-worker`) stating what it is and
   that it never runs discovery. If a Render log's first lines do not show
   one, the wrong command is running.
4. `render.yaml` opens with the hand-deploy rule; the correct commands are
   `uvicorn dashboard.app:app --host 0.0.0.0 --port $PORT` (web) and
   `python -m dashboard.worker` (worker). Never `python main.py`.

Emails from that run went only to `EMAIL_RECIPIENTS` (both `send_report` and
`send_proposal_email` resolve recipients solely from that env var; no code
path reads an address from a tender). The drafted opportunity sits in the
normal human-review state and needs a human decision like any other draft.

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

**Use the Blueprint. Do not create services by hand** (see §Incident). If you must, the web Start Command is `uvicorn dashboard.app:app --host 0.0.0.0 --port $PORT` and the worker Docker Command is `python -m dashboard.worker`; check the first log lines for `IDENTITY:`.

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

> **Approver, read this line first.** A Render Background Worker does not
> scale to zero. On the `standard` plan the worker bills **~$25 every month
> whether the team triggers 1 draft or 50** — you pay for the instance being
> up, not per draft. The web service adds $7. LLM spend is on top, bounded by
> the two caps. **The worker tier is not yet decided** (options A/B/C below);
> `render.yaml` currently says `standard` as a placeholder, not a decision.

**Is a smaller worker viable?** The only smaller paid worker plan is
`0.5c-512mb` ($7); workers have no free tier and nothing sits between 512 MB
and 2 GB. The repo's own defaults set Chromium's V8 heap cap to
`PLAYWRIGHT_JS_HEAP_MB=512` — the renderer alone may fill a 512 MB plan
before the browser process, Python, the SDKs and pdfplumber (documents up to
25 MB) are counted. No peak-RSS measurement exists for this pipeline. Render's
own guidance is to choose memory that "covers your peaks with room to spare"
or expect out-of-memory restarts. An OOM mid-draft is recoverable (lease
expires, Phase 7 checkpoint resumes) but re-pays for unpersisted work.
`standard` is therefore the minimum tier defensible today; `starter` would
require lowering the heap cap to ~128 MB *and* a measured run on real PDFs
first.

**Pause/resume is possible.** Render can suspend and resume a worker from the
dashboard or API, and compute is "billed only when actively running your
workload", prorated per second.

| Option | Monthly | Cost in practice |
|---|---|---|
| **A. Always on, `standard`** | ~$32 | Nothing to remember; a trigger runs within seconds |
| **B. `standard`, suspended between bursts** | $7 + ~$0.035/h of worker uptime (≈$0.83/day when on) | Someone must resume it before use; queued triggers wait as `queued` until then; no auto-resume without scripting |
| **C. `starter` worker** | ~$14 | Not recommended as configured — unmeasured OOM risk (above) |


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
