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
3. **Analyze** — Claude reads the full document (as untrusted data) and extracts structured fields. A **deterministic scorer** (`intelligence/bid_scorer.py`, weights in `intelligence/scoring_model.json`) calculates FIT / WIN / STRATEGIC / RISK and BID / WATCH / NO-BID. Claude's own numeric score is stored only as an audit field.
4. **Match & price** — team CVs matched semantically against requirements, then checked for explicit geography/years (missing education is UNKNOWN, never inferred). A budget is built from the real rate card. Skipped entirely for EOI-stage and NO-BID opportunities to avoid spending on work that isn't needed yet.
5. **Draft** — a strategy is decided once, then every section is drafted against it and against real past-proposal structure (extracted from 60+ real submissions, not an assumed template).
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
│   ├── airtable_client.py       # CRM layer — human-facing records, NOT the source of truth for data
│   └── supabase_client.py       # pgvector storage, semantic search, dedup, embeddings
├── intelligence/
│   ├── analyzer.py               # Claude extraction (advisory scores only)
│   ├── bid_scorer.py             # Deterministic FIT/WIN/RISK + versioned weights
│   ├── scoring_model.json        # score_version 1.0.0 — changing this does not rewrite old records
│   ├── cv_matcher.py             # Semantic CV matching + explicit capability overlay
│   ├── compliance.py             # SATISFIED/PARTIAL/MISSING/UNKNOWN matrix
│   ├── budget_calculator.py     # Rate-card-based budget generation
│   ├── proposal_writer.py       # Section generation, strategy, style-guide grounding, quality self-score
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
├── utils/claude_helpers.py      # get_text() — safe response parsing, use everywhere a Claude call is made
├── extract_style_guide.py       # One-time script: derives real structure/style guides from data/proposals/
├── populate_airtable.py         # One-time/occasional onboarding: loads real CVs and past proposals
├── check_schema.py              # Diagnostic: validates Airtable field names against what the code expects
├── data/
│   ├── cvs/                      # Real team CVs (source for embed_cvs.py)
│   └── proposals/                # Real past submissions (source for style-guide extraction + past-work context)
├── intelligence/style_guides/   # Generated by extract_style_guide.py — human-readable, review before trusting
├── supabase_migration_*.sql     # Schema migrations — run once each in the Supabase SQL editor
├── .env.example                  # Every variable that needs a real value in .env — .env itself is gitignored
└── CURSOR_TASK_*.md              # Historical implementation prompts — a record of design decisions and why
```

---

## Prerequisites

Accounts needed, all free-tier-capable except where noted:

| Service | Used for |
|---|---|
| [Anthropic Console](https://console.anthropic.com) | Claude API — analysis, drafting, review |
| [OpenAI Platform](https://platform.openai.com) | `text-embedding-3-small` — CV/opportunity/lesson embeddings |
| [Supabase](https://supabase.com) | pgvector storage, semantic search |
| [Airtable](https://airtable.com) | Human-facing CRM dashboard |
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

```bash
python main.py --once
```

Watch the output. A clean run should show discovery, filtering, and (if anything qualifies) analysis and drafting, ending in a "Pipeline Complete" summary panel. If it errors immediately, it's almost always a missing or malformed `.env` value — `require_env()` and `get_anthropic_client()` fail loudly if required keys are missing, which is deliberate.

---

## Environment Variables Reference

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys → Create Key |
| `OPENAI_API_KEY` | platform.openai.com → API Keys |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Supabase project → Settings → API. **Use the service role key**, not the anon key — this is server-side code, not a browser client. |
| `AIRTABLE_API_KEY` | airtable.com/create/tokens — a personal access token scoped to the base below |
| `AIRTABLE_BASE_ID` | Open the base in Airtable, the ID is in the URL (`app...`) |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` | Google Account → Security → 2-Step Verification → App Passwords. **Not your real Gmail password** — this won't work with one. |
| `RESEND_API_KEY` / `EMAIL_SENDER` | resend.com → API Keys, and a verified sending domain/address. Listed in `.env.example`. |
| `IMAP_HOST` / `IMAP_PORT` / `IMAP_USERNAME` / `IMAP_PASSWORD` | Whatever mail provider hosts the inbox receiving the ICA newsletter — likely different credentials than `GMAIL_ADDRESS` above, do not assume they're interchangeable |
| `CHECK_INTERVAL_HOURS` | How often the main discovery pipeline runs, in hours. Defaults to `6`. |
| `HEALTHCHECK_URL` | Optional. A Healthchecks.io-style ping URL for dead-man's-switch monitoring. Safe to leave blank — every call site checks for this being empty first. |
| `FULL_DRAFT_FOR_WATCH` | `true` (current default) generates the full proposal even for WATCH-tier opportunities. Set to `false` for the lightweight cover-letter-only path. |
| `ANTHROPIC_TIMEOUT_SECONDS` / `ANTHROPIC_MAX_RETRIES` | Claude call timeout (default 180s) and SDK retries (default 2). |
| `MAX_OPPORTUNITIES_PER_RUN` | Cap on drafts per discovery run. Default `15`. |

`python main.py` fails at startup if `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SUPABASE_URL`, or `SUPABASE_SERVICE_KEY` are missing. Airtable is still fail-open.

**`.env.example` is placeholders only.** If an older copy ever contained real keys, rotate them.

**None of these are recoverable from git.** `.env` is deliberately excluded from version control — see [Disaster Recovery](#disaster-recovery--rebuilding-from-zero).

---

## Running the Agent

```bash
python main.py --once                    # Run the full discovery pipeline once
python main.py                            # Old always-on scheduler loop — superseded by systemd, kept for reference
python main.py --submit-url "<url>"       # Manually process one specific tender URL immediately
python main.py --run-assortis             # Manually trigger just the newsletter check
python main.py --run-deadline-check       # Manually trigger just the deadline-escalation check
python main.py --run-winloss              # Manually trigger just the win/loss lesson extraction
python -m pytest tests/ -q                # Unit tests (no live APIs)
```

`--submit-url` bypasses the free discovery filters (a human explicitly chose this URL) but still respects exact-URL dedup — resubmitting the same URL warns rather than silently reprocessing, and proceeds anyway since it was a deliberate choice.

---

## Automatic Scheduling (systemd)

Since this runs locally rather than on an always-on host, scheduling uses `systemd --user` timers rather than the old in-process scheduler loop. The key property: **`Persistent=true`** means a missed run (because the machine was off or asleep) fires automatically the next time the machine is up, rather than silently never happening.

Four timers, each invoking one of the CLI flags above:

| Timer | Fires | Runs |
|---|---|---|
| `cortech-discovery.timer` | Every `CHECK_INTERVAL_HOURS` | `main.py --once` |
| `cortech-assortis.timer` | Daily, ~11:45 | `main.py --run-assortis` |
| `cortech-deadline.timer` | Daily, ~08:00 | `main.py --run-deadline-check` |
| `cortech-winloss.timer` | Daily, ~08:15 | `main.py --run-winloss` |

Unit files live in `~/.config/systemd/user/` — **outside this repository**, so they are not recoverable from git and need to be recreated separately if lost (see Disaster Recovery). Check status any time with:

```bash
systemctl --user list-timers --all
```

---

## Data Stores at a Glance

**Airtable is the human-facing CRM dashboard only — it is not the source of truth for anything the code depends on to function.** A failed Airtable write is treated as non-fatal everywhere in the pipeline; the run still completes and the team still gets a draft, just without a CRM record until Airtable's available again.

| Store | Holds | Recoverable if lost? |
|---|---|---|
| Supabase (`opportunities_cache`, `cv_embeddings`, `proposal_embeddings`, `win_loss_memory`) | All vector search, dedup, embeddings — the real operational data | Survives independently in the cloud; only credentials need re-adding to `.env` |
| Airtable (`OPPORTUNITIES`, `CONSULTANTS`, `PAST_PROPOSALS`, `RATE_CARDS`, `PIPELINE_TRACKER`, `AGENT_LOGS`, `DONOR_INTELLIGENCE`) | Human-browsable records, rate cards, donor knowledge | Survives independently in the cloud; only credentials need re-adding |
| `data/cvs/`, `data/proposals/` (in this repo) | The real source documents everything else is built from | Recoverable from git **if committed** — verify this is actually true for your clone, don't assume |
| `intelligence/style_guides/*.md` (in this repo) | Extracted structure/style guides | Regenerable by re-running `extract_style_guide.py` against `data/proposals/`, if that folder survives |

**`AGENT_LOGS` has hit Airtable's free-tier 1,000-record cap more than once.** If Airtable writes start failing with sustained 429 errors even after retries, this is almost always the cause — it's pure operational logging, safe to bulk-delete without affecting anything the code depends on.

---

## Known Issues & Hard-Won Lessons

These have each caused real, confirmed production failures. Documented here specifically so they don't get silently reintroduced by a future Cursor session working from a stale file:

- **Always use `get_text(response)` from `utils/claude_helpers.py`, never `response.content[0].text` directly.** A response that leads with a thinking block breaks the naive pattern with `'ThinkingBlock' object has no attribute 'text'`. This exact bug has been fixed and has **regressed twice** in this project's history, each time because a file got regenerated from an outdated base.
- **Never write `dict.get(key, default)[some_slice]`.** `.get()` only substitutes the default when the key is *missing* — if the key exists but its value is `None`, `.get()` returns `None`, and slicing it crashes with `'NoneType' object is not subscriptable`. Use `(dict.get(key) or default)[slice]` instead. This has also regressed once.
- **Railway SMTP is blocked at the platform level** (irrelevant now that this runs locally, but relevant again if ever redeployed to a similar host) — `email_report.py` tries Gmail SMTP first, falls back to Resend's HTTP API. Both paths need real, working credentials for delivery to succeed; a failure in one silently masks whether the other is even configured.
- **`.env` must never be committed.** It was tracked in git history for a period early in this project before being corrected — if that history was ever shared or the repo was ever public, treat every credential used at that time as compromised and rotate it, regardless of whether this has already been done.
- **NO-BID and EOI-stage opportunities intentionally skip CV matching and budget calculation** — this is a deliberate cost optimization, not a bug. The NO-BID label now comes from the deterministic scorer, not from Claude's integer.
- **Claude's `cortech_fit_score` is advisory.** Official FIT/WIN/recommendation are calculated in `intelligence/bid_scorer.py`. Do not "fix" a score by prompting Claude to return a different number.
- **`.env.example` must never contain live keys.**

---

## Disaster Recovery — Rebuilding From Zero

**Read this before you need it.** This assumes the local folder — this entire directory — has been lost (stolen laptop, disk failure, accidental deletion). It does **not** assume Supabase, Airtable, or the GitHub repo have also been lost; those are separate cloud services and almost certainly still exist. A section for the more extreme "everything is gone" case is at the end.

### Step 1 — Re-clone the code

```bash
git clone <your-github-repo-url>
cd cortech-bd-agent
```

If this succeeds, all Python code, `data/cvs/`, `data/proposals/`, the extracted style guides, and every `CURSOR_TASK_*.md` design record are back. **`.env` will not be — that's expected, it's gitignored on purpose.**

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
2. `OPENAI_API_KEY` — same, at platform.openai.com.
3. `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` — **your Supabase project and its data are almost certainly still alive.** Log into supabase.com, find the existing project (don't create a new one), and pull the URL and service role key from Settings → API. Creating a *new* project here would mean starting with an empty database, losing every embedding and cached opportunity that isn't the point of this step.
4. `AIRTABLE_API_KEY` / `AIRTABLE_BASE_ID` — same logic: the base still exists in your Airtable account. Generate a new personal access token if the old one isn't recoverable, but point it at the *existing* base ID, findable in that base's URL.
5. `GMAIL_APP_PASSWORD` — app passwords aren't recoverable, only regeneratable. Google Account → Security → App Passwords → create a new one. The Gmail address itself is unaffected.
6. `RESEND_API_KEY` — resend.com → API Keys → create a new one if needed.
7. `IMAP_PASSWORD` (and host/username if those changed) — whoever manages that mailbox.

### Step 4 — Verify you're connected to the *existing* cloud data, not empty new instances

Before running anything that writes data, confirm the Supabase and Airtable credentials point at your real, existing project/base:

```bash
python check_schema.py
```

This validates the Airtable base's field names against what the code expects — if it reports missing tables or fields entirely, you may be pointed at the wrong base ID, not a genuinely broken schema.

### Step 5 — Only run the SQL migrations if Supabase itself needed to be recreated from scratch

If Step 4 confirms you're connected to the real, existing Supabase project, **skip this step** — the schema and all embedded data are already there. Only run `supabase_migration_semantic_dedup.sql` and `supabase_migration_win_loss.sql` in the SQL Editor if you genuinely had to create a brand-new Supabase project (i.e., the old one is truly gone, not just temporarily unreachable).

### Step 6 — Recreate the systemd timers

These live outside this repository (`~/.config/systemd/user/`) and do not come back from `git clone`. Recreate the four service/timer pairs described in [Automatic Scheduling](#automatic-scheduling-systemd) — the exact unit file contents are documented in `CURSOR_TASK_local_scheduling.md` if that file is present in this clone.

```bash
systemctl --user daemon-reload
systemctl --user enable --now cortech-discovery.timer
systemctl --user enable --now cortech-assortis.timer
systemctl --user enable --now cortech-deadline.timer
systemctl --user enable --now cortech-winloss.timer
loginctl enable-linger $(whoami)
```

### Step 7 — Confirm, don't assume

```bash
python main.py --once
```

Watch it run end to end at least once before trusting the timers to run unattended. Check that a real, previously-known opportunity is correctly recognized as already-seen (confirms Supabase connectivity is real, not just credential-shaped), and that an Airtable record appears for anything new (confirms Airtable connectivity).

### If everything is gone — GitHub, Supabase, and Airtable, not just the local folder

This is a genuinely worse scenario and there's no clever recovery from it — it means starting over:

- **The code** can only be rebuilt from whatever `CURSOR_TASK_*.md` files, this README, or chat history with whoever helped build it survive elsewhere. There is no shortcut here.
- **`data/cvs/` and `data/proposals/`** — these are real business documents. If they only ever lived in this repo, they need to be re-sourced from wherever the *original* files came from (email, a shared drive, whoever originally supplied the CVs and past submissions) — they cannot be regenerated from code.
- **Supabase's embeddings** can be rebuilt from scratch once `data/cvs/` is recovered, by re-running the population and embedding scripts — but this means re-processing everything, and any opportunity/dedup history is genuinely gone.
- **Airtable's rate cards, donor intelligence, and consultant availability data** were hand-entered — if lost, they need to be hand-entered again. Worth an occasional manual export as insurance specifically because of this.

---

## Working With This Repo (Cursor Workflow)

Development on this project has followed a consistent pattern: design and diagnosis happen in conversation, and implementation happens via detailed prompts handed to Cursor. Each `CURSOR_TASK_*.md` file in the repo root is a historical record of one such prompt — they're worth reading even after being implemented, since they explain *why* a piece of code is shaped the way it is, not just what it does.

**Two things worth carrying forward from painful experience:**
- **Verify state before trusting it.** Several bugs in this project's history weren't new mistakes — they were previously-fixed bugs silently reappearing because a Cursor session regenerated a file from an older snapshot. When in doubt, `grep` for the specific pattern before assuming a fix is still in place.
- **Test the actual failure mode, not just that code compiles.** Nearly every `CURSOR_TASK_*.md` file ends with a testing section for exactly this reason — a change that "looks right" and a change that's actually been verified against a real failure case are not the same thing.
