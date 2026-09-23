# Cortech BD Intelligence Agent

**An autonomous pipeline that discovers, filters, analyzes, and drafts consultancy proposals for Cortech Consulting Group — running locally, on demand and on a schedule, without needing a person to find opportunities by hand.**

---

## Table of Contents

- [What This Is](#what-this-is)
- [How It Works](#how-it-works)
- [Repository Structure](#repository-structure)
- [Prerequisites](#prerequisites)
- [Setup — Fresh Machine, Repo Already Cloned](#setup--fresh-machine-repo-already-cloned)
- [Environment Variables Reference](#environment-variables-reference)
- [Running the Agent](#running-the-agent)
- [Automatic Scheduling (systemd)](#automatic-scheduling-systemd)
- [Data Stores at a Glance](#data-stores-at-a-glance)
- [Known Issues & Hard-Won Lessons](#known-issues--hard-won-lessons)
- [Disaster Recovery — Rebuilding From Zero](#disaster-recovery--rebuilding-from-zero)
- [Working With This Repo (Cursor Workflow)](#working-with-this-repo-cursor-workflow)

---

## What This Is

Cortech competes for consulting work advertised across dozens of tender portals, procurement sites, and daily email newsletters — new postings appear constantly, mixed in with staff vacancies and work outside Cortech's East Africa / Horn of Africa focus. This agent watches those sources continuously, filters out what doesn't fit, and for genuine opportunities, produces a real first draft — either a full technical proposal or a short Expression of Interest, matched to what's actually being requested — grounded in Cortech's own real past submissions rather than a generic template.

**This system runs locally, not on a cloud host.** It was previously deployed on Railway; that deployment was intentionally removed after an incident where it ran unattended for two days and drained both the Railway account balance and Anthropic API credits without anyone noticing. It now runs only on this machine, only while the machine is on — see [Automatic Scheduling](#automatic-scheduling-systemd) for how it still runs without needing to be triggered by hand every time.

---

## How It Works

1. **Discover** — RSS feeds, Playwright-driven scrapers for JavaScript-heavy portals, and an IMAP-based check of the Assortis/ICA World daily newsletter.
2. **Filter, for free** — before any paid API call, every posting passes a three-gate keyword check (staff-vacancy language, geography, thematic relevance) and a semantic near-duplicate check against everything already seen, catching the same tender posted on multiple portals under different URLs.
3. **Analyze** — the LLM reads the full document (as untrusted data) and extracts structured fields. A **deterministic scorer** (`intelligence/bid_scorer.py`, weights in `intelligence/scoring_model.json`) calculates FIT / WIN / STRATEGIC / RISK and BID / WATCH / NO-BID. The model's own numeric score is stored only as an audit field.
4. **Match & cost safely** — team CVs are matched semantically against requirements, then checked for explicit geography, sector, language, years, skills, and live availability (missing education or availability is UNKNOWN, never inferred as a credential or as 100% free). Financial preparation uses only ToR-stated effort days and an exact maintained rate-card row. It returns `PARTIAL` / `INSUFFICIENT DATA` rather than inventing travel, workshop, overhead, contingency, or tax figures.
5. **Draft** — a strategy is decided once, then every section is drafted against it and against real past-proposal structure (extracted from 60+ real submissions, not an assumed template). Named “Cortech has done X before” claims are checked against retrieved proposal chunks; unsupported names are tagged `[NOT VERIFIED]` in the draft (`intelligence/grounding.py`, ADR 003 / ADR 008).
6. **Review itself** — a self-assessment pass scores the draft against the ToR's actual stated evaluation criteria before anyone sees it.
7. **Deliver** — a formatted `.docx` and an email land with the team, flagged by urgency and by anything the review pass caught.
8. **Learn** — when a bid is later marked Won or Lost, the system extracts concrete lessons and feeds them into future proposals for similar clients and donors.

---

## Repository Structure

```
cortech-bd-agent/
├── main.py                      # Orchestrator — all CLI entry points live here
├── config.py                    # All environment variables and constants, read once
├── database/
│   ├── airtable_client.py       # CRM reads and writes against Supabase (historical module name)
│   ├── supabase_client.py       # pgvector storage, semantic search, dedup, embeddings
│   ├── organizations.py         # Fail-open org index (Phase 2)
│   ├── market_store.py          # Fail-open observed-opportunity loaders (Phase 3)
│   └── intelligence_facts.py    # Fail-open award/relationship citations (Phase 5)
├── intelligence/
│   ├── analyzer.py               # LLM extraction (advisory scores only)
│   ├── bid_scorer.py             # Deterministic FIT/WIN/RISK + versioned weights
│   ├── pipeline_stages.py        # Phase 7 extract/score/draft + resume
│   ├── win_calibration.py        # Phase 6 P(win) harness (null unless labeled n meets the bar)
│   ├── win_calibration_artifact.json  # v0.1.0 census; fitted: false — does not rewrite history
│   ├── market_trends.py          # Observed-data 30/90-day digest (no LLM)
│   ├── organizations.py          # Canonical client/donor matching + roll-up
│   ├── competitors.py            # Assortis Awarded Firm(s) citations (Phase 5)
│   ├── relationships.py          # Cited JV/consortium edges from data/proposals/
│   ├── scoring_model.json        # score_version 1.0.0 — changing this does not rewrite old records
│   ├── cv_matcher.py             # Semantic CV matching + explicit capability overlay
│   ├── compliance.py             # SATISFIED/PARTIAL/MISSING/UNKNOWN matrix
│   ├── budget_calculator.py     # Evidence-bound personnel costing; never LLM-estimated
│   ├── proposal_writer.py       # Section generation, strategy, style-guide grounding, quality self-score
│   ├── grounding.py             # Named past-work claim → retrieved chunk; else [NOT VERIFIED]
│   └── learning.py               # Win/loss lesson extraction and retrieval
├── monitors/
│   ├── rss_monitor.py            # RSS feeds + the 3-gate free filter + semantic dedup
│   ├── scraper.py                # Playwright scrapers for JS-rendered portals
│   └── assortis_email.py        # IMAP parsing of the ICA Daily Newsletter's Assortis section
├── processors/downloader.py     # Document fetch + text extraction (PDF/DOCX/HTML)
├── processors/document_quality.py
├── tests/                       # Unit tests — URL/dedup, dates, scoring, injection wrap
├── docs/                        # CURRENT_STATE, ARCHITECTURE, SCORING_MODEL, SECURITY, audit
├── reporting/
│   ├── email_report.py           # All outgoing email — Gmail SMTP first, Resend HTTP fallback
│   └── docx_builder.py           # Renders finished sections into a formatted Word document
├── utils/llm.py                 # OpenAI client, complete(), get_text() — use everywhere an LLM call is made
├── extract_style_guide.py       # One-time script: derives real structure/style guides from data/proposals/
├── populate_airtable.py         # Loads CVs and past proposals into Supabase
├── check_schema.py              # Confirms the Supabase CRM tables exist
├── data/
│   ├── cvs/                      # Real team CVs (source for embed_cvs.py)
│   └── proposals/                # Real past submissions (source for style-guide extraction + past-work context)
├── intelligence/style_guides/   # Generated by extract_style_guide.py — human-readable, review before trusting
├── supabase_migration_*.sql     # Schema migrations — run once each in the Supabase SQL editor
├── deploy/systemd/              # Templates for cortech-market.service/.timer (copy to ~/.config/systemd/user/)
├── .env.example                  # Every variable that needs a real value in .env — .env itself is gitignored
└── CURSOR_TASK_*.md              # Historical implementation prompts — a record of design decisions and why
```

---

## Prerequisites

Accounts needed, all free-tier-capable except where noted:

| Service | Used for |
|---|---|
| [Anthropic](https://console.anthropic.com) | Chat: `claude-haiku-4-5` (analysis) + `claude-sonnet-5` (proposals) |
| [OpenAI Platform](https://platform.openai.com) | Embeddings only (`text-embedding-3-small`) |
| [Supabase](https://supabase.com) | CRM, pgvector storage, semantic search |
| Gmail account | Primary outgoing email (app password, not your real password) |
| [Resend](https://resend.com) | Fallback outgoing email — required if deploying anywhere that blocks SMTP |
| IMAP-accessible mailbox | Whatever inbox receives the ICA Daily Newsletter |

Python 3.12+, and `git`.

---

## Setup — Fresh Machine, Repo Already Cloned

```bash
cd cortech-bd-agent
python3 -m venv cortech
source cortech/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` with real values — see [Environment Variables Reference](#environment-variables-reference) below for where each one comes from. Then:

Before the first production run, open the Supabase SQL Editor and apply
`supabase_migration_opportunity_state.sql`. It creates the ownership-bound
lease ledger used for retryable opportunity processing; without it the agent
fails open (never drops a tender), but cannot provide cross-process deduplication.
Apply `supabase_migration_opportunity_stages.sql` once after that so
`pipeline_stage` + checkpoint resume work; until then the agent logs CRITICAL
that resume is unavailable and still attempts the opportunity from discovered
(it will not drop a BID/WATCH item). Apply `supabase_migration_content_hash.sql` once so `opportunities_cache.content_hash`
is a unique document-body identity; until then the client fail-opens and omits the column.
Apply `supabase_migration_organizations.sql` once so client/donor matching can persist
canonical orgs; until then the matcher fail-opens (empty index, review email still
renders N=0 / UNKNOWN). Apply `supabase_migration_opportunity_facts.sql` once so
`opportunities_cache` can store thematic/location/donor/`discovered_at` for the
observed-data market digest; until then the digest fail-opens to cache timestamps
or an honest insufficient digest. Apply
`supabase_migration_leave_airtable.sql` once so consultants, rate cards, agent
logs, and opportunity status live in Supabase. Apply
`supabase_migration_award_relationships.sql` once (after the organizations
migration) so Assortis award-firm citations and Cortech-submission relationship
edges can persist; until then those stores fail-open. Do not invent
organization, competitor, or partner fields on a second CRM.

```bash
python main.py --once
```

Watch the output. A clean run should show discovery, filtering, and (if anything qualifies) analysis and drafting, ending in a "Pipeline Complete" summary panel. If it errors immediately, it's almost always a missing or malformed `.env` value — `require_env()` fails loudly if required keys are missing, which is deliberate.

---

## Environment Variables Reference

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys — used for analysis and proposal drafting |
| `OPENAI_API_KEY` | platform.openai.com → API Keys — used for embeddings only |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Supabase project → Settings → API. **Use the service role key**, not the anon key — this is server-side code, not a browser client. Consultants, rate cards, logs, and opportunity status live here. |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` | Google Account → Security → 2-Step Verification → App Passwords. **Not your real Gmail password** — this won't work with one. |
| `RESEND_API_KEY` / `EMAIL_SENDER` | resend.com → API Keys, and a verified sending domain/address. Listed in `.env.example`. |
| `IMAP_HOST` / `IMAP_PORT` / `IMAP_USERNAME` / `IMAP_PASSWORD` | Whatever mail provider hosts the inbox receiving the ICA newsletter — likely different credentials than `GMAIL_ADDRESS` above, do not assume they're interchangeable |
| `CHECK_INTERVAL_HOURS` | How often the main discovery pipeline runs, in hours. Defaults to `6`. |
| `HEALTHCHECK_URL` | Optional. A public Healthchecks.io-style ping URL for the **full discovery pipeline**. It receives one bounded HTTPS ping only after `--once` / scheduled discovery completes; an unhandled discovery failure sends its `/fail` endpoint. It is not pulsed by deadline alerts or manual submits, so those cannot mask a missed discovery run. Leave blank to disable. |
| `FULL_DRAFT_FOR_WATCH` | `true` (current default) generates the full proposal even for WATCH-tier opportunities. Set to `false` for the lightweight cover-letter-only path. |
| `ANTHROPIC_TIMEOUT_SECONDS` / `ANTHROPIC_MAX_RETRIES` | Claude call timeout (default 180s) and SDK retries (default 2). |
| `DOCUMENT_DOWNLOAD_TIMEOUT_SECONDS` / `MAX_DOCUMENT_BYTES` / `MAX_DOWNLOAD_REDIRECTS` / `DOCUMENT_DOWNLOAD_MAX_RETRIES` / `MAX_GDRIVE_FILES` / `MAX_EXTRACTED_TEXT_CHARS` / `MAX_DOCUMENT_UNCOMPRESSED_BYTES` | Limits for untrusted document downloads and Drive annex packs. Defaults: 60 seconds, 25 MiB, 5 redirects, 2 transient retries, 25 Drive files, 120,000 extracted characters, and 100 MiB expanded DOCX content. |
| `MAX_OPPORTUNITIES_PER_RUN` | Cap on drafts per discovery run. Default `15`. |

`python main.py` fails at startup if `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SUPABASE_URL`, or `SUPABASE_SERVICE_KEY` are missing.

**`.env.example` is placeholders only.** If an older copy ever contained real keys, rotate them.

**None of these are recoverable from git.** `.env` is deliberately excluded from version control — see [Disaster Recovery](#disaster-recovery--rebuilding-from-zero).

---

## Running the Agent

```bash
python main.py --once                    # Run the full discovery pipeline once
python main.py                            # Old always-on scheduler loop — superseded; REFUSES to start unless in an interactive terminal with no $PORT/$RENDER (see docs/DASHBOARD.md §Incident)
python main.py --submit-url "<url>"       # Manually process one specific tender URL immediately
python main.py --submit -url "<url>"      # Same (accepted alias of --submit-url)
python main.py --run-assortis             # Manually trigger just the newsletter check
python main.py --run-deadline-check       # Manually trigger just the deadline-escalation check
python main.py --run-winloss              # Manually trigger just the win/loss lesson extraction
python main.py --run-market-digest        # Observed-data market digest (trailing 30/90 days)
python main.py --extract-relationships    # Cited JV/consortium edges from data/proposals/
python -m pytest tests/ -q                # Unit tests (no live APIs)
```

`--submit-url` bypasses the free discovery filters (a human explicitly chose this URL) but still respects exact-URL dedup — resubmitting the same URL warns rather than silently reprocessing, and proceeds anyway since it was a deliberate choice.

---

## Automatic Scheduling (systemd)

> **Status check 2026-09-21:** no `cortech-*` timers are installed on this machine (`systemctl --user list-timers --all`), and `cron.log` was last written 2026-08-27. The description below is how it is *meant* to run; it is not currently running. See `docs/DASHBOARD.md` §Scheduler for the open decision.

Since this runs locally rather than on an always-on host, scheduling uses `systemd --user` timers rather than the old in-process scheduler loop. The key property: **`Persistent=true`** means a missed run (because the machine was off or asleep) fires automatically the next time the machine is up, rather than silently never happening.

Four timers plus a fifth for the observed-data market digest, each invoking one of the CLI flags above:

| Timer | Fires | Runs |
|---|---|---|
| `cortech-discovery.timer` | Every `CHECK_INTERVAL_HOURS` | `main.py --once` |
| `cortech-assortis.timer` | Daily, ~11:45 | `main.py --run-assortis` |
| `cortech-deadline.timer` | Daily, ~08:00 | `main.py --run-deadline-check` |
| `cortech-winloss.timer` | Daily, ~08:15 | `main.py --run-winloss` |
| `cortech-market.timer` | Weekly, Monday ~08:30 | `main.py --run-market-digest` |

Unit files live in `~/.config/systemd/user/` — **outside this repository**, so they are not recoverable from git and need to be recreated separately if lost (see Disaster Recovery). Templates for the market digest units are also kept in-repo at `deploy/systemd/cortech-market.service` and `deploy/systemd/cortech-market.timer`. Check status any time with:

```bash
systemctl --user list-timers --all
```

Playwright: the host must have Google Chrome at `/usr/bin/google-chrome` (or `google-chrome-stable`). **Do not set `PLAYWRIGHT_BROWSERS_PATH` to an empty cache** in the unit `Environment=` — Cursor sandboxes do that and bundled Chromium then fails. `launch_chromium` unsets a broken path and falls back to system Chrome so systemd and Cursor both work.

The pipeline does not call Airtable. A failed Supabase log insert is ignored so it cannot stop a run.

---

## Data Stores at a Glance

**Supabase holds the CRM and the vectors.** A failed log insert does not stop a run. A missing rate-card row leaves that budget line incomplete.

| Store | Holds | Recoverable if lost? |
|---|---|---|
| Supabase (`opportunities_cache`, `opportunity_processing`, `cv_embeddings`, `proposal_embeddings`, `consultants`, `rate_cards`, `agent_logs`, `win_loss_memory`) | Vectors, dedup, stages, consultants, rates, status, logs | Survives independently in the cloud; only credentials need re-adding to `.env` |
| `data/cvs/`, `data/proposals/` (Supabase Storage bucket `Cortech-documents`, **not git**) | The real source documents everything else is built from | Survive in the cloud with the rest of the Supabase project; `python sync_source_documents.py pull` restores them into a fresh clone (§Source documents) |
| `intelligence/style_guides/*.md` (in this repo) | Extracted structure/style guides | Regenerable by re-running `extract_style_guide.py` against `data/proposals/`, if that folder survives |

### Source documents

`data/proposals/` (past technical and financial proposals, named for their
clients) and `data/cvs/` (named consultants' CVs) are **not tracked in git**.
Decision 2026-09-22: a repository history that carries client-identifying
financial documents and personal CVs into every clone ever made — including
clones by people since removed — was not an acceptable exposure, and the 466 MB
they added to every clone was the smaller of the two problems. The git history
was rewritten the same day to remove them (`git filter-repo`; anyone with an
older clone must re-clone).

They live in the private Supabase Storage bucket **`Cortech-documents`** in the
same project as everything else, under `proposals/` and `cvs/`, and are moved
with one script that uses the credentials already in `.env`:

```bash
python sync_source_documents.py status   # compare data/ with the bucket
python sync_source_documents.py pull     # fresh clone: bucket → data/
python sync_source_documents.py push     # after adding a document: data/ → bucket
```

Run `pull` before `extract_style_guide.py`, `extract_voice_exemplars.py`,
`embed_cvs.py` or `main.py --extract-relationships`. The pipeline itself
degrades gracefully without the folder (`proposal_writer` logs that it is
proceeding without the real proposals), and neither Render service ships the
documents in its image (`.dockerignore`). Only the two `PUT_*_HERE.txt`
placeholders remain in git so the folders exist on checkout.

Agent logs are rows in Supabase `agent_logs`. A failed insert is ignored.

---

## Known Issues & Hard-Won Lessons

These have each caused real, confirmed production failures. Documented here specifically so they don't get silently reintroduced by a future Cursor session working from a stale file:

- **Always use `get_text(response)` from `utils/llm.py`**, never `choices[0].message.content` scattered across call sites. All chat goes through `complete()`.
- **Never write `dict.get(key, default)[some_slice]`.** `.get()` only substitutes the default when the key is *missing* — if the key exists but its value is `None`, `.get()` returns `None`, and slicing it crashes with `'NoneType' object is not subscriptable`. Use `(dict.get(key) or default)[slice]` instead. This has also regressed once.
- **Railway SMTP is blocked at the platform level** (irrelevant now that this runs locally, but relevant again if ever redeployed to a similar host) — `email_report.py` tries Gmail SMTP first, falls back to Resend's HTTP API. Both paths need real, working credentials for delivery to succeed; a failure in one silently masks whether the other is even configured.
- **`.env` must never be committed.** It was tracked in git history for a period early in this project before being corrected — if that history was ever shared or the repo was ever public, treat every credential used at that time as compromised and rotate it, regardless of whether this has already been done.
- **NO-BID and EOI-stage opportunities intentionally skip CV matching and budget calculation** — this is a deliberate cost optimization, not a bug. A NO-BID is stored as a recommendation on a `New` record for human confirmation; it is not an automated final business decision. The label comes from the deterministic scorer, not Claude's integer.
- **The LLM `cortech_fit_score` is advisory.** Official FIT/WIN/recommendation are calculated in `intelligence/bid_scorer.py`. Do not "fix" a score by prompting the model to return a different number. `calibrated_win_probability` is an audit field that is currently INSUFFICIENT DATA — it is not the official WIN score.
- **`.env.example` must never contain live keys.**

---

## Disaster Recovery — Rebuilding From Zero

**Read this before you need it.** This assumes the local folder — this entire directory — has been lost (stolen laptop, disk failure, accidental deletion). It does **not** assume Supabase or the GitHub repo have also been lost. A section for the more extreme "everything is gone" case is at the end.

### Step 1 — Re-clone the code

```bash
git clone <your-github-repo-url>
cd cortech-bd-agent
```

If this succeeds, all Python code, the extracted style guides, and every `CURSOR_TASK_*.md` design record are back. **`.env` will not be — that's expected, it's gitignored on purpose.** `data/cvs/` and `data/proposals/` will not be either: they are not in git (§Source documents). Once `.env` has the Supabase credentials, `python sync_source_documents.py pull` restores them.

### Step 2 — Rebuild the Python environment

```bash
python3 -m venv cortech
source cortech/bin/activate
pip install -r requirements.txt
```

### Step 3 — Recreate `.env`

This is the step that actually matters. If you kept a secure backup of your real `.env` values (a password manager, an encrypted note — **never** a second git repo), restore it now and skip to Step 4.

If you did **not** back it up, every credential needs to be regenerated or re-fetched from its source, one at a time, using the [Environment Variables Reference](#environment-variables-reference) table above:

1. `ANTHROPIC_API_KEY` — generate a fresh key at console.anthropic.com. The old one, if it still exists, should be revoked regardless, since you don't know for certain it wasn't exposed.
2. `OPENAI_API_KEY` — generate a fresh key at platform.openai.com. Needed for CV embeddings only.
3. `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` — **your Supabase project and its data are almost certainly still alive.** Log into supabase.com, find the existing project (don't create a new one), and pull the URL and service role key from Settings → API. Creating a *new* project here would mean starting with an empty database, losing every embedding, consultant, rate, and cached opportunity.
4. `GMAIL_APP_PASSWORD` — app passwords aren't recoverable, only regeneratable. Google Account → Security → App Passwords → create a new one. The Gmail address itself is unaffected.
5. `RESEND_API_KEY` — resend.com → API Keys → create a new one if needed.
6. `IMAP_PASSWORD` (and host/username if those changed) — whoever manages that mailbox.

### Step 4 — Verify you're connected to the *existing* cloud data, not empty new instances

Before running anything that writes data, confirm the Supabase credentials point at your real, existing project:

```bash
python check_schema.py
```

This checks that `consultants`, `rate_cards`, `agent_logs`, and `opportunity_processing` are present. A missing table means the project is wrong or `supabase_migration_leave_airtable.sql` has not been applied.

### Step 5 — Only run the SQL migrations if Supabase itself needed to be recreated from scratch

If Step 4 confirms you're connected to the real, existing Supabase project, **do not replay its historical migrations**. Apply `supabase_migration_opportunity_state.sql` once if its `opportunity_processing` table is absent; apply `supabase_migration_opportunity_stages.sql` once if `pipeline_stage` / `checkpoint` are absent; apply `supabase_migration_content_hash.sql` once if `opportunities_cache.content_hash` is absent; apply `supabase_migration_organizations.sql` once if the `organizations` table is absent; apply `supabase_migration_opportunity_facts.sql` once if cache fact columns (`thematic_areas`, `locations`, `donor`, `discovered_at`) are absent; apply `supabase_migration_award_relationships.sql` once (after organizations) if `award_observations` / `relationship_edges` are absent. The older semantic-dedup and win/loss migrations are only for a genuinely new project (i.e., the old one is truly gone, not just temporarily unreachable).

### Step 6 — Recreate the systemd timers

These live outside this repository (`~/.config/systemd/user/`) and do not come back from `git clone`. Recreate the four service/timer pairs described in [Automatic Scheduling](#automatic-scheduling-systemd), plus `cortech-market.service` / `cortech-market.timer` from `deploy/systemd/` (Phase 3 observed-data digest).

```bash
systemctl --user daemon-reload
systemctl --user enable --now cortech-discovery.timer
systemctl --user enable --now cortech-assortis.timer
systemctl --user enable --now cortech-deadline.timer
systemctl --user enable --now cortech-winloss.timer
systemctl --user enable --now cortech-market.timer
loginctl enable-linger $(whoami)
```

### Step 7 — Confirm, don't assume

```bash
python main.py --once
```

Watch it run end to end at least once before trusting the timers to run unattended. Check that a real, previously-known opportunity is correctly recognized as already-seen (confirms Supabase connectivity is real, not just credential-shaped), and that a new BID/WATCH row gets a `crm_status` on `opportunity_processing`.

### If everything is gone — GitHub and Supabase, not just the local folder

This is a genuinely worse scenario and there's no clever recovery from it — it means starting over:

- **The code** can only be rebuilt from whatever `CURSOR_TASK_*.md` files, this README, or chat history with whoever helped build it survive elsewhere. There is no shortcut here.
- **`data/cvs/` and `data/proposals/`** — these are real business documents. Their home is the Supabase Storage bucket `Cortech-documents`; if Supabase is gone too, they must be re-sourced from wherever the *original* files came from (email, a shared drive, whoever originally supplied the CVs and past submissions) — they cannot be regenerated from code. Worth an occasional `python sync_source_documents.py pull` onto a machine that is backed up.
- **Supabase's embeddings** can be rebuilt from scratch once `data/cvs/` is recovered, by re-running the population and embedding scripts — but this means re-processing everything, and any opportunity/dedup history is genuinely gone.
- **Rate cards** are seeded by `supabase_migration_leave_airtable.sql` from the list in `populate_rate_cards()`. Donor-intelligence notes that existed only in Airtable were not copied. Consultant availability that was never stored on an embedding stays Unknown.

---

## Working With This Repo (Cursor Workflow)

Development on this project has followed a consistent pattern: design and diagnosis happen in conversation, and implementation happens via detailed prompts handed to Cursor. Each `CURSOR_TASK_*.md` file in the repo root is a historical record of one such prompt — they're worth reading even after being implemented, since they explain *why* a piece of code is shaped the way it is, not just what it does.

**Two things worth carrying forward from painful experience:**
- **Verify state before trusting it.** Several bugs in this project's history weren't new mistakes — they were previously-fixed bugs silently reappearing because a Cursor session regenerated a file from an older snapshot. When in doubt, `grep` for the specific pattern before assuming a fix is still in place.
- **Test the actual failure mode, not just that code compiles.** Nearly every `CURSOR_TASK_*.md` file ends with a testing section for exactly this reason — a change that "looks right" and a change that's actually been verified against a real failure case are not the same thing.
