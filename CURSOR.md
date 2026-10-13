# CORTECH BD INTELLIGENCE AGENT — PROJECT CONTEXT

## What this project is
Autonomous business development pipeline for Cortech Consulting Group,
an East Africa development-sector consultancy (4-8 core staff).
Monitors tender portals, analyzes RFPs with Claude, matches internal
consultant CVs, drafts proposals, emails the team for human review.
Nothing submits to clients automatically — human approval required.

## Tech stack
- Python 3.12 + virtualenv at ~/cortech-bd-agent/cortech/
- Supabase (supabase-py + pgvector) = consultants, rate cards, agent logs, opportunity status, CV vectors, document cache
- Claude = chat (Haiku 5.5 for analysis, scoring, technical proposals, and EOIs)
- OpenAI API = embeddings only (text-embedding-3-small, 1536-dim)
- Gmail SMTP = email reports

## Project structure
main.py                    ← orchestrator, entry point
config.py                  ← all constants, CORTECH_PROFILE
monitors/rss_monitor.py    ← RSS feeds + three-gate keyword filter
monitors/scraper.py        ← Playwright browser scrapers
processors/downloader.py   ← PDF/DOCX/HTML text extraction
intelligence/analyzer.py   ← RFP → structured JSON via Claude
intelligence/cv_matcher.py ← Supabase pgvector semantic search
intelligence/budget_calculator.py ← rate card × effort estimate
intelligence/proposal_writer.py   ← full proposal generation
database/supabase_client.py
database/airtable_client.py  ← Supabase CRM reads/writes (name kept)
reporting/email_report.py
scripts/populate_airtable.py
scripts/embed_cvs.py
check_schema.py            ← confirms Supabase CRM tables exist

## CRM rules — NEVER break these
1. Do not call api.airtable.com. Consultants, rate cards, agent logs, and
   opportunity status live in Supabase (`consultants`, `rate_cards`,
   `agent_logs`, columns on `opportunity_processing`).
2. log_agent_action() MUST be wrapped in try/except — it is called
   inside every error handler; if it throws, it cascades and kills
   the entire pipeline
3. submission_deadline is a DATE — send YYYY-MM-DD, never a full timestamp
4. A missing rate-card row leaves the budget incomplete. Do not invent a price.
5. A missing availability value is Unknown, never free.
6. Donor-intelligence notes were not copied. get_donor_intelligence() stays empty.

## Supabase critical rules
1. OPENAI_API_KEY required for embeddings — text-embedding-3-small for vectors
   ANTHROPIC_API_KEY required for all chat / analysis / drafting
2. Embedding model MUST be identical between embed_cvs.py and
   supabase_client.py — mixing models = silent garbage match results
3. import os required at top of supabase_client.py
4. SUPABASE_URL must have https://, no trailing slash, no quotes

## Pipeline flow (main.py process_opportunity)
1. Claim `opportunity_processing` lease; load `pipeline_stage` + checkpoint (resume)
2. Fetch document text (discovered → extracted)
3. Claude analysis → structured JSON + deterministic score (extracted → scored)
4. is_consultancy_contract gate — FALSE = save as NO-BID, return None
5. Write CRM status on the processing row (BID/WATCH)
6. Stop. Store a recommended outline (the ToR's chapters, or the house outline when the ToR lists none) and release the lease. Do not match CVs, price a budget, or draft.
7. A person selects that recommendation or a house template (technical or EOI), may edit the headings, and saves it on this opportunity (`POST /api/opportunity/outline`). The catalog itself is unchanged.
8. The next run matches CVs, calculates the budget, and drafts only the accepted outline (an EOI stays an EOI). Each extracted fact is given to one chapter. The draft is kept only when traced alignment to the extracted evaluation criteria is at least 80% (addressed counts full, partial counts half, ToR weights used when every criterion has one). A criterion counts as addressed only when its own words sit in a paragraph long enough to be an answer. A name dropped into a short line is partial. Below that, the writer rewrites the chapters that missed criteria. A draft that stays under 80% retries, then dead-letters. Before the file is kept, the cover letter is placed first and the executive summary second, and a later chapter loses a passage it repeats. Then set crm_status to Reviewing and send the proposal email. A pasted link can instead carry the chapters immediately (`intent=draft`): the file is still read, and the score and the requirements extract are skipped. With no extracted criteria, an 80% alignment score is not claimed.
Human only, from the dashboard: reviewed → outcome (Won/Lost). Crash mid-run resumes at the last persisted stage. A scored row with no accepted outline resumes by stopping again, not by drafting.
Store in Supabase cache only after durable complete (dedup on source_url).

## is_consultancy_contract gate (CRITICAL)
The LLM returns this boolean in bid_analysis.
Default is TRUE (fail open — never miss a real opportunity).
FALSE only for pure staff vacancies with no deliverables.
When FALSE: save as NO-BID on the processing row, return None, stop pipeline.
This gate prevents burning proposal tokens on CV matching and proposal
writing for job postings.

## Filter philosophy (monitors/rss_monitor.py quick_relevance_check)
PERMISSIVE by design. Two gates only:
1. Hard reject obvious staff vacancy language in title
2. Require ONE signal — geography OR thematic keyword
The LLM is_consultancy_contract gate does the real quality filtering.
Do NOT make the RSS filter strict — it kills real opportunities.

## Models
CLAUDE_MODEL = "claude-haiku-5-5"            # analysis + extraction + scoring + ToR reading
CLAUDE_MODEL_PROPOSAL = "claude-haiku-5-5"  # technical proposals and EOIs
Both in config.py — never hardcode model strings in other files.
Embeddings: text-embedding-3-small (unchanged).

## Known failure modes — always check these when debugging
- CRM write rejected → the processing row was missing, or crm_status was not one of New/Reviewing/Bidding/Won/Lost/No-bid
- submission_deadline dropped → the value was not YYYY-MM-DD
- Embeddings returning garbage → different model in embed vs query
- os not imported in supabase_client.py → NameError on module load
- log_agent_action throwing → breaks pipeline, wrap in try/except
- Scraper returning 0 results → httpx can't render JS, use Playwright
- Staff vacancies passing filter → RSS filter too loose OR
  is_consultancy_contract not checked in main.py

## Run commands
python main.py --once     # single run, manual test + crontab
python main.py            # continuous scheduler every 6 hours
python check_schema.py    # confirm Supabase CRM tables exist
python scripts/embed_cvs.py  # refresh CV embeddings from consultants (run after populate)
