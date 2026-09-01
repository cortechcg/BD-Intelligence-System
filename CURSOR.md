# CORTECH BD INTELLIGENCE AGENT — PROJECT CONTEXT

## What this project is
Autonomous business development pipeline for Cortech Consulting Group,
an East Africa development-sector consultancy (4-8 core staff).
Monitors tender portals, analyzes RFPs with Claude, matches internal
consultant CVs, drafts proposals, emails the team for human review.
Nothing submits to clients automatically — human approval required.

## Tech stack
- Python 3.12 + virtualenv at ~/cortech-bd-agent/cortech/
- Airtable (pyairtable) = human-facing CRM dashboard
- Supabase (supabase-py + pgvector) = CV vector store + document cache
- Anthropic Claude = chat (claude-sonnet-5 analysis, claude-fable-5 proposals)
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
database/airtable_client.py
reporting/email_report.py
scripts/populate_airtable.py
scripts/embed_cvs.py
check_schema.py            ← schema diagnostic, run before bulk writes

## Airtable critical rules — NEVER break these
1. Every .create() call MUST use typecast=True or multi-select fields
   throw 422 INVALID_MULTIPLE_CHOICE_OPTIONS errors
2. log_agent_action() MUST be wrapped in try/except — it is called
   inside every error handler; if it throws, it cascades and kills
   the entire pipeline
3. discovered_at field is a plain DATE field — send strftime("%Y-%m-%d")
   never .isoformat() — full timestamps get rejected
4. year field in PAST_PROPOSALS is NUMBER not DATE — send int not string
5. won field in PAST_PROPOSALS is CHECKBOX — send Python bool
6. All field names are lowercase_with_underscores — exact match required

## Supabase critical rules
1. OPENAI_API_KEY required for embeddings — text-embedding-3-small for vectors
   ANTHROPIC_API_KEY required for all chat / analysis / drafting
2. Embedding model MUST be identical between embed_cvs.py and
   supabase_client.py — mixing models = silent garbage match results
3. import os required at top of supabase_client.py
4. SUPABASE_URL must have https://, no trailing slash, no quotes

## Pipeline flow (main.py process_opportunity)
1. Fetch document text
2. Store in Supabase cache (dedup on source_url)
3. Claude analysis → structured JSON
4. is_consultancy_contract gate — FALSE = save as NO-BID, return None
5. Create Airtable OPPORTUNITIES record
6. CV matching via Supabase pgvector
7. Budget calculation via rate card
8. Compliance matrix via Claude
9. Proposal draft via Claude Fable 5 (section by section)
10. Update Airtable status to Reviewing
11. Send proposal email to full team

## is_consultancy_contract gate (CRITICAL)
The LLM returns this boolean in bid_analysis.
Default is TRUE (fail open — never miss a real opportunity).
FALSE only for pure staff vacancies with no deliverables.
When FALSE: save as NO-BID in Airtable, return None, stop pipeline.
This gate prevents burning proposal tokens on CV matching and proposal
writing for job postings.

## Filter philosophy (monitors/rss_monitor.py quick_relevance_check)
PERMISSIVE by design. Two gates only:
1. Hard reject obvious staff vacancy language in title
2. Require ONE signal — geography OR thematic keyword
The LLM is_consultancy_contract gate does the real quality filtering.
Do NOT make the RSS filter strict — it kills real opportunities.

## Models
CLAUDE_MODEL = "claude-sonnet-5"            # analysis + extraction
CLAUDE_MODEL_PROPOSAL = "claude-fable-5"    # proposal writing
Both in config.py — never hardcode model strings in other files.
Embeddings: text-embedding-3-small (unchanged).

## Known failure modes — always check these when debugging
- UNKNOWN_FIELD_NAME → field name typo in Airtable, run check_schema.py
- INVALID_MULTIPLE_CHOICE_OPTIONS → missing typecast=True
- discovered_at rejected → sending .isoformat() to a DATE field
- Embeddings returning garbage → different model in embed vs query
- os not imported in supabase_client.py → NameError on module load
- log_agent_action throwing → breaks pipeline, wrap in try/except
- Scraper returning 0 results → httpx can't render JS, use Playwright
- Staff vacancies passing filter → RSS filter too loose OR
  is_consultancy_contract not checked in main.py

## Run commands
python main.py --once     # single run, manual test + crontab
python main.py            # continuous scheduler every 6 hours
python check_schema.py    # verify all Airtable field names before bulk ops
python scripts/embed_cvs.py  # load CVs into Supabase (run after populate)
