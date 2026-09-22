# Dashboard design direction

Second pass, 2026-09-22. The first pass (2026-09-21) established the rules
that still hold: colour encodes evidence status, missing data is grey, the
serif/sans boundary marks document text, and one value primitive enforces
"never invent data". This pass replaces the *visual* system around those
rules, because the first one was functionally right and visually a 3/10.

Before/after screenshots for every view, rendered from real Supabase rows,
are in `docs/design-pass/` (`before-*` / `after-*`).

---

## 0. What was wrong (the critique that drove this pass)

Taken from the before-screenshots, not from memory:

1. **Nothing dominated.** Labels, notes, table headers, chips and states were
   all 11px uppercase grey. The two things a person checks dozens of times a
   day — how many runs are halted, and how much has been spent against the
   cap — were a sentence inside a bordered paragraph.
2. **The left spine cost 192px per section and held README copy.** Every
   section carried a paragraph about `opportunity_processing`, ADR numbers and
   CLI flags. The system talked about itself instead of about the tenders, and
   the tables it squeezed wrapped URLs into three mono lines per row.
3. **The generated-page cluster.** Cream paper, serif titles, hairline rules,
   zero radius, uppercase eyebrow labels, monospace for small labels, middle
   dots between meta items. Each is defensible alone; together they are the
   look of a page nobody chose.
4. **No button hierarchy.** "Draft this" appeared forty times in the same
   filled navy as the page's one primary action.
5. **The signature data — pipeline stage — was four 9px dots.** WIN
   PROBABILITY on the detail view was a 13px row in a definition list, the
   same weight as "Score version".
6. **The spend meter was a 14px track at the bottom of a key/value list.**
7. **Login and denied did not exist as screens.** Login bounced straight to
   Google; a refused login was a plain-text 403 in the browser's default
   monospace.
8. **Client history rendered raw Python dicts** in the citations list.

## 1. Direction: a desk instrument

Cortech writes technical and EOI proposals for UNICEF, UNDP, DRC and GIZ. The
tool should feel like an instrument on a bid desk: precise, quiet, credible,
expensive — not a SaaS product, not an admin template, and not a broadsheet.

The one memorable thing is the **pipeline rail**: a segmented bar, four navy
agent stages plus a fifth dashed brass segment for the human review that
follows. It appears at 76px in every table row, labelled and full-width at the
top of the detail and job views, all-empty in every empty state, and as the
mark on the sign-in card. Every fill is read from the ledger — `pipeline_stage`
sets done/current, the lease state colours a halted segment — so the signature
device is also the most honest one on the page. Everything else is kept
quiet so the rail and the large light-weight figures can carry the hierarchy.

## 2. Typography

| Role | Face | Why |
|---|---|---|
| Everything the system says: labels, tables, headings, figures | **IBM Plex Sans** (variable, 100–700) | A technical humanist grotesk designed for an enterprise, not a startup. Light (300) at display size gives the figures an annual-report calm; medium (500–600) carries headings without shouting. It is the sans sibling of the Plex Mono already in use. |
| Everything a document or model said: tender titles, evidence, reasons, gaps | **Source Serif 4** | Unchanged. Serif still means "this text came out of a document or a model"; sans means the application is talking. |
| Identifiers: URLs, ids, hashes, timestamps | **IBM Plex Mono** | Unchanged. Comparable character by character. |

Type scale (px): **12 · 13 · 14 · 16 · 20 · 26 · 34 · 48.** Body and tables
at 13; section headings 16/600; view titles 26/500; figures 34/300; the one
hero figure per view 48/300. Large figures use proportional numerals; table
columns use tabular. No uppercase labels anywhere: labels are sentence case,
13px, medium weight, in secondary ink.

All faces are self-hosted woff2 under `dashboard/static/fonts/`; the Inter file
from the first pass was removed. `tests/test_dashboard_palette.py` fails if
the stylesheet references a font file that is not in the repo.

## 3. Colour

### 3.1 Base palette

Deliberately off the cream. The ground is a matte, cool instrument-panel grey
and the ink is blue-black — not a tinted near-black.

| Token | Hex | Role |
|---|---|---|
| `--ground` | `#F2F3F5` | page |
| `--surface` | `#FFFFFF` | panels, tables, tiles |
| `--ink` | `#131C2B` | text (15.4:1 on ground) |
| `--ink-2` | `#566070` | secondary text (5.7:1 on ground) |
| `--navy` | `#1F3864` | the system's colour: primary action, BID, completed rail segments |
| `--brass-ink` | `#7A5E12` | **a person is needed here**: human review segment, human-gate band, caveat footnotes |

Every state colour has a text/mark step and a tint, and marks use the text
step so no dot or segment is below 3:1 on its own:

| State | ink | tint | Meaning |
|---|---|---|---|
| good | `#0B6B0B` | `#E4F3E4` | agent work completed |
| warn | `#765400` | `#FBF1D2` | spend-cap halt, cancelled — both resumable, never red |
| serious | `#963A14` | `#FBE6DC` | `failed`, retryable |
| critical | `#9B2C2C` | `#F8E2E2` | `dead_letter`, cap reached |

### 3.2 The rules that did not change

- **Missing data is grey, never red.** `--ink-2` is the colour of an em dash and
  its reason. The absence of data is never more salient than its presence.
- **BID / WATCH / NO-BID get weight and shape, not hue.** BID is a filled navy
  chip, WATCH an outlined one, NO-BID grey. `tests/test_dashboard_palette.py`
  fails if a recommendation chip borrows a status colour.
- **Red means the machine stopped.** Never a bad score.
- **Brass now has one meaning: a human.** In the first pass it was a decorative
  focus ring and a rule colour. Now it marks the human review segment, the
  human-gate band, and the caveat footnotes — the places where the tool hands
  off to a person.

### 3.3 Contrast, computed

Every text/background pair the stylesheet composes is checked by
`tests/test_dashboard_palette.py` (WCAG 2.x formula, 4.5:1 for text, 3:1 for
marks). The run that shipped this pass:

```
ink on ground 15.39  ink on surface 17.09  ink-2 on ground 5.73  ink-2 on surface 6.36
navy on ground 10.46  white on navy 11.62  brass-ink on brass-tint 5.28
good-ink on good-tint 5.84  warn-ink on warn-tint 6.13
serious-ink on serious-tint 5.99  critical-ink on critical-tint 6.08
marks: navy 11.62  navy-soft 6.00  good 6.73  warn 6.91  serious 7.21  critical 7.53
→ ALL PASS
```

Two marks were demoted by the numbers: raw brass `#C4A35A` (2.40:1) is no
longer used as a mark or ring, and the recommendation ramp was re-stepped so
its lightest bar clears 3:1:

```
$ node validate_palette.js "#a8873a,#7a5e12,#4d3a05" --ordinal --mode light --surface "#FFFFFF"
  [PASS] Lightness monotone   [PASS] Adjacent ΔL
  [PASS] Light-end contrast   #a8873a at 3.39:1 vs surface
  [PASS] Single hue           hue spread 1°
$ node validate_palette.js "#6291e3,#406cbb,#1e4994,#002972" --ordinal --mode light --surface "#FFFFFF"
  → ALL CHECKS PASS  (light end 3.14:1)
```

Bars still carry direct labels and a table view; that rule is kept even
though the ramps now pass.

## 4. Spacing, shape, layout

- **Space scale: 4 · 8 · 12 · 16 · 24 · 32 · 48 · 64.** Tokens `--sp1`…`--sp8`;
  the stylesheet uses nothing else. Sections are 48 apart, a section head is 12
  above its content, table cells are 12×16, panels pad 24.
- **Three radii, one per kind of thing:** 2px rail segments and meter tracks,
  4px controls and chips, 8px panels and tables.
- **Single column, 1240px max.** The left spine is gone. Each view opens with
  a title row (title left, the counts that matter right), then one
  *instrument* panel, then sections with a heading row (name left, count
  right) and a one-line note only where it changes what you do. Explanations
  that were paragraphs live in `docs/DASHBOARD.md`, where they always were.
- **Surfaces only where content is an instrument.** Tables, panels and tiles
  are white on the grey ground; bands and empty states sit directly on it.
  No shadows anywhere.
- **Button hierarchy.** One filled button per screen for the primary action
  (queue a tender, download the draft, resume a halted run). Row actions are
  outlined; cancel is a ghost.

## 5. What each view leads with

| View | Dominant | Recedes |
|---|---|---|
| Queue | Five figures (needs attention / in flight / awaiting review / stopped by design / discovered) and the aggregate spend meter, in one panel | the forty-row table of unclaimed cache rows, now with host instead of URL and a rail instead of a stage word |
| Detail | WIN PROBABILITY at 48px with its heuristic label, FIT, the recommendation chip, calibrated P(win) shown empty; then the labelled rail with the state | the factor table and weights, behind a disclosure whose summary states verified/inferred/unknown counts; the ledger row, behind another |
| Job | This run's spend as a large meter against its own cap, with 50 % and 90 % ticks; model calls, attempts, state | the raw request row, in an open disclosure |
| Portfolio | Coverage tiles first, then the two ordinal bar charts with consistent 10px marks and direct labels; the outcome census as tiles with brass caveat footnotes | the count tables, behind "Table view" disclosures |
| Sign in / denied | A card with the rail and one primary action; a denied login prints the exact reason in a critical band | — |

## 6. States

- **Empty** — every bucket renders a designed empty state: an all-empty rail,
  what is true, what it means. "Nothing needs attention — no run is halted,
  capped or dead-lettered." Never a bare "no data".
- **Live** — a running job's state dot pulses (respecting
  `prefers-reduced-motion`), the page polls, and the band says exactly what
  granularity exists: stage boundaries, spend, call counts. No spinner, no
  invented sub-step.
- **Halted** — the current rail segment takes the state colour and a calm band
  states the failure class and the recorded reason. Dead-letter and spend-cap
  copy is unchanged and still asserted by `tests/test_dashboard_views.py`.
- **Forms** — 3px navy focus ring on inputs, red-brown border on an invalid URL
  once typed, a primary button that is the only filled one on the screen, a
  bulk-draft button that reads "Draft 3 selected" and stays disabled at zero.

## 7. The one component that enforces "never invent data"

Unchanged from the first pass and still the only sanctioned way to render a
field: `value()` / `value_terse()` / `doc_value()` in
`dashboard/templates/_macros.html`. A missing value is a grey em dash plus the
literal reason; there is no CSS path that makes it look like a figure. Score
provenance is emitted with the score. The calibrated P(win) is shown and
empty, never hidden — it is now the fourth stat in the detail header.

## 8. Why the screenshots use real rows

Constraint 1 of the brief is "never invent data", and a design probe populated
with tidy invented tenders would validate the wrong thing. Every screenshot in
`docs/design-pass/` was rendered by the running app against the live Supabase
project. That is why the before/after pairs show scraped lowercase titles,
`title not stored` rows, a `Somaliland` tender with no client, and zero verified
Lost outcomes — and why two states the brief asked for (a run in flight, a
dead-lettered row) have no screenshot: neither existed in the data on
2026-09-22 and none was fabricated to get one.
