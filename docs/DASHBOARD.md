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
4. **Second misconfiguration, same day:** the worker command was then deployed
   under `type: web`. A web service must bind `$PORT`; the worker never does,
   so Render port-scanned forever and no dashboard existed. `render.yaml` now
   declares the worker as `type: worker` (Background Worker — never
   port-scanned) and the web service as `type: web` with `healthCheckPath`,
   and `tests/test_render_yaml.py` fails CI if either drifts.
5. `render.yaml` opens with the hand-deploy rule; the correct commands are
   `uvicorn dashboard.app:app --host 0.0.0.0 --port $PORT` (web) and
   `python -m dashboard.worker` (worker). Never `python main.py`.

6. **Third failure, same day:** the web service was recreated by hand and its
   Start Command was typed into the form wrong. Rule from here on: **services
   are created only from `render.yaml` (New → Blueprint)**, never by hand. The
   file is the source of truth for both services; §3.3 is the procedure.
   `tests/test_render_yaml.py` (16 checks) fails CI if a command, service
   type, or env var name drifts from what the code actually reads.

7. **Fourth failure, 2026-09-22, from `render.yaml` itself:** the web
   `dockerCommand` was an inline `sh -c '…'` string. Render handed the whole
   line to the container as a single literal program name → exit 127
   `sh: 1: exec uvicorn dashboard.app:app … : not found` (pip had installed
   uvicorn fine). Nested quoting in a YAML string that passes through another
   shell layer is fragile, and this was the second whitespace/quoting deploy
   failure. Fix: the command lives in a committed script,
   [`dashboard/start.sh`](../dashboard/start.sh) (`chmod +x` in the
   Dockerfile), and `dockerCommand` is the bare path. Rule:
   **`dockerCommand` is plain words only — no `sh -c`, no quotes**;
   `tests/test_render_yaml.py` rejects any shell metacharacter in it.

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

### 3.2 Google Cloud — OAuth client (create before the Blueprint; register the URI after)

**Does a client already exist?** Unknown. On 2026-09-21 no Google OAuth client
existed in the build environment (ADR 013), the local `.env` has no
`GOOGLE_*` values, and nothing in the repo records one being created since.
Check console.cloud.google.com → *APIs & Services* → *Credentials* for a
**Web application** client named for this dashboard. If none, create one:

1. *OAuth consent screen*: **Internal** user type if the Google Workspace is
   `cortechconsultinggroup.com` (only workspace accounts can then reach the
   consent screen — a second wall in front of the app's own check). External
   works too; the app rejects off-domain emails regardless.
2. *Credentials* → *Create credentials* → *OAuth client ID* → **Web application**.
3. Authorised redirect URIs: for now add only
   `http://127.0.0.1:8000/auth/callback` (local login). **The production URI
   is added in §3.3 Step 5b**, after Render has assigned the real hostname —
   you cannot know it before the service exists (see below).
4. Copy the client ID and secret; they are entered in Render as
   `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in §3.3 Step 4.

The callback path is fixed by the code: `dashboard/settings.py::redirect_uri()`
returns `PUBLIC_BASE_URL + "/auth/callback"`, and `dashboard/app.py` serves
`GET /auth/callback`. Google requires an **exact** match — scheme, host,
case, path and trailing slash (`redirect_uri_mismatch` otherwise), and
`https` for anything other than localhost.

### 3.3 Render — Blueprint (the only supported way to create the services)

**Never create or edit a Render service by hand.** Three deploys failed on
2026-09-21 because a Start Command was typed into a form (§Incident). Both
services are declared in [`render.yaml`](../render.yaml) at the repo root;
that file is the source of truth. To change a service, edit the file, commit,
and sync the Blueprint.

Both commands were re-verified from the code on 2026-09-21 (§6):

| Service | `type` | `dockerCommand` | Proof it is the right process |
|---|---|---|---|
| `cortech-bd-dashboard` | `web` | `dashboard/start.sh` — runs `exec uvicorn dashboard.app:app --host 0.0.0.0 --port "${PORT:-10000}" --proxy-headers --forwarded-allow-ips="*"` | first log line `IDENTITY: cortech-bd-web`, then `Uvicorn running on http://0.0.0.0:<PORT>`; `/healthz` → 200 |
| `cortech-bd-worker` | `worker` | `python -m dashboard.worker` | first log lines `IDENTITY: cortech-bd-worker` then `dashboard worker up (token …); aggregate cap 100.00 USD / 24h`; **no port** |

The FastAPI instance is the module-level `app` in `dashboard/app.py`
(`app = FastAPI(...)`), hence `dashboard.app:app`. The worker entry point is
`dashboard/worker.py::main_loop`, run via `if __name__ == "__main__"`, hence
`python -m dashboard.worker`. The Docker runtime uses `dockerCommand`;
`startCommand` is for native runtimes and is ignored for Docker.

Three facts about Blueprints (render.com/docs/infrastructure-as-code, read
2026-09-21) shape the procedure below:

* The creation flow **matches existing resources by name**. If a match exists
  Render "appends a suffix to the name of each new resource to prevent
  collisions" — it does not adopt a hand-made service at creation.
* "Changes to a Blueprint never cause a resource to be deleted." Deleting is
  always a manual step in the dashboard.
* A service's `type` "can't be modified after creation." A hand-made Web
  Service can never become a Background Worker.

#### Procedure

**Step 1 — Delete the broken hand-made web service.** In the Render dashboard
find the Web Service whose latest deploy failed (wrong Start Command / "No
open ports detected"). Open its *Environment* tab and copy any secret values
you had pasted there (they are not recoverable after deletion). Then *Settings
→ Delete service*. **Do not touch the live worker `srv-daoh3h0473hc73aa77ag`.**

**Step 2 — Avoid the name collision with the live worker (rename only).**
The Blueprint declares `cortech-bd-worker`. If the live worker already has
that name, open `srv-daoh3h0473hc73aa77ag` → *Settings → Name* and rename it
to `cortech-bd-worker-manual`. A rename changes no command, env var or
deploy. If you prefer not to touch it at all, skip this step: Render will then
name the Blueprint's worker `cortech-bd-worker-<suffix>`, and that suffixed
service is the one the Blueprint manages from then on.

**Step 3 — Create the Blueprint.** Render dashboard → *New* → *Blueprint* →
connect the `cortech-bd-agent` repo, branch `main`. Render reads
`render.yaml` and shows the plan. Confirm it lists **exactly two** new
resources and nothing else:

| Name | Type | Runtime | Region | Plan |
|---|---|---|---|---|
| `cortech-bd-dashboard` | Web Service | Docker | Frankfurt | 0.5c-512mb |
| `cortech-bd-worker` | Background Worker | Docker | Frankfurt | 1c-2g |

If it shows a third resource, a Web Service named `…worker`, or a spec error,
stop and fix `render.yaml` first.

**Step 4 — Fill the `sync: false` secrets when prompted.** These are the
only values you type; everything else has a committed default.

| Service | Variable | Value |
|---|---|---|
| web | `SUPABASE_URL` | the project URL, `https://<ref>.supabase.co` (no `/rest/v1`) |
| web | `SUPABASE_SERVICE_KEY` | the **service_role** key from Supabase → Settings → API |
| web | `GOOGLE_CLIENT_ID` | OAuth client ID from §3.2 |
| web | `GOOGLE_CLIENT_SECRET` | OAuth client secret from §3.2 |
| web | `PUBLIC_BASE_URL` | **Placeholder for now:** `https://cortech-bd-dashboard.onrender.com` (no trailing slash). Render assigns the real hostname only when it creates the service, and it may append a random suffix (e.g. `cortech-bd-dashboard-x4k2.onrender.com`), so this value is corrected in **Step 5b**. Until then `/auth/login` returns 503 or Google returns `redirect_uri_mismatch`; `/healthz` is unaffected, so the deploy still goes Live |
| web | `AUTH_ALLOWED_EMAILS` | **leave empty** unless §3.5 applies |
| web | `DASHBOARD_SESSION_SECRET` | nothing — `generateValue: true`, Render creates it |
| worker | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | same two values as the web |
| worker | `ANTHROPIC_API_KEY` | from the live worker's *Environment* tab (same name) |
| worker | `OPENAI_API_KEY` | same source — required, embeddings |
| worker | `AIRTABLE_API_KEY`, `AIRTABLE_BASE_ID` | same source — optional, CRM write is fail-open |
| worker | `EMAIL_RECIPIENTS` | comma-separated reviewer addresses; empty disables the review email |
| worker | `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | fill if review email goes out via Gmail, else empty |
| worker | `RESEND_API_KEY`, `EMAIL_SENDER` | fill if via Resend, else empty |

No Google / OAuth / session value goes on the worker. No Anthropic, OpenAI,
Airtable or email value goes on the web.

**Step 5 — Deploy Blueprint and watch both logs.** Each service confirms it is
running the right process in its first seconds:

* **web** → `IDENTITY: cortech-bd-web — dashboard web service (FastAPI) …`,
  then `Uvicorn running on http://0.0.0.0:10000`, then the health check on
  `/healthz` passes and Render marks the service *Live*. Open the URL: you are
  redirected to `/auth/login`, which answers **503 "Google sign-in is not
  configured"** or Google shows `redirect_uri_mismatch` until Step 5b is
  done — expected at this point. Anything else in the first lines (a worker identity, a
  Rich progress banner, "No open ports detected") means the wrong command.
* **worker** → `IDENTITY: cortech-bd-worker — dashboard background worker …`,
  then `dashboard worker up (token srv-…); aggregate cap 100.00 USD / 24h`.
  It logs nothing more until a job is queued. There is no port and Render
  does not look for one.

**Step 5b — Set the real `PUBLIC_BASE_URL` and register the redirect URI.**
This ordering is forced: the hostname exists only after Step 5, and Google
must have the exact URI before the first login can succeed.

1. **Read the real URL.** Render dashboard → `cortech-bd-dashboard` → the URL
   under the service name (also visible as `RENDER_EXTERNAL_URL` in the
   service's *Environment* tab, and the logs print `Uvicorn running on
   http://0.0.0.0:10000`, not the public host). Render's docs say the
   `onrender.com` subdomain "incorporates" the service name; they do not
   promise it equals it, and new services commonly get a four-character
   suffix. **Copy it from the page; never type it from memory.** No custom
   domain is configured for this project.
2. **Google Cloud Console** → *APIs & Services* → *Credentials* → the client
   from §3.2 → *Authorised redirect URIs* → *Add URI* →
   `<real URL>/auth/callback` (e.g.
   `https://cortech-bd-dashboard-x4k2.onrender.com/auth/callback`). Exact
   match: `https`, no trailing slash, lowercase host. Save. The console's
   own note says settings may take from a few minutes to a few hours to
   take effect; if login still fails with `redirect_uri_mismatch` right
   after saving, wait before changing anything else.
3. **Render** → `cortech-bd-dashboard` → *Environment* → edit
   `PUBLIC_BASE_URL` to the real URL, **no trailing slash, no path** → choose
   **Save and deploy** (not *Save only*). An env var change does not reach a
   running Docker process; Render's *Save only* leaves the old value live
   "until its next deploy". *Save and deploy* reuses the existing image and
   restarts with the new value in about a minute; no rebuild is needed.
4. **Verify:** open `<real URL>/auth/login` → redirected to Google's account
   chooser (not a 503 "not configured" text, not `redirect_uri_mismatch`).
   Sign in with an `@cortechconsultinggroup.com` account → the Queue.

The URL is fixed at creation and does **not** change when the service is
renamed, so this is a one-time step. If the service is ever deleted and
recreated (Blueprint re-sync after a manual delete), the hostname may change
and Step 5b must be repeated.

**Step 6 — Cut the worker over.** Once the Blueprint worker shows its two
identity lines, compare it with the old one using the checklist below, then
*Suspend* `cortech-bd-worker-manual`. Leave it suspended for a day, then
delete it. Two workers on one queue is safe (`claim_dashboard_trigger` is an
atomic lease; a job runs once) but bills twice.

#### Does the Blueprint reproduce the live worker? (checklist)

This machine has no Render API key, so the live worker's configuration could
not be read programmatically. Compare
these fields in the dashboard, `srv-daoh3h0473hc73aa77ag` vs the Blueprint's
`cortech-bd-worker`. Every row must match:

| Field | Expected (from `render.yaml`) |
|---|---|
| Type | Background Worker |
| Runtime | Docker, Dockerfile path `./Dockerfile`, branch `main` |
| Docker Command | `python -m dashboard.worker` (an empty command is equivalent — it is the image CMD) |
| Region / Plan | Frankfurt / 1c-2g (legacy name `standard`). A different plan is a cost choice, not a correctness issue |
| Env: required | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` present with the same values |
| Env: caps | `MAX_RUN_COST_USD`, `DASHBOARD_AGGREGATE_CAP_USD`, `DASHBOARD_AGGREGATE_WINDOW_HOURS` — same numbers; the worker prints the aggregate cap in its second log line, so both logs must show `aggregate cap 100.00 USD / 24h` |
| Env: absent | no `GOOGLE_*`, `PUBLIC_BASE_URL`, `DASHBOARD_SESSION_SECRET`, `IMAP_*` |

Any variable the Blueprint sets that the old worker lacks carries the code's
own default (every `value:` in `render.yaml` equals the default in `config.py`
or `dashboard/settings.py`), so the new worker behaves identically.

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
| `cortech-bd-dashboard` | `0.5c-512mb` (legacy `starter`) = 0.5 CPU / 512 MB | **$7** |
| `cortech-bd-worker` | `1c-2g` (legacy `standard`) = 1 CPU / 2 GB | **$25** — Chromium + pdfplumber + the 512 MB V8 heap cap do not fit in 512 MB; workers have no free tier |
| **Total** | | **$32/mo** + LLM spend, which the two caps bound |

Cheaper option: web on `free` ($0) → **$25/mo**, but the web spins down after 15 idle minutes and the first request each time waits ~1 minute. For a tool used a few times a day that is a real annoyance; $7 is the recommendation. Prices are pro-rated per second; Render renamed plans in August 2026 (`starter`→`0.5c-512mb`, `standard`→`1c-2g`), old names still work.

---

## 4. Environment variables

Every name below was taken from a grep of `os.getenv` / `os.environ` in
`dashboard/`, `config.py` and `reporting/email_report.py` on 2026-09-21 and is
declared in `render.yaml`; `tests/test_render_yaml.py` fails if a declared
key is not read anywhere in the code.

**Web — required:** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (the web reads and
writes Supabase only), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`DASHBOARD_SESSION_SECRET` (generated by Render), `PUBLIC_BASE_URL`. The web
does **not** need `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or Airtable:
`config.require_env()` is called by the worker and `main.py`, not by
`dashboard.app`, and the web boots and serves `/healthz` without them (§6).
`PORT` is injected by Render (default 10000) and read by the command; do not
declare it.

**Web — optional (defaults in `dashboard/settings.py`):** `AUTH_ALLOWED_DOMAIN`
(`cortechconsultinggroup.com`), `AUTH_ALLOWED_EMAILS` (empty),
`DASHBOARD_SESSION_MAX_AGE` (43200), `DASHBOARD_COOKIE_SECURE` (true — false
only for local http), `DASHBOARD_POLL_MS` (15000), `DASHBOARD_TRIGGERS_ENABLED`
(true), `DASHBOARD_BULK_MAX` (40), `DASHBOARD_AGGREGATE_CAP_USD` (100),
`DASHBOARD_AGGREGATE_WINDOW_HOURS` (24), `DASHBOARD_HEARTBEAT_SECONDS` (10) —
the last three mirror the worker's values for display and must stay identical.

**Worker — required (`config.require_env`, exit 1 if missing):** `SUPABASE_URL`,
`SUPABASE_SERVICE_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.

**Worker — optional credentials:** `AIRTABLE_API_KEY`, `AIRTABLE_BASE_ID`
(CRM write-through, fail-open); `EMAIL_RECIPIENTS`, `GMAIL_ADDRESS`,
`GMAIL_APP_PASSWORD`, `RESEND_API_KEY`, `EMAIL_SENDER` (review email; empty
`EMAIL_RECIPIENTS` disables it).

**Worker — numeric settings, all with committed defaults:** `MAX_RUN_COST_USD`
(25), `DASHBOARD_AGGREGATE_CAP_USD` (100), `DASHBOARD_AGGREGATE_WINDOW_HOURS`
(24), `ANTHROPIC_TIMEOUT_SECONDS` (180), `ANTHROPIC_MAX_RETRIES` (2),
`DOCUMENT_DOWNLOAD_TIMEOUT_SECONDS` (60), `DOCUMENT_DOWNLOAD_MAX_RETRIES` (2),
`MAX_DOWNLOAD_REDIRECTS` (5), `MAX_DOCUMENT_BYTES` (26214400),
`MAX_DOCUMENT_UNCOMPRESSED_BYTES` (104857600), `MAX_EXTRACTED_TEXT_CHARS`
(400000), `MAX_GDRIVE_FILES` (25), `PLAYWRIGHT_TIMEOUT_MS` (45000),
`PLAYWRIGHT_JS_HEAP_MB` (512), `FULL_DRAFT_FOR_WATCH` (true),
`DASHBOARD_HEARTBEAT_SECONDS` (10), `DASHBOARD_WORKER_POLL_SECONDS` (5),
`DASHBOARD_WORKER_LEASE_SECONDS` (3600).

**On neither service:** `IMAP_*`, `CHECK_INTERVAL_HOURS`,
`MAX_OPPORTUNITIES_PER_RUN`, `HEALTHCHECK_URL` — discovery-scheduler settings;
neither Render service runs discovery.

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

* **Render commands re-verified from code, 2026-09-21 (Blueprint rewrite).**
  `dashboard/app.py:53` defines `app = FastAPI(...)` → `dashboard.app:app`;
  `dashboard/worker.py:305` `main_loop()` behind `__main__` → `python -m
  dashboard.worker`. Local runs with the exact `render.yaml` commands:
  * web, `PORT=8766 sh -c 'exec uvicorn dashboard.app:app --host 0.0.0.0 --port "${PORT:-10000}" …'`
    → `IDENTITY: cortech-bd-web`, `Uvicorn running on http://0.0.0.0:8766`,
    socket `LISTEN 0.0.0.0:8766`, `GET /healthz` 200 `ok`, `GET /` 302 →
    `/auth/login`. **Superseded 2026-09-22** (Incident item 7): the same
    command now runs from `dashboard/start.sh`. Re-verified in the built
    image: `docker run -e PORT=8766 -p 8766:8766 <image> dashboard/start.sh`
    → `Uvicorn running on http://0.0.0.0:8766`, `GET /healthz` 200.
  * web with **only** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` and the four auth
    vars set (no `.env`, no Anthropic/OpenAI/Airtable) → same result. The web
    does not need pipeline credentials; they were removed from its Blueprint
    block.
  * worker, `python -m dashboard.worker`, real env, queue empty →
    `IDENTITY: cortech-bd-worker`, `dashboard worker up (token local-…);
    aggregate cap 100.00 USD / 24h`, no listening socket, idles, SIGTERM →
    `dashboard worker stopped`.
  * worker with only Supabase set → `Missing required environment variables:
    ANTHROPIC_API_KEY, OPENAI_API_KEY`, exit 1.
  * `tests/test_render_yaml.py`: 16 passed.
* **Not verified:** the live worker `srv-daoh3h0473hc73aa77ag` could not be
  read (no Render API key on this machine); §3.3 gives the manual checklist.

**Not done, and why:** no Render deploy, no live Google login — no credentials in this environment (decision 2026-09-21: build + verify locally, hand over the runbook above).

---

## 7. Found while building — not fixed, needs a decision

**The Phase 3 market digest is silently degraded in production.** `database/market_store.py` selects `created_at` from `opportunities_cache`, a column that does not exist (the table has `discovered_at` and `processed_at`). Both the full and the fallback select fail, so the digest loads rows with **no dates at all** and every window reports "insufficient data for a trend (n=0 < 10)" — despite `discovered_at` being populated on 1210/1210 rows. This affects the weekly `cortech-market` email today, not only the dashboard. It is a one-line fix (`_CACHE_WITH_CREATED = "id, source_url, title, discovered_at"`, and drop `created_at` from `_CACHE_FULL`) but it changes what the weekly email says, so it was not made as part of a control-surface phase.
