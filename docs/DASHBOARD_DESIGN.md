# Dashboard design direction

> **2026-09-22, night: light cream + forest green (§14) is current.** A
> subtractive pass: one cream surface, forest ink, brass only for "a person
> needs to act". Structure and the Outfit face carry over; the surface stack,
> tinted fills, uppercase headings and highlight pills from §9–§13 are gone.
> Screenshots: `docs/design-pass/simple-before-*` / `simple-after-*`.

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


---

## 9. Auros reskin (2026-09-22)

A token swap plus component remap onto the Auros style reference ("abyssal
terminal with bioluminescent data orbs"): dark teal surface stack, silver and
platinum text, lavender-phosphor for statistics, one gradient button per
screen. The structure from §1–§7 is untouched; `dashboard/static/app.css`
carries the Auros custom-property block verbatim and a role layer that maps
every dashboard role onto one of those tokens.

### 9.1 Type

| Role | Face | Note |
|---|---|---|
| The system's voice: labels, tables, headings, figures | **DM Sans** (variable, OFL 1.1) | The reference's substitute for Matter (Inter, DM Sans or Satoshi). DM Sans is the geometric grotesk of the three; Satoshi's Fontshare licence forbids redistribution, so it could not be committed. Self-hosted as one woff2 with `LICENSE-DM-Sans-OFL.txt` beside it; `tests/test_dashboard_palette.py` fails on any CDN reference. |
| Document and model text | Source Serif 4 | Unchanged. Auros has no serif, but the serif-means-a-document-said-it boundary is structural, not skin. |
| Identifiers | IBM Plex Mono | Kept. It sits well on the teal stack; nothing in the palette argues for a swap. |

Weights are Auros's two only: 400 body, 500 headings and figures — no bold,
no light. The test file rejects any other `font-weight`. Uppercase tracked
labels (0.055–0.12em) return for eyebrows, table heads, stat labels and
buttons because the reference makes them signature. Body is set at 14px, not
the reference's 16px: this is a dense operations tool, and 16px body wraps
the queue table on a laptop. Figures use the reference scale (36px counters,
61px hero), display tracking −0.02/−0.04em, line-height 1.

### 9.2 Token map

| Dashboard role | Auros token | Value |
|---|---|---|
| page canvas | `--color-liquid-abyss` | `#012624` |
| raised card: panels, tables, tiles, bands | `--color-liquid-kelp` | `#003734` |
| recessed: inputs, footer well, warning band on the sign-in card | `--color-liquid-deep` | `#011d1c` |
| headings, nav, human-review colour | `--color-platinum` | `#ffffff` |
| body, notes, table text | `--color-silver-mist` | `#bbc7c6` |
| emphasised body, document text, stat labels | `--color-liquid-mist` | `#edfffe` |
| large statistics only (figures, hero, stat, tile, meter values) | `--color-lavender-phosphor` | `#fde9ff` |
| the one primary button per screen | `--gradient-aurora-gradient` with abyss text | — |
| hairlines, hovers, rail track | white at 4–18 % alpha | — |
| quiet button and arrow-icon fill | `rgba(3, 81, 75, 0.5)` (the reference's Arrow Icon Button fill) | — |

Why the **aurora** gradient on the button and not the bioluminescent one the
brief named: the bioluminescent sweep starts at `#00827c`, which is 3.44:1
under dark text and 4.68:1 under white — no single text colour clears 4.5:1
along its whole length. The reference's own Gradient Pill Button component
uses the aurora gradient with dark text, and that pair is 12–15:1 end to end.
The bioluminescent gradient's two ends instead supply the rail ramp (§9.3).

### 9.3 The rail and machine state — meaning that had to survive

| Rail state | Token | Value | How it is told apart without hue |
|---|---|---|---|
| agent stage 1–4, done or current | `--stage-1…4` | `#0a9d95 → #43b1ab → #78cbc6 → #a8e5e1` | a four-step ramp between the gradient's ends, validated on kelp (light end 3.93:1, all ΔL ≥ 0.06), capped well below white |
| human review, awaiting a person | `--human` | `#ffffff`, **dashed, hollow** | lightness ≥ 1.3× every stage step (test-enforced), plus shape |
| human review, a person has acted (reviewed/outcome) | `--human` | `#ffffff`, filled | |
| the machine stopped on this stage | `--halt` | `#fad1ff` (the aurora gradient's pink stop) | solid fill in a stage position; the state chip says the word |
| not reached | `--track` | white at 12 % | |

Machine-state dots follow the same logic: teal filled = completed, aqua
filled and pulsing = processing, slate ring = pending, pink ring = failed
(retryable), pink filled = dead-letter, white ring = stopped by a cap or a
person (resumable). The literal ledger word is always printed beside the dot,
as before. Evidence labels: VERIFIED teal dot, INFERRED aqua ring, UNKNOWN no
dot. Recommendation stays achromatic weight-and-shape: BID filled white,
WATCH white outline, NO-BID slate outline; the test rejects any status or
stage token inside a chip rule.

The reference's "Don't use any colour outside the teal scale, silver neutrals
and lavender" is honoured: `#fad1ff` is a stop of its own aurora gradient and
`#0a9d95` is the gradient's teal end lifted from 2.8:1 to 3.9:1 on kelp so it
survives as a mark. No red, green or amber remains anywhere in the UI.

### 9.4 Contrast, recomputed on the dark palette

`tests/test_dashboard_palette.py` (55 checks) resolves every role alias to its
hex and asserts the pairs the stylesheet actually composes. The run that
shipped:

```
text ≥ 4.5   platinum on abyss 16.10 · on kelp 13.16    mist on abyss 15.60 · on kelp 12.74 · on deep 17.03
             silver on abyss 9.28 · on kelp 7.58 · on deep 10.13
             lavender (stats) on kelp 11.45 · on abyss 14.02   pink (halt) on kelp 9.79 · on abyss 11.99
             abyss text on aurora cyan end 14.76 · on aurora pink end 11.99 · on the filled BID chip 16.10
marks ≥ 3.0  teal 3.93 on kelp · stage ramp 3.93 / 4.83 / 7.81 / 12.06 · pink 9.79 · white 13.16
rejected     dark text on bioluminescent teal 3.44 (why the button is aurora)
             slate-deep #707777 as text 2.88 (rings and borders only; test-enforced)
```

Ramps through the dataviz validator (dark mode, kelp surface):

```
"#0a9d95,#43b1ab,#78cbc6,#a8e5e1" --ordinal   → ALL CHECKS PASS  (light end 3.93:1, ΔL ≥ 0.06, hue spread 3°)
"#707777,#bbc7c6,#ffffff"         --ordinal   → monotone L PASS, ΔL PASS, light end 2.88:1 PASS; "single hue" FAIL
```

The recommendation ramp's one failure is the validator computing a hue spread
on three near-greys; an achromatic ramp has no hue to keep. Its light end is
2.88:1, so the relief rule stays in force: every bar carries a direct label
and a table view.

### 9.5 Deviations from the reference, each on purpose

- **Radii 16/6 plus one 2px.** Cards 16, controls and chips 6. Rail segments,
  meter and bar tracks are 6–14px tall; 6px there makes pills, which the
  reference forbids, so those marks use 2px (test-enforced list).
- **Section gap 48px, not 68; card padding 32, not 36–48.** Dashboard density.
- **Body 14px, not 16.** As above.
- **Serif kept for document text.** Structural (see §2 of the first pass).
- **Focus ring and live pulse are the only `box-shadow`s** (test-enforced).
  No elevation shadow anywhere; depth is abyss → deep → kelp.
- **Colour literals exist only in `:root`** — hex and `rgb()` alike
  (test-enforced), and Portfolio bar colours are passed as `var(--stage-n)` /
  `var(--rec-n)` from app.py rather than hex.

### 9.6 Polish pass, same day — six named gaps

Screenshots: `docs/design-pass/polish-before-*` / `polish-after-*`.

1. **Rail.** Never dropped: it is the Stage column of every queue table
   (96px, 8px tall after this pass) and the labelled bar on Detail and Job.
   The reskin's mapping (§9.3) stands.
2. **Needs attention.** The two buckets that mean "a person should act" borrow
   the rail's human signal: a 2px dashed white left edge on the figure. Halted
   runs additionally set the number in the halt pink. At zero both go quiet
   (muted number, hairline edge). No new colour was introduced.
3. **Surfaces float.** Verbatim kelp is 1.22:1 against abyss and cards did not
   read as raised when squinting. The card *role* is now `#004843`, a kelp step
   lifted in the same hue: 1.54:1 against the canvas, 1.68:1 above the recess,
   and the teal mark stays at 3.12:1 on it (the next lighter step drops teal to
   2.83:1, which is why it stops here). The `--color-liquid-kelp` token itself
   is untouched. Text on the new surface: silver 6.02:1, mist 10.13:1,
   lavender 8.84:1, pink 7.78:1 — all recomputed, all in the test file, plus a
   new assertion that the step is ≥ 1.5:1.
4. **Jobs table as an instrument.** `.table--jobs`: fixed column widths, ids,
   timestamps, spend and domains in Plex Mono, kind and requester on one line,
   spend right-aligned, rows on the 12/24 spacing steps. The state chip is now
   a tinted pill (fill from the state's own tint token, dot for shape, the
   ledger word in uppercase). The same class and columns are used on Queue
   (recent jobs), Detail (dashboard jobs) and Portfolio (dashboard runs).
5. **Type scale.** Figures and stat tiles move to the reference's heading-lg
   step: 61px, weight 500, −2.44px tracking, line-height 1. The Detail hero
   moves to 86px at −0.046em — the reference's own oversized-kinetic setting.
   Detail stats and the meter value sit at 36px. Weight stays 500 (the
   reference has no light weight).
6. **Spacing.** Page top 48, section gap 64, panel and strip padding 40, tiles
   36, chart 36/40, auth card 48, footer well 36/40 — each a step on the Auros
   scale. Table rows deliberately stay tight (12px vertical) so the tables read
   as instruments inside spacious cards rather than spacious tables.


---

## 10. Seline reskin (2026-09-22)

Token swap plus component remap onto the Seline Analytics reference ("quiet
analyst's desk on warm paper"): stone canvas, white hairline cards, one cyan
accent, display type at weight 400 with tight tracking. Structure unchanged
since §1–§7 and the jobs-table instrument from §9.6. Portfolio was built
first as the reference screen. Screenshots: `docs/design-pass/seline-before-*`
/ `seline-after-*`.

### 10.1 Type

| Role | Face | Note |
|---|---|---|
| Headings, figures, disclosure titles | **Inter Tight** (OFL 1.1), weight 400 only | The reference's substitute for Roobert. 52px display at −1.092px, 32px at −0.8px, 20px at −0.1px. Never bolder than 400 at display sizes (test-enforced). |
| Body, labels, tables, buttons | **Inter** (OFL 1.1) | 14px at 1.64 — the reference's dominant rhythm — with +0.004em tracking (test-enforced). Restored from this repo's own history. |
| Document and model text | Source Serif 4 | Unchanged, structural. |
| Identifiers | IBM Plex Mono | Unchanged. |

Both new faces are one variable woff2 each, self-hosted with their OFL text
beside them. DM Sans is removed.

### 10.2 Token map

| Dashboard role | Seline token | Value |
|---|---|---|
| page canvas | `--color-stone-canvas` | `#fafaf9` |
| cards, tables, tiles, inputs | `--color-pure-white` | `#ffffff` |
| hairlines (the structure) | `--color-stone-border` / `--color-stone-muted` | `#e8e6e5` / `#d6d3d1` |
| headings, figures, primary text | `--color-ink-black` | `#0c0a09` |
| body, notes, table text | `--color-warm-gray` | `#78716c` |
| disabled, missing figures, rings only | `--color-ash-gray` | `#a8a29e` |
| BID chip, completed marks, rail done | `--color-soot` | `#1c1917` |
| the one filled button, focus ring | `--color-cyan-signal` | `#3ba6f1` |
| marks and edges that mean "a person is needed" | `--color-cyan-edge` | `#3398e1` |
| the highlight pill, halted state pills | `--color-sky-wash` | `#c1e1f7` |
| ordinal ramps | + Tailwind stone-700 | `#44403c` (the one added neutral, from the same scale) |

Shadows: `--shadow-md` on exactly one card per page — the hero instrument
(Queue strip, Detail score band, Job meter, the first Portfolio tile, the
sign-in card). Every other card is a 1px hairline. The only other
`box-shadow`s are the focus ring, the live pulse, and a 1px inset that edges a
halted rail segment (test-enforced).

### 10.3 Meaning that had to survive

| Signal | Treatment |
|---|---|
| agent stages done | soot (stone-700 for the first two, soot for the last two) |
| human review, awaiting a person | cyan-edge, dashed, hollow (2px when it is the current step) |
| human review, a person acted | cyan-edge filled |
| the machine stopped here | sky-wash fill with a 1px cyan-edge inset |
| state pills | pending: white/hairline; processing: white with a cyan edge and pulsing cyan dot; completed: stone-border fill, soot dot; failed / dead-letter / cap / cancelled: sky-wash fill, ink text, dot ring vs filled |
| recommendation | BID soot filled, WATCH ink outline, NO-BID warm-gray on a stone outline — achromatic, weight and shape |
| the one highlight per headline | sky-wash pill with **ink** text on the view's key real figure ("2 need attention", "23 scored of 26", the job's state) |

Cyan therefore means one thing across the whole tool: **a person is needed**
(act on this, review this, this stopped and waits for you). It is the same
meaning the reference gives it ("actions feel switched on").

### 10.4 Contrast, recomputed on the light palette

```
text ≥ 4.5   ink on canvas 18.92 · on card 19.76 · on sky-wash 14.47 · on cyan (button) 7.44
             warm-gray on canvas 4.59 · on card 4.80        white on soot (BID) 17.49
marks ≥ 3.0  cyan-edge on card 3.13 · soot 17.49 · stone-700 10.27 · warm-gray 4.80
rejected     cyan-edge text on sky-wash 2.29 (the reference's highlight span — not used as text)
             cyan-edge text on canvas 2.99 · white on cyan-signal 2.65 (the reference's CTA text — not used)
             cyan-signal as a mark on white 2.65 (marks use cyan-edge) · ash-gray as text 2.52 · warm-gray on sky-wash 3.51
```

Ramps (dataviz validator, light mode, white surface):

```
stage  "#a8a29e,#78716c,#44403c,#1c1917" --ordinal → ALL CHECKS PASS (light end 2.52:1 ≥ 2.0 floor; direct labels + table view kept)
rec    "#78716c,#44403c,#1c1917"         --ordinal → ALL CHECKS PASS (light end 4.80:1)
```

### 10.5 Deviations, each on purpose

- **Cyan is never text.** Three of the reference's own pairings fail AA (above).
  The highlight pill keeps the wash and takes ink text; the CTA keeps the cyan
  fill and takes ink text, which the reference's agent guide itself offers.
- **Warm-gray body never sits on sky-wash** (3.51:1): pills and the highlight
  use ink.
- **Section gap 64px, not 96; card padding 24 as specified.** Tool density.
- **2px radius on thin marks** (rail, meter, bars): 9999 would make pills of
  data, 10 would look wrong at 8px tall.
- **Serif kept for document text.** Structural.
- **Labels stay sentence case.** The reference has no uppercase-label component.


---

## 11. Dark green + cream hybrid (2026-09-22)

Asked for after §10 shipped: the white page was not wanted. The canvas returns
to Auros' abyssal teal; Seline's cream paper stays as the card surface, with
its type, pills, hairlines and highlight. Screenshots:
`docs/design-pass/hybrid-before-*` / `hybrid-after-*`.

### 11.1 What plays which role

| Role | Token | Value |
|---|---|---|
| page canvas / recess | `--color-liquid-abyss` / `--color-liquid-deep` | `#012624` / `#011d1c` |
| cards, tables, tiles, bands, disclosures | `--color-stone-canvas` (cream) | `#fafaf9` |
| inputs, table heads, pills inside cards | `--color-pure-white` | `#ffffff` |
| text on the canvas | cream / `--color-silver-mist` | `#fafaf9` / `#bbc7c6` |
| text inside cards | `--color-ink-black` / `--color-warm-gray` | `#0c0a09` / `#78716c` |
| the one filled button, marks, "a person is needed" | `--color-teal-deep` | `#006b66` |
| brand-mark corner and focus ring on the canvas | `--color-teal` | `#00827c` |
| the highlight pill, halted fills | `--color-aqua-wash` | `#cbfffc` |
| BID chip, completed marks, rail done | `--color-soot` | `#1c1917` |

Text colour follows context. `:root` sets `--ink`/`--ink-2` to cream/silver
for the canvas; one scope rule (`.panel, .table-wrap, .tile, .band, .empty,
.auth__card, .disclosure, …`) redefines them to ink/warm-gray inside any cream
card, so every component reads correctly on either surface without per-rule
edits. Cyan is gone; teal is the single accent and, as before, never text.

### 11.2 Contrast, recomputed

```
canvas   cream on abyss 15.42 · on deep 16.84      silver on abyss 9.28 · on kelp 7.58
cards    ink on cream 18.92 · warm-gray on cream 4.59 · ink on aqua wash 18.11
button   cream on teal-deep 6.10        (gradient teal #00827c: cream 4.48, ink 4.22 — both fail, hence the deeper step)
marks    teal-deep on cream 6.10 · soot 16.74 · stone-700 9.84 · teal on abyss 3.44 (brand mark, focus ring)
step     cream card vs canvas 15.42:1 — cards cannot fail to float
rejected warm-gray on aqua wash 4.40 (the pill is ink) · teal as text anywhere
```

### 11.3 Notes

- The hero card's shadow is invisible on the dark canvas; its lift is carried by
  a stone-muted border instead. Every other card is a cream hairline card.
- Disclosures (Detail ledger, Job request) became cream cards so their
  key/value text is ink, not warm-gray on green.
- `tests/test_dashboard_palette.py` names base tokens per surface (the role
  aliases now vary by context) and asserts the card step, the teal-never-text
  rule and the one-fill rule.


---

## 12. ORYZO darkroom skin (2026-09-22)

Token swap plus component remap onto the ORYZO reference ("darkroom product
editorial: a lone object floating in warm darkness, cream typography the
only decoration"). Structure unchanged. Screenshots:
`docs/design-pass/oryzo-before-*` / `oryzo-after-*`.

### 12.1 Type

| Role | Face | Note |
|---|---|---|
| Everything the system says | **Outfit** (variable, OFL 1.1), 400 and 500 only | The Halyard Display substitute (the reference offers Inter or Söhne; Outfit is the geometric grotesk of the free options and its wide caps carry uppercase at line-height 0.9). One family for headings, labels, figures and body, as the reference does. Self-hosted with its OFL text; Inter and Inter Tight removed. |
| Document and model text | Source Serif 4, mixed case | Unchanged, structural: serif means a document or model said it. `text-transform: none` is test-enforced. |
| Identifiers | IBM Plex Mono | Unchanged; keeps its case inside uppercase headings. |

Two typographic modes, as the reference specifies: **uppercase weight 500**
for headings, labels, nav, buttons, pills, table heads and disclosure titles,
with no letter-spacing adjustment; **mixed-case weight 400** for body copy at
14px (the reference's 29px body is a product page, not a tool). Display sizes
are the reference's: 51px hero figures and meter, 41px view titles and tile
figures, 24px section headings and stats, 18px subheads — all at line-height
0.9 above 36px so the figures stack as solid form.

### 12.2 Token map

| Dashboard role | ORYZO token | Value |
|---|---|---|
| canvas, and every outlined card | `--color-walnut-shadow` | `#100904` |
| the one elevated solid per screen: hero card, auth card, filled button | `--color-bark-brown` | `#382416` |
| hairlines, dashed dividers, rail track | `--color-cork-border` | `#40372e` |
| strong hairlines, quiet button borders, missing figures | `--color-driftwood` | `#6c5f51` |
| headings, figures, primary text, BID chip, completed marks, rail stages | `--color-warm-cream` | `#ffedd7` |
| the editorial accent: human-review segment, halted stage, "a person should act" edge, the one highlighted phrase per headline | `--color-ember-accent` | `#dc5000` |
| secondary text | **added** `--color-oat` | `#b8a894` |
| ramp step | **added** `--color-sandstone` | `#8f8070` |

The two additions are steps of the reference's own warm grey between
driftwood and cream. Driftwood as body text is 3.19:1 on walnut and 2.37:1 on
bark, so it cannot carry secondary copy; oat (8.5:1 / 6.3:1) does. Table heads,
pills and hover rows use bark at 45 % alpha — the same solid, thinner.

### 12.3 Meaning that had to survive

| Signal | Treatment |
|---|---|
| agent stages done | cream |
| human review, awaiting a person | ember, dashed, hollow (2px when it is the current step) |
| human review, a person acted | ember filled |
| the machine stopped here | solid ember in a stage position (shape and position tell it from the dashed human segment; the pill says the word) |
| state pills | bark-tinted pills with the dot: completed cream, processing ember and pulsing, failed ember ring, dead-letter ember filled, cap / cancelled oat ring |
| recommendation | BID cream filled with walnut text, WATCH cream outline, NO-BID driftwood outline — weight and shape, no hue |
| the one highlight per headline | ember text on the canvas (4.9:1). Inside a bark card the phrase stays cream with a 2px ember underline (ember text there is 3.6:1) |
| "a person should act" figures | 2px dashed ember left edge; the number stays cream |
| primary button | bark fill, cream text, 36px pill. Never ember: the reference forbids ember on buttons, and cream on ember is 3.5:1 anyway |
| ghost buttons | driftwood outline at 22.5px, cream on hover (the reference's cream outline at forty rows would out-shout the data) |
| inputs | underline only: transparent, 1px cream bottom rule, 0 radius, ember rule on focus |

### 12.4 Contrast, recomputed on the walnut palette

```
text ≥ 4.5   cream on walnut 17.26 · on bark 12.81      oat on walnut 8.53 · on bark 6.33
             ember highlight on walnut 4.88             walnut text on the cream BID chip 17.26
marks ≥ 3.0  ember on walnut 4.88 · on bark 3.62        cream on bark 12.81   driftwood on walnut 3.19
large text   driftwood (is-zero / is-missing figures, ≥ 24px) 3.19 on walnut
rejected     driftwood as body text 3.19 / 2.37 (→ oat)   ember text on bark 3.62 (→ cream + ember underline)
             cream on ember 3.53 (ember is never a fill)   driftwood as a mark on bark 2.37 (never placed there)
step         bark vs walnut 1.35:1 + a cork outline — squint-checked in the after-shots
```

Ramps (dataviz validator, dark mode, walnut surface):

```
stage  "#6c5f51,#8f8070,#b8a894,#ffedd7" --ordinal → ALL CHECKS PASS (light end 3.19:1, ΔL ≥ 0.06, hue spread 4°)
rec    "#6c5f51,#b8a894,#ffedd7"         --ordinal → ALL CHECKS PASS
```

### 12.5 Deviations, each on purpose

- **Body 14px at 1.45, not 29px at 1.26.** A tool showing forty rows.
- **Section gap 68px** (the reference's), but **no 100vh sections**: the
  dashboard is a scrolling instrument, not a product reveal.
- **Ghost buttons in driftwood**, not cream (above).
- **2px radius on thin marks**; the reference's 12px floor is for containers.
- **Serif kept for document text.** Structural.
- **The wordmark is type only**: the brand square is hidden, as the reference
  has no icon.
- **One shadow-free stylesheet.** The only `box-shadow` is the live-pulse
  keyframe (motion, not elevation), test-enforced.


---

## 13. Forest green + cream (2026-09-22)

A palette swap onto the ORYZO structure (§12): same Outfit type scale, same
spacing, same components. What changes is the atmosphere — deep forest green
instead of walnut — and one rule is restored from the navy/brass pass:
**brass means a person needs to act**, and means nothing else.

### 13.1 Values, chosen by the numbers

Every hex below was a draft until `tests/test_dashboard_palette.py` (WCAG 2.x)
and the dataviz validator passed it. Three drafts were rejected on the way:
a card at `#16291f` (1.12:1 above the canvas — cards did not float), a mid
green for rail stages (1.08:1 in lightness against brass — the human segment
disappeared), and a green→cream ordinal ramp (78° hue spread — two hues, not
one).

| Role | Token | Hex | Verified |
|---|---|---|---|
| recess: inputs, table heads, footer well | `--color-forest-recess` | `#06100b` | cream 16.87 · sage 10.51 |
| canvas | `--color-forest-canvas` | `#0b1a12` | cream 15.66 · sage 9.76 · brass 8.68 |
| card: tables, tiles, bands, disclosures | `--color-forest-card` | `#213e30` | **1.53:1 above the canvas**; cream 10.21 · sage 6.36 · brass 5.65 |
| hero card: the one lifted surface per screen | `--color-forest-hero` | `#2a4d3c` | **1.90:1 above the canvas**, 1.24 above the card; cream 8.24 · sage 5.13 · brass 4.56 |
| tracks and hairlines | `--color-forest-track` | `#35533f` | 1.37:1 on the card (a hairline, not text) |
| primary text, rail stages, the paper surface | `--color-cream` | `#f4efe6` | ink on paper 15.66 |
| paper on hover | `--color-cream-hover` | `#e6e0d4` | ink on it 13.65 |
| secondary text | `--color-sage` | `#b8c2b5` | ≥ 5.13 on every surface |
| disabled / missing figures (≥ 24px), rings | `--color-moss` | `#8a9788` | 3.82 on the card, 3.08 on the hero; **3.82 fails the 4.5 body floor (test-enforced: display figures only)** |
| a person needs to act | `--color-brass` | `#d4b05a` | ≥ 4.56 as text on every surface; 1.81 in lightness from the cream stages |
| Portfolio ramps | `--ramp-green-1…4` | `#4f7d63 #6f9c82 #93bba4 #bcd9c8` | one hue (1° spread), light end 2.47 on the card, relief rule applies |

### 13.2 Where each colour may appear

- **Paper (cream fill, ink text)** — the one filled button per screen, the
  highlighted phrase in a headline, the BID chip, and the marks (rail stages,
  meter fill, completed dots). Nowhere else (test-enforced).
- **Brass** — the human-review rail segment (dashed), a halted stage (solid),
  the NEEDS ATTENTION figure and its dashed edge, halted / processing state
  dots and pill borders, the halt band edge, and the queue headline's
  `hl--attention` phrase. Never a button, never a focus ring, never body text
  (test-enforced: brass as `color:` only inside a selector containing
  "attention").
- **Green steps** carry all elevation. No shadows (test-enforced).

### 13.3 The six checks from the last review, in this palette

1. Rail in every queue row — cream stages, brass human segment, brass halted
   stage, on the card step. `forest-after-queue-desktop.png`.
2. NEEDS ATTENTION — brass 51px figure with a brass dashed edge; the routine
   counts stay cream. Zero goes moss and loses the brass.
3. Cards float — 1.53:1 card and 1.90:1 hero against the canvas, both with a
   hairline; the test floor is 1.45 and 1.75.
4. Jobs table — fixed columns, Plex Mono ids/timestamps/spend/domains, pill
   states on the recess fill (test-enforced).
5. Figures — 51px hero and bucket figures, 41px tiles, weight 500 at
   line-height 0.9, unchanged from §12.
6. Spacing — 68px sections, 31px hero padding, 24px cards, unchanged.

### 13.4 Ramps

```
stage "#4f7d63,#6f9c82,#93bba4,#bcd9c8" --ordinal --mode dark --surface "#213e30" → ALL CHECKS PASS
rec   "#6f9c82,#93bba4,#bcd9c8"         --ordinal --mode dark --surface "#213e30" → ALL CHECKS PASS
```


---

## 14. Light cream + forest green — the subtractive pass (2026-09-22)

The brief: stop adding design decisions. Everything below is a removal or a
value; the structure (§0–§8) and the Outfit face (§12) are unchanged.

### 14.1 Values, chosen by the numbers

| Role | Token | Hex | Verified on cream `#f6f1e7` |
|---|---|---|---|
| the page, and every card | `--color-cream` | `#f6f1e7` | — |
| ink, lines, the one filled button, BID chip, rail done | `--color-forest` | `#1b4332` | **9.84:1**; cream on it 9.84 |
| rail current stage, ramp step | `--color-forest-mid` | `#4f7d63` | 5.1:1 as a mark; 1.9:1 from forest, so current ≠ done by lightness |
| secondary text | `--color-moss` | `#4f6b5d` | **5.19:1** (the first draft `#5c7566` was 4.45 — rejected) |
| disabled / missing figures (display size), rings | `--color-sage` | `#6b8177` | 3.4:1 — display figures only, test-enforced (the draft `#7f948a` was 2.87 — rejected) |
| hairlines | `--color-hairline` | `#d9d2c3` | 1.34:1 — a line, not text |
| tracks | `--color-track` | `#e6e0d2` | 1.17:1 |
| a person needs to act | `--color-brass` | `#7a5e12` | **5.43:1** as text (the dark-surface brass `#d4b05a` is 1.84 on cream — rejected; this is the first pass's brass-ink, §3.1) |
| Portfolio ramps | `--ramp-1…4` | `#7fa691 #4f7d63 #2f5f48 #1b4332` | one hue (5°), light end 2.40 ≥ the 2.0 floor (the draft `#a7c4b3` was 1.67 — rejected) |

### 14.2 What was removed, and why each was there out of habit

- **The surface stack** (recess / canvas / card / hero). One cream; a card is
  a hairline. The hero instrument keeps a 2px forest rule on top — the one
  exception the brief allows — and nothing else is tinted. Test: `--surface`
  must alias `--ground`; no `elevated`, `recess`, `paper` or `surface-2` token
  may exist; no `box-shadow` except the pulse keyframe and the inset focus
  ring; no `gradient(`.
- **Uppercase headings, buttons, nav, pills.** Only labels and the
  recommendation chips are uppercase now, tracked at 0.08em. Test: exactly
  two `text-transform: uppercase` rules.
- **The highlight pill.** A headline phrase is plain text; only the queue
  headline's count is brass, because it means "act".
- **Dashed dividers** under section heads and the masthead. Space does that
  job; a hairline remains only where it separates content (masthead, footer,
  table rows).
- **Filled table heads, filled state pills, filled quiet buttons.** All are
  outlines now. Test: forest is the only fill, and only on the primary
  button, the BID chip and marks.
- **The rail as a bar.** In queue rows it is a 4px line; done is forest,
  current is mid forest, a halted stage is brass, and the human-review
  segment is a 2px dashed brass rule — a different shape, not a filled block.
- **Brass as a ring, a fill or a focus colour.** Brass is text on the NEEDS
  ATTENTION figure and the queue headline, the dashed edge, the halted stage,
  halted state dots and pill borders, the halt band edge. Test-enforced.

### 14.3 What was added

Space. Page top 64px, section gap 96px, card padding 32px, hero padding 48px,
table cells 16×24px, body 15px at 1.6. Figures: 56px hero and bucket counts
(the NEEDS ATTENTION count at weight 500, the routine counts at 400), 44px
tiles and view titles, 24px supporting stats. That difference — size and
weight, not colour or boxes — is the hierarchy.
