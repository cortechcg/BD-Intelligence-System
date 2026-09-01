# Current state (2026-09-01)

Honest inventory of what this repository **actually does today**, versus what a “BD Intelligence OS” directive would ask for. No fictional architecture.

Evidence basis: source files in this repo (OBSERVED). External Airtable/Supabase contents were not re-queried for this document (UNKNOWN unless noted).

---

## What this system is

A **local, scheduled BD pipeline** for Cortech Consulting Group. It discovers tenders, filters cheaply, reads documents, asks Claude for structured analysis, matches CVs in Supabase, prices from an Airtable rate card, drafts a proposal, and emails humans. Nothing is submitted to a client automatically.

Entry point: `main.py` (`--once`, `--submit-url`, `--run-assortis`, `--run-deadline-check`, `--run-winloss`, or the in-process scheduler).

---

## What already works (preserve)

| Capability | Where | Status |
|---|---|---|
| RSS discovery + three-gate keyword filter (permissive by design) | `monitors/rss_monitor.py` | OBSERVED — `quick_relevance_check()` is the free gate; Claude does quality filtering |
| Playwright scraper (Somali Jobs) | `monitors/scraper.py` | OBSERVED — one live source |
| Assortis/ICA newsletter via IMAP | `monitors/assortis_email.py` | OBSERVED — `dedup_url` because Assortis rewrites tokens daily |
| PDF/DOCX/HTML extract + Google Drive packs + Playwright fallback | `processors/downloader.py` | OBSERVED |
| Claude RFP analysis → JSON, including `is_consultancy_contract` | `intelligence/analyzer.py` | OBSERVED — **numeric fit/win/recommendation are LLM numbers** |
| Consultancy gate defaults TRUE when field missing | `main.py` | OBSERVED |
| Exact-URL dedup + title embedding near-dup | `database/supabase_client.py` | OBSERVED — near-dup is used on Assortis; RSS/scraper use exact URL only |
| CV semantic match + availability annotate | `intelligence/cv_matcher.py` | OBSERVED — semantic-only match; availability is annotation, not a hard filter |
| Rate-card budget | `intelligence/budget_calculator.py` | OBSERVED |
| Section-by-section proposal + ToR brief + style guides + past-proposal retrieval | `intelligence/proposal_writer.py`, `intelligence/tender_reader.py`, `intelligence/style_guides/` | OBSERVED |
| Win/loss lesson extraction | `intelligence/learning.py` | OBSERVED |
| Airtable CRM writes (fail-open, circuit breaker, fail-fast retry) | `database/airtable_client.py` | OBSERVED |
| Email reports + docx | `reporting/` | OBSERVED |
| CVs, past proposals, embeddings, populate/embed scripts | `data/`, `populate_airtable.py`, `embed_cvs.py` | OBSERVED on disk; live vector contents UNKNOWN without querying Supabase |
| Config: `CLAUDE_MODEL` / `CLAUDE_MODEL_PROPOSAL`, Anthropic timeout/retries | `config.py` | OBSERVED |

Pipeline flow in `process_opportunity()` (OBSERVED): fetch → Supabase cache → `analyze_rfp` → consultancy gate → LLM NO-BID gate → Airtable create → CV match → budget/EOI-or-proposal → Airtable status Reviewing → email.

---

## What the directive asks for vs reality

| Directive theme | Today | Gap |
|---|---|---|
| Hybrid bid score (LLM extracts, code scores) | LLM `cortech_fit_score` / `win_probability` / `bid_recommendation` are the numbers stored and gated on | P1 — must not remain the final score |
| Separate FIT / WIN / COMMERCIAL / STRATEGIC / RISK + EV | Collapsed into two LLM numbers + a label | Missing |
| Versioned weights | None | Missing |
| Capability match beyond cosine | Cosine + availability flag | Partial |
| Provenance on retrieved chunks | Past-work context includes title/client/year/similarity | Partial — no `[NOT VERIFIED]` discipline in writer |
| Compliance matrix with SATISFIED/PARTIAL/MISSING/UNKNOWN | Airtable field `compliance_matrix` exists in `check_schema.py` EXPECTED list; **no writer in Python** | Missing |
| Competitor / relationship / knowledge graph / exec brief / chatbot | Not present | **Out of scope this pass** — do not stub |
| execution_id, structured cost logs, error taxonomy | Plain loguru + Airtable AGENT_LOGS; cost is fabricated at $3/MTok regardless of model | P0 |
| Env validation at startup | `get_anthropic_client()` fails loud on missing Anthropic key; other required vars fail later at import/use | P0 |
| Untrusted document wrapping | Tender pack is concatenated into the user prompt; writer has BEGIN/END tender markers but analyzer does not treat content as untrusted | P0 |
| Empty/corrupt extraction rejected loudly | Char-count skip in `main.py` (`MIN_FETCHED_CHARS=200`); no DOCUMENT_ERROR taxonomy; empty PDF returns `""` and is skipped | Partial |
| Tests | **Zero test files** | P0 |
| Secrets in source | `.env.example` currently contains **live-looking credentials** (OBSERVED in file). `.env` is gitignored. | P0 — example file must be placeholders |
| `get_text()` everywhere | Used in proposal_writer, tender_reader, learning, rss deadline extract. **Regressed** in `analyzer.py`, `budget_calculator.py`, `supabase_client.summarize_for_embedding`, `populate_airtable.py` | P0 |
| Airtable `typecast=True` on every create/update | create() has it; **`update_opportunity` does not** | P0 |
| `log_agent_action` never raises | Wrapped in airtable_client. **RSS monitor error path calls it unwrapped** | P0 |

---

## Security (pre-change, OBSERVED)

- Document downloader follows redirects to any `http(s)` URL, including localhost/link-local/private IPs (SSRF if a listing URL is hostile).
- Storage path uses `url.split("/")[-1]` as filename (path traversal if a URL path contains `..`).
- SSL verification can be disabled on retry (`verify=False`).
- Tender/PDF/email text is interpolated into Claude prompts without an explicit “this is data, not instructions” boundary in the analyzer.
- AGENT_LOGS / logger can include exception strings; API keys are not dumped in full except analyzer 401 path which logs last 4 chars (OK). Cost field is not a secret issue.
- `.env.example` is not an example — it holds real-looking secrets.

---

## Ingestion / resilience (pre-change)

- Airtable: fail-fast retry (`backoff_factor=0.5`, `total=2`) + 90s circuit. OBSERVED — recent hang from `backoff_factor=5` is already fixed.
- Anthropic client: timeout 180s, max_retries 2.
- RSS: `feedparser.parse(url)` has no explicit HTTP timeout.
- One failed source in `run_pipeline()` does not kill other sources (try/except per source). OBSERVED.
- Dedup: exact `source_url` string; no canonicalization (http vs https, trailing slash, `utm_*` create duplicates). Near-dup is title embedding at 0.90, Assortis-only.

---

## What this pass will and will not do

**Will:** P0 stability/security/observability/config/document-quality/tests; P1 hybrid scoring wired into `process_opportunity`; a thin capability-match and compliance-matrix hardening if scoring is solid; docs that describe the resulting system.

**Will not:** competitor intelligence, relationship graphs, executive-brief products, a chatbot, microservices, 10 empty “intelligence OS” packages, Airtable schema invention (new scores stored inside existing `claude_analysis` JSON + in-memory result; `relevance_score` / `win_probability` / `bid_recommendation` fields keep their names but receive **code-calculated** numbers).
