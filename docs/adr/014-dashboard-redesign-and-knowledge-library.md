# ADR 014 — Dashboard redesign and Knowledge Library

Status: accepted for the library and the committee reading. The visual-system
replacement in the brief is specified and not yet the live stylesheet.
Date: 2026-10-22.

## Context

Phase 0 was read before this file was written. The facts below were
re-checked in the tree. They are the codebase brief.

### 1. Route inventory

GET: `/`, `/opportunity`, `/job/{trigger_id}`, `/portfolio`, `/draft`,
`/track-record` (was `/design-preview`), `/healthz`, `/auth/login`, `/auth/google`,
`/auth/callback`, `/auth/logout`, `/api/queue`, `/api/opportunity`,
`/api/draft-templates`, `/api/job/{trigger_id}`, `/api/portfolio`.

POST, every one CSRF-checked: `/trigger/url`, `/trigger/existing`,
`/trigger/retry`, `/trigger/rerun`, `/trigger/bulk`, `/trigger/cancel`,
`/opportunity/reviewed`, `/opportunity/final-grade`, `/opportunity/outline`,
`/opportunity/outcome`, `/api/opportunity/outline`.

`test_dashboard_views.py` pins that POST list and forbids `submit`, `send`,
`email`, `publish`, `award`, and `apply` in any route path. The web module
does not import a client-facing sender and does not call
`submit_single_url` or `process_opportunity`.

### 2. Screen-by-field map

- Login (`login.html`): `product_name`, Google button, configuration reason
  when `auth_configured()` is false. H1 is the product name.
- Denied (`denied.html`): title and the reason from `email_allowed()`.
- Queue (`queue.html`): counts `halted`, `choose_outline`, `drafted`,
  `in_flight`, `terminal_skip` from `queue_view()`. Rows use `item.title`,
  client, donor, deadline, recommendation, scores, `stage_rail()`, and
  `ui_state`. Start-a-bid posts to `/trigger/url`.
- Detail (`detail.html`): title, client, donor, reference, source URL,
  deadline, recommendation, stage rail, lease state, attempt and draft-fail
  counts, fit/strategic/commercial/risk factors, strengths/gaps/risks, ToR
  fields, outline studio, team match, budget (missing rate stays incomplete),
  requirement alignment, claim checks, final-grade form, review and outcome
  forms. No send control.
- Job (`job.html`): kind label, status, error, spend, opportunity link.
- Portfolio (`portfolio.html`): stage census, recommendations, machine
  states, outcomes, runs, market digest. HTML bars are the canonical chart.
- Redesign preview (`redesign.html`): a second visual system with sample
  copy. It is not the live desk.
- Outline (`_outline.html`): chapter form, `/api/draft-templates`, local
  draft key `cortech_outline_draft:`.

Stages: agent `discovered → extracted → scored → drafted`. Human
`reviewed → outcome`. Lease and UI states include `pending`, `processing`,
`failed`, `completed`, `dead_letter`, plus synthetic `spend_cap` and
`cancelled`, plus `no_ledger` for cache-only rows.

`queue_bucket()` returns `halted`, `drafted`, `terminal_skip`,
`choose_outline`, or `in_flight`. The brief's four groups
(needs-human, in-flight, halted, done) are a presentation of those buckets.
`choose_outline` is the needs-human bucket. `terminal_skip` stays visible.
It is not dropped.

### 3. Tests that pin design or route behaviour

- `test_dashboard_views.py`: POST list, forbidden path words, no sender
  import, outline copy, final-grade copy, no send control, dead-letter and
  spend-cap copy.
- `test_dashboard_palette.py`: parchment/moss hex, AA pairs, no `gradient(`,
  shadow allowlist, two fills, brass only for a human action, Instrument
  Sans + Newsreader, font files on disk.
- `test_dashboard_parity.py`: dashboard runs enter through
  `main.submit_single_url`. This test is not changed.
- `test_dashboard_controls.py`, `test_dashboard_auth.py`,
  `test_embed_cvs.py`, `test_sql_migrations.py`, `test_render_yaml.py`,
  `test_email_safety.py`.
- `test_final_grade.py`: the web queues extracted text; the worker grades
  with `CLAUDE_MODEL`. That uncommitted fix is on this branch and must not
  be reverted. The live web service still has the old handler until this
  branch is deployed. The web service must not receive `ANTHROPIC_API_KEY`.

### 4. Risks

- `issue_session` stores `email`, `name`, and `iat` only. `picture` is an
  argument and is not written. The callback calls
  `issue_session(claims["email"], claims.get("name", ""))`. Avatars need an
  initials fallback. Storing `picture` is a later session change.
- `canonicalize_url` is a tender-URL normalizer. A synthetic
  `library://<uuid>` must not pass through it. `enqueue` keeps that scheme
  untouched.
- `dashboard_triggers.trigger_kind` is a CHECK. `library_ingest` is rejected
  by the database until `supabase_migration_knowledge_library.sql` is
  applied. This ADR does not apply it.
- `consultants.thematic_expertise` and `languages` are JSONB, not `TEXT[]`.
  Library sectors on `library_documents` are `TEXT[]` as specified. A new
  consultant stores JSON arrays.
- `embed_consultant_cv` writes `day_rate_usd: 0` into embedding metadata
  when the field is absent. The Library never writes
  `consultants.day_rate_usd`, `availability_status`, or
  `availability_percentage`.
- Two stylesheets exist (`app.css` and `redesign.css`). Replacing the pinned
  palette in the same change as the library would force every palette
  assertion to move at once. The library ships on the current desk. The
  brief's tokens, Fraunces/Inter, glass, and dark theme stay the accepted
  target and are not pretended to be live.
- Strict CSP with no inline script would break the poll snippet in
  `base.html` if shipped halfway. CSP stays as it is until the inline script
  moves into a module in the same change.
- The web service has no model key. Extraction for the library happens on
  the worker. Upload on the web stores bytes and a queue row only.

## Decision

1. Knowledge Library rows live in `library_documents` and `library_events`.
   Permissions are enforced in Python. RLS is unchanged because the web uses
   the service key server-side only.
2. Ingest of a library file is a `dashboard_triggers` row with
   `trigger_kind = library_ingest` and `source_url = library://<uuid>`.
   The worker downloads, extracts, and embeds. The web does not.
3. Proposal text is embedded by one function,
   `intelligence.library_ingest.embed_proposal_text`. `embed_proposal_from_file`
   and the worker both call it. CV text still goes through
   `embed_consultant_cv`. `sync_source_documents.py push` stays a byte mirror
   into `proposals/` and `cvs/`. Library objects use those prefixes, so
   `pull` already mirrors them. Push does not embed.
4. A draft's committee reading is the traced alignment, expressed as a mark
   out of each criterion's weight (full, half, or zero). It names the gap
   and the 80% pass mark. It is not a buyer award. The draft is still kept
   only when traced alignment is at least 80%.
5. `uploaded_by_email`, `uploaded_by_name`, `archived_by`, and `actor_email`
   come from the session. A form field with those names is ignored.
6. `LIBRARY_ADMIN_EMAILS` is a web-service variable. Empty means only the
   uploader can archive their own document. Restore is admin-only.
7. Update 2026-10-24: `/design-preview` is retired and replaced by
   `/track-record`; glass is described in docs/DASHBOARD_DESIGN.md section 16.
   Original note: palette, glass, and the retirement of `/design-preview` wait for one
   change that updates `test_dashboard_palette.py` on purpose. This slice
   does not delete that test and does not change its colour pins. The
   allowlist now also names the committed rail-icon hairline, the sidenav
   fill, and `--color-tint` as a surface tint rather than a second primary
   fill. `app.css` is unchanged.

## Consequences

Apply `supabase_migration_knowledge_library.sql` after
`supabase_migration_dashboard_controls.sql` with
`python scripts/apply_sql_migration.py supabase_migration_knowledge_library.sql`.
Until that runs, library enqueue fails the kind CHECK. Do not apply it
from this change set against production without a person doing it.

The Grade button on the deployed web service still fails until the queued
final-grade fix on this branch is deployed to both services. The key stays
on the worker.
