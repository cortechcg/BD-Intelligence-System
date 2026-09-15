# ADR 009 — Evidence-bound competitor and relationship facts (Phase 5)

## Status

Accepted

## Context

This is Phase 5 of the S-Tier BD Intelligence OS upgrade. It does **not**
start Phase 6 (a calibrated win model), likely-bidder inference, a
competitor graph product, or an account-management CRM.

Scorecard / architecture / remaining-gap lines this ADR implements
(quoted, not paraphrased), **before** any table or extractor was written:

- §19 Competitor Intelligence **0/10**: “Not implemented.”
- §19 Relationship Intelligence **0/10**: “Not implemented.”
- `docs/ARCHITECTURE.md` empty-scaffolding warning: “The design remains
  modular in one Python process. No empty microservices or unimplemented
  “intelligence domains” were added.”
- `docs/ARCHITECTURE.md` “Not in this architecture”: “Competitor
  intelligence, external market-research products, relationship graphs,
  executive-brief products, knowledge-graph services, a UI, or extra LLM
  agent loops. Those were out of scope and are not stubbed.”
- `docs/CURRENT_STATE.md` (pre-Phase 5): “This is not yet a competitor,
  relationship, account-management, or executive intelligence product.
  … There is still no competitor table or calibrated win-probability
  model. … Phase 5 (competitor & relationship intelligence) was not
  started.”
- §20: “Competitors, relationships, and strategic recommendations remain
  later. **Phase 5 was not started here.**”
- Highest-risk warning for this phase: empty scaffolding. Only build
  what has a real data source. VERIFIED only with a citation a human can
  open. Never infer a winner from silence (Cortech did not win ≠ firm X
  won). Never invent partners from “we typically work with…”.

ADR 003 (UNKNOWN is honest) and ADR 006 (one org identity system:
normalize + exact/fuzzy, never force-merge below threshold) remain in
force. Names go through `intelligence/organizations.py`. This ADR does
not invent a second identity graph.

### Source inventory (done before implementation)

**Discovery sources that exist in `monitors/` today:**

| Source | What the code actually reads | Winner / award-firm field? |
|---|---|---|
| RSS (`config.RSS_FEEDS`) | Empty list. No feeds configured. | No source at all. |
| Somali Jobs Playwright scraper (`monitors/scraper.py`) | Listing page `https://www.somalijobs.com/tenders`, selector `a[href*='/tenders/']`. Title + link + surrounding summary for the three-gate filter. | **No.** Open-tender listings only. No award/results parser, no winner CSS field, no `/awards` source. |
| Assortis/ICA newsletter (`monitors/assortis_email.py`) | IMAP HTML. Assortis `bsc_view.asp` links with `DataType=busop` become bid opportunities. ICA `ProjectByNewsletter` links are member-posted projects (the member who *posted*, not who *won*). | **Busop: no.** **ICA project news: no** (poster ≠ winner). |
| Assortis `DataType=contract` | Present in the same newsletter HTML the agent already parses. Currently **skipped** (`data_type != busop`) with the comment that these are award notices, not open tenders. Live Assortis contract pages publish a labelled **Awarded Firm(s):** block with contractor names and a stable listing URL (`bsc_view.asp?id=…&DataType=contract`). `processors/downloader.py` already fetches Assortis listing pages for busop. | **Yes — this is the only current source that publishes winner names.** |

The ICA newsletter fixture (`tests/fixtures/ica_newsletter_sample.html`)
has `0 contracts` that day. Silence on a given send is not a winner.
Somali Jobs and RSS are documented gaps: **do not stub a competitors
table for those sources.**

**Relationship source that exists on disk today:**

`data/proposals/` contains Cortech’s own past submissions. Several name
real counterparties in JV / consortium / commissioned-partnership
language a human can open, including:

- `…IoM_Monitoring Framework…pdf` — “submitted by Cortech Consulting
  Group Ltd. (SO) in joint venture with SPI – Sociedade Portuguesa de
  Inovação SA (PT)”
- `Cortech Consulting Consortium_Technical Proposal…Somalia.pdf` —
  “in joint venture with IBF- IBF Expertise SA (BL)”
- World Bank MOH assessment narrative — “In partnership with BK Plus
  Europe and commissioned by the World Bank, Cortech…”
- CEF MEAL proposal — Fennec Fox Technology Associates (FFTA) as a
  named Joint Venture contributor under Cortech as lead
- `EOI_SFERE_CORTECH.pdf` — “SFERE as Leader of the Consortium and
  Cortech Consulting Group as Partner”

“Lead Consultant” as a *person role*, client-side consortia (LWF/DKH…),
donor CFP subcontracting policy, “we have also partnered with leading UN
agencies”, and “Cortech submits as a sole firm with no JV partner” are
**not** relationship facts.

Analyzer `recommended_external_partners` is an LLM suggestion for *this*
tender. It is not a stored relationship.

## Decision

1. **No empty domains.** Competitor facts are implemented only for
   Assortis `DataType=contract` award notices that actually contain a
   labelled winner field. Relationship facts are implemented only from
   `data/proposals/` (and, when present, `proposal_embeddings`
   `content_chunk` text already stored from those files). Somali Jobs,
   empty RSS, ICA project-poster names, and “likely bidders” are not
   stubbed.

2. **VERIFIED is code-gated, not model-gated.** A competitor row is
   stored only when all of these hold: a public `source_url`, a winner
   name taken from a labelled award field on that page/text, and a
   verbatim `excerpt` that contains that name. A relationship row is
   stored only when a document name + chunk id + verbatim excerpt
   contain both Cortech as a party and the named counterpart, using
   deterministic patterns (`submitted by … in joint venture with`,
   `in partnership with … and commissioned by`, `as Leader of the
   Consortium and Cortech`, named JV contributor). LLM numbers stay
   advisory; this phase does **not** call Claude to invent partners.
   Patterns were sufficient on the real corpus. Optional LLM extraction
   is not added, because garbage-in would look like a graph.

3. **Never from silence.** A busop/tender page with no Awarded Firm
   field stores nothing. Cortech not winning does not create a
   competitor. A proposal that does not name a counterpart stores
   nothing. “We typically work with…” stores nothing.

4. **One identity system (ADR 006).** Winner and partner names pass
   `match_organization`. Exact spaced/compact is VERIFIED identity;
   fuzzy ≥ 0.95 with existing guards is INFERRED identity; below
   threshold is a new candidate / UNKNOWN — never a silent merge of two
   different firms. `entity_kind` stays `unknown` unless the name
   already exists as client/donor. No second org table.

5. **Untrusted award HTML is data.** Page text is length-capped.
   `wrap_untrusted()` is applied at the ingest boundary (ADR 002).
   Extraction is regex on the page text, not on model output. Injection
   strings that are not inside a labelled Awarded Firm block are not
   stored as winners. Huge names are capped at `MAX_ORG_NAME_CHARS`.

6. **Supabase additive, fail-open, not applied hosted.**
   `supabase_migration_award_relationships.sql` adds
   `award_observations` and `relationship_edges` with URL/document
   citations. Do not apply from this session. Missing tables, missing
   `organizations`, or missing `proposal_embeddings` fail-open (empty
   list, no crash). Airtable is unchanged (`check_schema.py`: no
   competitor or partner field exists; inventing one would fail bulk
   writes).

7. **Surface is cited facts only.** Review email may show a small
   “Cited past partners” block when stored relationship edges exist
   (HTML-escaped, document + excerpt). Market digest may show a small
   “Cited award winners” block when stored Assortis award rows exist;
   otherwise one honest gap sentence — not a fake ranking. Nothing
   submits. No likely-bidder list on the current tender.

8. **Phase 6 is out of scope.** WIN PROBABILITY stays the heuristic in
   ADR 001. No calibrated win model, no outcome regression.

## Consequences

- Competitor Intelligence becomes a narrow, tested Assortis award-firm
  extractor with citations — not a win-rate or “who will bid” product.
  Live history is empty until `DataType=contract` items appear in the
  newsletter *and* the migration is applied. Unapplied migration →
  fail-open → no rows, which is honest.
- Relationship Intelligence becomes cited JV/consortium/commissioned-
  partner edges from Cortech’s own files — not an account graph.
  Generic past-client name-drops stay out.
- Somali Jobs and RSS remain documented zeros for winners.
- Golden-set extraction/scoring must not move.
- Phase 6 (calibrated win model) is not started here.

Pytest on 2026-09-15 (`~/cortech-bd-agent/cortech/bin/python -m pytest tests/ -q`):

```text
307 passed in 16.00s
```

Golden-set extraction vs Phase 0–4 baseline: consultancy P/R/acc 1.000. Equal, not worse.
`supabase_migration_award_relationships.sql` was **not** applied to a live project in this session. No live Assortis award fetch and no `--extract-relationships` corpus write were run as part of implementation (unit tests use fixtures and one on-disk IOM JV PDF).
