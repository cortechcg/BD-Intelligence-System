# main.py
"""
CORTECH BD INTELLIGENCE AGENT — MAIN ORCHESTRATOR
==================================================

Entry points:
  python main.py --once        → single run (used by crontab + manual testing)
  python main.py --submit-url  → process one URL on demand (web page, PDF, or
                                 a Google Drive folder with multiple annexes)
  python main.py               → continuous scheduler every CHECK_INTERVAL_HOURS

Pipeline (runs for every opportunity that passes the three-gate filter):
  1.  Discovery: Assortis newsletter + Somali Jobs scraper
  2.  Document download and text extraction
  3.  LLM analysis → structured JSON + is_consultancy_contract gate
  4.  Airtable opportunity record creation
  5.  CV semantic matching via Supabase pgvector
  6.  Budget calculation (rate card × effort estimate)
  7.  Compliance matrix generation
  8.  Full technical proposal draft (section-by-section)
  9.  Individual proposal email to full team (TOR link + budget + draft)
  10. Pipeline summary email at end of each run

No score threshold gates — every confirmed consultancy contract gets a
full proposal drafted. The human reviewer decides what to submit.
"""

import os
import sys
import json
import uuid
import schedule
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.progress import track

from config import MAX_OPPORTUNITIES_PER_RUN, RSS_FEEDS, require_env
from monitors.rss_monitor import monitor_rss_feeds
from monitors.scraper import scrape_non_rss_sources
from monitors.assortis_email import check_assortis_newsletter
from processors.downloader import fetch_and_extract
from processors.document_quality import assess_extraction
from intelligence.analyzer import analyze_rfp
from intelligence.bid_scorer import apply_bid_intelligence
from intelligence.compliance import build_compliance_matrix
from intelligence.cv_matcher import match_team_to_requirements
from intelligence.budget_calculator import calculate_budget
from intelligence.proposal_writer import generate_proposal, generate_eoi
from reporting.docx_builder import SECTION_ORDER
from intelligence.learning import process_win_loss_outcomes
from database.supabase_client import (
    check_opportunity_exists,
    claim_opportunity_processing,
    complete_opportunity_processing,
    fail_opportunity_processing,
    opportunity_ledger_available,
    store_opportunity,
)
from database.airtable_client import (
    create_opportunity,
    update_opportunity,
    log_agent_action,
)
from reporting.email_report import (
    send_report,
    send_proposal_email,
    send_deadline_alert_email,
    get_urgency_level,
)
from utils.errors import ErrorType
from utils.hashing import content_hash
from utils.observability import (
    configure_logging,
    get_execution_id,
    log_stage,
    new_execution_id,
    opportunity_usage,
    reset_opportunity_usage,
)
from utils.urls import canonicalize_url
from utils.healthcheck import ping_healthcheck

console = Console()

# A fetched ToR/listing page below this is a fetch failure, not a short
# document — matches MIN_USEFUL_CHARS in processors/downloader.py.
MIN_FETCHED_CHARS = 200
# A newsletter blurb is short by nature — matches MIN_BLURB_LENGTH in
# monitors/assortis_email.py. Only ever applied to the fallback text.
MIN_BLURB_CHARS = 40


@dataclass
class _ExecutionBudget:
    limit: int
    used: int = 0


_execution_budget: ContextVar[_ExecutionBudget | None] = ContextVar(
    "execution_budget", default=None
)
_pipeline_outcome: ContextVar[str] = ContextVar(
    "pipeline_outcome", default="retryable"
)


def _start_execution_budget() -> _ExecutionBudget:
    """Start a per-command hard cap at the actual processing boundary."""
    budget = _ExecutionBudget(limit=max(0, int(MAX_OPPORTUNITIES_PER_RUN)))
    _execution_budget.set(budget)
    return budget


def _remaining_execution_budget() -> int:
    budget = _execution_budget.get()
    if budget is None:
        return max(0, int(MAX_OPPORTUNITIES_PER_RUN))
    return max(0, budget.limit - budget.used)


def _reserve_processing_slot() -> bool:
    """Reserve one opportunity attempt; no command can exceed its cap."""
    budget = _execution_budget.get()
    if budget is None:
        # ``process_opportunity`` is also a public library entry point. Give a
        # caller that did not establish a run context the same hard ceiling
        # rather than silently creating an unbounded alternate path.
        budget = _start_execution_budget()
    if budget.used >= budget.limit:
        return False
    budget.used += 1
    return True


def _run_monitored(name: str, callback, *, heartbeat: bool = False):
    """Report a *full discovery* completion, never incidental job liveness.

    One HEALTHCHECK_URL represents the configured discovery pipeline. Deadline
    alerts, manual submits, and maintenance jobs must not make a failed
    discovery run look healthy merely because they happened to succeed.
    """
    try:
        result = callback()
    except BaseException:
        if heartbeat:
            ping_healthcheck(failed=True)
        logger.exception(f"{name} run failed")
        raise
    if heartbeat:
        ping_healthcheck(failed=False)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE OPPORTUNITY PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def process_opportunity(raw_opportunity: dict, force: bool = False) -> dict | None:
    """Claim a retryable workflow lease, then run one opportunity pipeline.

    ``opportunities_cache`` is a successful-content cache, never the source of
    truth for whether processing is complete. A failed fetch/analysis/draft
    releases this lease and is therefore retryable on the next execution.
    """
    raw_opportunity = raw_opportunity or {}
    title = raw_opportunity.get("title") or "Unknown"
    if not isinstance(title, str):
        title = "Unknown"
    source_url = raw_opportunity.get("source_url", "")
    dedup_url = raw_opportunity.get("dedup_url") or source_url
    dedup_url = canonicalize_url(dedup_url) or dedup_url

    claim_token = claim_opportunity_processing(dedup_url, title, force=force)
    if not claim_token:
        logger.info(f"Opportunity already completed or actively claimed: {title[:60]}")
        return None
    # Do not let completed/actively-claimed duplicates consume a run slot.
    # If local capacity was exhausted between discovery and the atomic claim,
    # release this lease so another run can process it normally.
    if not _reserve_processing_slot():
        fail_opportunity_processing(dedup_url, claim_token, "deferred: processing cap reached")
        logger.info(
            f"Processing cap reached ({MAX_OPPORTUNITIES_PER_RUN}); deferring "
            f"'{title[:60]}'"
        )
        return None

    token = _pipeline_outcome.set("retryable")
    try:
        result = _process_opportunity_pipeline(raw_opportunity, force=force)
        outcome = _pipeline_outcome.get()
    except BaseException as exc:
        fail_opportunity_processing(dedup_url, claim_token, f"unhandled pipeline error: {exc}")
        raise
    finally:
        _pipeline_outcome.reset(token)

    if result:
        cache_text = result.pop("_cache_text", "")
        cache_title = result.get("title") or title
        # Durable completion precedes semantic-cache indexing. If the process
        # dies between them, exact dedup is still correct; the reverse order
        # would let a half-finished cache row suppress a retry semantically.
        if not complete_opportunity_processing(dedup_url, claim_token):
            logger.warning("Completion state was not persisted; leaving opportunity retryable")
            return result
        try:
            store_opportunity(dedup_url, cache_title, cache_text)
        except Exception as exc:
            logger.warning(f"Supabase successful-content cache write failed: {exc}")
        return result

    if outcome == "terminal":
        # A confirmed staff vacancy or deterministic NO-BID is intentionally
        # terminal, unlike an infrastructure/analysis failure.
        complete_opportunity_processing(dedup_url, claim_token)
    else:
        fail_opportunity_processing(dedup_url, claim_token, "pipeline did not complete")
    return None


def _process_opportunity_pipeline(raw_opportunity: dict, force: bool = False) -> dict | None:
    """
    Runs the full pipeline for one opportunity.

    Returns a result dict on success.
    Returns None if the opportunity should be skipped — either because
    text extraction failed, Claude confirmed it's a staff vacancy, or
    a critical error occurred.

    Every confirmed consultancy contract runs all the way through to
    a drafted proposal and team email. No score thresholds block this.
    The human reviewer makes the final call on what to submit.
    """
    title      = raw_opportunity.get("title") or "Unknown"
    if not isinstance(title, str):
        title = "Unknown"
    source_url = raw_opportunity.get("source_url", "")
    # Email-newsletter sources fetch and dedup on different URLs — the
    # newsletter rewrites its access tokens daily. See monitors/assortis_email.py.
    dedup_url  = raw_opportunity.get("dedup_url") or source_url
    dedup_url  = canonicalize_url(dedup_url) or dedup_url
    opp_id     = str(uuid.uuid4())
    reset_opportunity_usage(opp_id)

    console.print(f"\n[bold blue]Processing:[/bold blue] {title[:70]}")

    # ── STEP 1: FETCH AND EXTRACT DOCUMENT TEXT ────────────────────────────
    logger.info("  Step 1: Fetching document...")
    # Newsletter sources carry a usable summary blurb inline. It is worth
    # far less than the real listing page, so it is a fallback, not a
    # replacement — and when the fetch works, both are passed to Claude:
    # the blurb's metadata line (donor, country, deadline) is often
    # cleaner than anything on the page itself.
    fallback_text = raw_opportunity.get("fallback_text", "")
    full_text = fetch_and_extract(source_url, opportunity_id=opp_id) if source_url else ""

    if len(full_text) >= MIN_FETCHED_CHARS and fallback_text:
        full_text = (
            f"===== SOURCE FILE: newsletter listing =====\n\n{fallback_text}\n\n"
            f"===== SOURCE FILE: {source_url} =====\n\n{full_text}"
        )
    elif len(full_text) < MIN_FETCHED_CHARS and len(fallback_text) >= MIN_BLURB_CHARS:
        logger.warning(
            f"  Listing page gave only {len(full_text)} chars — falling back to "
            f"the {len(fallback_text)}-char newsletter blurb"
        )
        full_text = fallback_text
    elif len(full_text) < MIN_FETCHED_CHARS:
        logger.warning(
            f"  Insufficient text ({len(full_text)} chars) — skipping "
            f"error_type={ErrorType.DOCUMENT_ERROR}"
        )
        log_stage("fetch", "error", error_type=ErrorType.DOCUMENT_ERROR, chars=len(full_text))
        return None

    min_quality = MIN_BLURB_CHARS if (
        fallback_text and full_text == fallback_text
    ) else MIN_FETCHED_CHARS
    quality = assess_extraction(full_text, source=source_url, min_chars=min_quality)
    if not quality["ok"]:
        logger.error(
            f"  Document quality check failed: {quality['reason']} "
            f"error_type={quality['error_type']}"
        )
        log_stage("fetch", "error", error_type=quality["error_type"], reason=quality["reason"])
        return None

    console.print(
        f"  Extracted [green]{len(full_text):,}[/green] characters"
    )
    log_stage("fetch", "ok", chars=len(full_text), content_sha256=content_hash(full_text)[:12])

    # ── STEP 2: CLAUDE ANALYSIS ────────────────────────────────────────────
    # The raw document is cached only after this opportunity completes. A
    # pre-analysis cache write used to make transient analysis failures look
    # permanently processed to future discovery runs.
    logger.info("  Step 2: Analyzing...")
    analysis = analyze_rfp(full_text, opportunity_id=opp_id, title=title)

    if not analysis:
        logger.error("  Analysis returned empty — skipping")
        log_stage("analyze", "error", error_type=ErrorType.ANALYSIS_ERROR)
        return None

    # Hybrid score: LLM numbers become llm_* audit fields; code calculates
    # FIT / WIN / recommendation. Consultancy boolean is not overwritten.
    analysis = apply_bid_intelligence(analysis)

    opportunity    = analysis.get("opportunity") or {}
    if not isinstance(opportunity, dict):
        opportunity = {}
    bid_analysis   = analysis.get("bid_analysis") or {}
    intelligence   = analysis.get("bid_intelligence") or {}
    fit_score      = bid_analysis.get("cortech_fit_score", 0)
    win_prob       = bid_analysis.get("win_probability", 0)
    recommendation = bid_analysis.get("bid_recommendation") or "WATCH"

    console.print(
        f"  Score: [green]{fit_score}/100[/green] | "
        f"Recommendation: [green]{recommendation}[/green] "
        f"[dim]({intelligence.get('score_version', '')})[/dim]"
    )
    log_stage(
        "score",
        "ok",
        fit=fit_score,
        win=win_prob,
        recommendation=recommendation,
        score_version=intelligence.get("score_version", ""),
    )

    # ── CONSULTANCY CONTRACT GATE ──────────────────────────────────────────
    # Claude has read the full document. If it is definitively a staff
    # vacancy, stop here — do not write to Airtable at all. Staff vacancies
    # are noise, not opportunities. Default is TRUE — fail open, not closed.
    is_consultancy = bid_analysis.get("is_consultancy_contract", True)

    if not is_consultancy and not force:
        rationale = bid_analysis.get(
            "rationale",
            "Staff vacancy — not a firm-level consultancy contract",
        )
        console.print(
            f"  [red]Staff vacancy — stopping pipeline[/red]\n"
            f"  [dim]{rationale[:100]}[/dim]"
        )
        _pipeline_outcome.set("terminal")
        return None
    elif not is_consultancy and force:
        logger.warning(
            f"  Not flagged as a consultancy contract, but force=True — "
            f"proceeding anyway: {title[:60]}"
        )
    else:
        console.print(
            "  [green]Confirmed consultancy contract — running full pipeline[/green]"
        )

    # ── NO-BID GATE — stop before CV matching / budget / proposal ──────────
    # The recommendation saves paid drafting effort, but it is not a final
    # business decision. The CRM item remains New for a human to confirm or
    # override; no automated process marks it as a final no-bid.
    if recommendation == "NO-BID" and not force:
        rationale = bid_analysis.get(
            "rationale",
            "Low fit — not recommended for bid",
        )
        console.print(
            f"  [yellow]NO-BID recommendation — stopping before CV/proposal[/yellow]\n"
            f"  [dim]{rationale[:100]}[/dim]"
        )
        try:
            create_opportunity({
                "title":              opportunity.get("title") or title,
                "client":             opportunity.get("client", ""),
                "source_portal":      raw_opportunity.get("source_portal", "Unknown"),
                "source_url":         source_url,
                "relevance_score":    fit_score,
                "win_probability":    win_prob,
                "bid_recommendation": "NO-BID",
                "key_strengths":      "\n".join(bid_analysis.get("key_strengths") or []),
                "key_gaps":           "\n".join(bid_analysis.get("key_gaps") or []),
                "claude_analysis":    str(analysis)[:50000],
                "status":             "New",
            })
        except Exception:
            pass
        log_stage(
            "opportunity",
            "skipped",
            recommendation="NO-BID",
            decision_state="HUMAN_REVIEW_REQUIRED",
        )
        _pipeline_outcome.set("terminal")
        return None
    elif recommendation == "NO-BID" and force:
        logger.warning(
            f"  NO-BID recommendation, but force=True — proceeding anyway: "
            f"{title[:60]}"
        )

    title = opportunity.get("title") or title or "Unknown"
    if not isinstance(title, str):
        title = "Unknown"

    # ── STEP 4: CREATE AIRTABLE OPPORTUNITY RECORD ─────────────────────────
    airtable_record_id = create_opportunity({
        "title": opportunity.get("title") or title,
        "client": opportunity.get("client", ""),
        "donor": opportunity.get("donor", ""),
        "source_portal": raw_opportunity.get("source_portal", "Unknown"),
        "source_url": source_url,
        "submission_deadline": opportunity.get("submission_deadline", ""),
        "estimated_budget_usd": opportunity.get("estimated_budget_usd", 0),
        "location": opportunity.get("project_location", []),
        "thematic_areas": (
            (analysis.get("requirements") or {}).get("thematic_areas") or []
            if isinstance(analysis.get("requirements") or {}, dict)
            else []
        ),
        "relevance_score": fit_score,
        "win_probability": win_prob,
        "bid_recommendation": recommendation,
        "claude_analysis": str(analysis)[:50000],  # Airtable long-text limit
        "key_strengths": "\n".join(
            bid_analysis.get("key_strengths") or []
        ),
        "key_gaps": "\n".join(
            bid_analysis.get("key_gaps") or []
        ),
        "status": "New",
    })

    if airtable_record_id is None:
        console.print(
            "  [yellow]Could not save to Airtable (rate-limited or down) — "
            "continuing anyway. No CRM record will exist for this run, but "
            "the draft will still be generated and emailed.[/yellow]"
        )

    # ── STEP 5: CV MATCHING ────────────────────────────────────────────────
    logger.info("  Step 3: Matching team from CV database...")
    matched_team_result = {
        "matched_team": {},
        "gaps":         [],
        "coverage_percent": 0,
    }
    team_requirements = analysis.get("team_requirements", [])

    if team_requirements:
        reqs = analysis.get("requirements") or {}
        if not isinstance(reqs, dict):
            reqs = {}
        matched_team_result = match_team_to_requirements(
            team_requirements,
            opportunity_id=opp_id,
            opportunity_title=title,
            opportunity_context={
                "thematic_areas": reqs.get("thematic_areas") or [],
                "language_requirements": reqs.get("language_requirements") or [],
                "geographic_experience": (
                    reqs.get("geographic_experience")
                    or opportunity.get("project_location")
                    or []
                ),
            },
        )
        try:
            if airtable_record_id:
                update_opportunity(airtable_record_id, {
                    "matched_team": str(matched_team_result),
                })
        except Exception as e:
            logger.warning(f"  Airtable matched_team update failed: {e}")

        # Re-score now that team_capacity is known. Weights version is stored
        # with the result so later weight changes do not rewrite this record.
        analysis = apply_bid_intelligence(analysis, matched_team_result)
        bid_analysis = analysis.get("bid_analysis") or {}
        intelligence = analysis.get("bid_intelligence") or {}
        fit_score = bid_analysis.get("cortech_fit_score", fit_score)
        win_prob = bid_analysis.get("win_probability", win_prob)
        recommendation = bid_analysis.get("bid_recommendation") or recommendation
        try:
            if airtable_record_id:
                update_opportunity(airtable_record_id, {
                    "relevance_score": fit_score,
                    "win_probability": win_prob,
                    "bid_recommendation": recommendation,
                    "claude_analysis": str(analysis)[:50000],
                })
        except Exception as e:
            logger.warning(f"  Airtable score refresh failed (non-fatal): {e}")

    # ── STEPS 6–7: BUDGET / DRAFT (branch on submission type) ───
    # `or`, not a .get() default: Claude returns an explicit null here for
    # anything it classified as a staff vacancy, and a null key is present,
    # so the default never fires.
    submission_type = (analysis.get("bid_analysis") or {}).get(
        "submission_type"
    ) or "FULL_PROPOSAL"
    budget = {}

    if submission_type == "EOI":
        logger.info("  Submission type: EOI — full shortlisting draft")
        console.print(
            "  [cyan]EOI submission — writing a complete shortlisting draft (budget skipped)[/cyan]"
        )
        logger.info("  Step 4: Skipping budget (EOI stage)")
        logger.info("  Step 5: Reading the tender documents, then writing the EOI...")
        # full_text, not just `analysis`: the writer reads the tender pack
        # itself before drafting — see intelligence/tender_reader.py.
        proposal_sections = generate_eoi(
            analysis,
            matched_team_result,
            opportunity_id=opp_id,
            tor_text=full_text,
        )
    else:
        logger.info("  Submission type: Full technical proposal")

        logger.info("  Step 4: Calculating budget...")
        project_locations = opportunity.get("project_location") or []
        if isinstance(project_locations, str):
            project_locations = [project_locations]
        elif not isinstance(project_locations, list):
            project_locations = []
        primary_location = project_locations[0] if project_locations else "Nairobi"

        try:
            budget = calculate_budget(
                analysis,
                matched_team_result.get("matched_team", {}),
                primary_location,
            )
        except Exception as e:
            logger.warning(f"  Budget calculation failed (non-fatal): {e}")
            budget = {}

        logger.info("  Step 5: Reading the tender documents, then drafting...")
        # The budget is passed for the internal review email only. No amount
        # from it reaches the drafted proposal — see NO_MONETARY_RULE in
        # intelligence/proposal_writer.py.
        proposal_sections = generate_proposal(
            analysis,
            matched_team_result,
            budget,
            opportunity_id=opp_id,
            tor_text=full_text,
        )

    # ── STEP 9: UPDATE AIRTABLE STATUS ────────────────────────────────────
    matrix = build_compliance_matrix(
        analysis, matched_team_result, proposal_sections
    )
    usage = opportunity_usage()
    try:
        if airtable_record_id:
            update_opportunity(airtable_record_id, {
                "status": "Reviewing",
                "compliance_matrix": json.dumps(matrix),
            })
    except Exception as e:
        logger.warning(f"  Airtable status update failed (non-fatal): {e}")

    if submission_type == "EOI":
        console.print(
            "  [bold green]Expression of Interest draft complete — ready for review[/bold green]"
        )
    elif recommendation == "BID":
        console.print(
            "  [bold green]Proposal draft complete — ready for team review[/bold green]"
        )
    else:
        console.print(
            "  [bold yellow]WATCH quick-flag sent — not a full draft[/bold yellow]"
        )

    est_cost = usage.get("estimated_cost_usd") if usage.get("cost_known") else None
    log_stage(
        "opportunity",
        "ok",
        recommendation=recommendation,
        estimated_cost_usd=est_cost if est_cost is not None else "UNKNOWN",
        tokens_in=usage.get("input", 0),
        tokens_out=usage.get("output", 0),
    )

    return {
        "airtable_id":       airtable_record_id,
        "title":             title,
        "score":             fit_score,
        "recommendation":    recommendation,
        "source_url":        source_url,
        "deadline":          opportunity.get("submission_deadline", "TBD"),
        "client":            opportunity.get("client", ""),
        "budget_cap":        opportunity.get("estimated_budget_usd", 0),
        "analysis":          analysis,
        "bid_intelligence":  intelligence,
        "compliance_matrix": matrix,
        "execution_id":      get_execution_id(),
        "matched_team":      matched_team_result,
        "budget":            budget,
        "proposal_sections": proposal_sections,
        # Exact provider-call records and aggregate measured/unknown usage for
        # this opportunity; never a guessed section token constant.
        "llm_usage": usage,
        "estimated_cost_usd": est_cost,
        # Private handoff to the outer state wrapper. It is removed before
        # email/reporting callers receive the result.
        "_cache_text": full_text,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MANUAL URL SUBMISSION
# ══════════════════════════════════════════════════════════════════════════════

def submit_single_url(url: str) -> None:
    """
    Manual on-demand entry point. Runs one URL through the full pipeline
    immediately. Skips quick_relevance_check() and find_similar_opportunity()
    deliberately — those exist to filter noise out of AUTOMATED discovery;
    a human explicitly choosing this URL has already made that call.
    Exact-URL dedup (check_opportunity_exists) still applies, so
    re-submitting something already processed doesn't waste a second
    full run.
    """
    console.print(Panel(f"Manual submission: {url}", style="bold cyan"))
    new_execution_id()
    _start_execution_budget()

    if check_opportunity_exists(url):
        console.print(
            "[yellow]Note: this URL was already processed before. "
            "Proceeding anyway since you submitted it directly — if the "
            "earlier attempt failed partway through, this run will still "
            "produce a fresh result. Check Airtable/Supabase afterward if "
            "you want to compare against the earlier record.[/yellow]"
        )

    raw_opportunity = {
        "title": "",                    # analyze_rfp() extracts the real title from the document
        "source_url": url,
        "summary": "",
        "source_portal": "Manual submission",
    }

    result = process_opportunity(raw_opportunity, force=True)

    if result is None:
        console.print(
            "[red]Could not process this URL — check the log above: "
            "usually a fetch failure or insufficient extracted text.[/red]"
        )
        return

    if not result.get("analysis", {}).get("bid_analysis", {}).get(
        "is_consultancy_contract", True
    ):
        console.print(
            "[yellow]Note: this didn't read as a consultancy contract "
            "(closer to a staff vacancy or unrelated posting) — a draft "
            "was generated anyway since you submitted it directly. "
            "Use your judgment on whether it's actually worth using.[/yellow]"
        )

    try:
        send_proposal_email(result)
    except Exception as email_err:
        logger.error(f"Proposal email failed: {email_err}")

    # Write immediately to a local file — don't make the person wait on
    # email delivery when they're sitting at the terminal watching this run
    os.makedirs("output", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"output/draft_{timestamp}.md"
    sections = result.get("proposal_sections", {})
    _skip_keys = {"lightweight", "lightweight_reason", "submission_type", "quality_score"}
    with open(out_path, "w") as f:
        written = set()
        for key, heading in SECTION_ORDER:
            content = sections.get(key)
            if key in _skip_keys or not isinstance(content, str) or not content.strip():
                continue
            f.write(f"## {heading}\n\n{content}\n\n")
            written.add(key)
        for section_name, content in sections.items():
            if (
                section_name in written
                or section_name in _skip_keys
                or not isinstance(content, str)
                or not content.strip()
            ):
                continue
            f.write(
                f"## {section_name.replace('_', ' ').title()}\n\n{content}\n\n"
            )

    console.print(Panel(
        f"Draft generated: {out_path}\nAlso sent to the team via the normal proposal email, "
        f"and logged in Airtable/Supabase like any other opportunity.",
        style="bold green",
    ))


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def _run_pipeline() -> None:
    """
    Main pipeline execution — called by scheduler and --once flag.

    Discovers new opportunities from all sources, runs each through
    the full pipeline, sends individual proposal emails per opportunity,
    and sends a summary report at the end of each run.
    """
    eid = new_execution_id()
    _start_execution_budget()
    console.print(Panel.fit(
        f"[bold blue]Cortech BD Agent Running[/bold blue]\n"
        f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  execution_id={eid}[/dim]",
        border_style="blue",
    ))
    log_stage("pipeline", "start", execution_id=eid)

    all_new: list[dict]              = []
    processed_opportunities: list[dict] = []

    # ── SOURCE 1: RSS FEEDS (unused — RSS_FEEDS is empty) ─────────────────
    if RSS_FEEDS:
        logger.info("Checking RSS feeds...")
        try:
            rss_results = monitor_rss_feeds()
            all_new.extend(rss_results)
            logger.info(f"  RSS total: {len(rss_results)} passed filter")
        except Exception as e:
            logger.error(f"RSS monitor failed: {e}")
            try:
                log_agent_action(
                    action_type="Error",
                    description=f"RSS monitor failed: {e}",
                    status="Error",
                    error_message=str(e),
                )
            except Exception:
                pass

    # ── SOURCE 2: SOMALI JOBS SCRAPER ──────────────────────────────────────
    # https://www.somalijobs.com/tenders — Playwright for JS-rendered pages.
    logger.info("Running web scrapers...")
    try:
        scraped_results = scrape_non_rss_sources()
        all_new.extend(scraped_results)
        logger.info(f"  Scrapers total: {len(scraped_results)} passed filter")
    except Exception as e:
        logger.error(f"Non-RSS scrapers failed: {e}")
        try:
            log_agent_action(
                action_type="Error",
                description=f"Scrapers failed: {e}",
                status="Error",
                error_message=str(e),
            )
        except Exception:
            pass

    # ── SOURCE 3: ASSORTIS / ICA DAILY NEWSLETTER (EMAIL) ──────────────────
    # Only processes UNSEEN newsletter emails and marks them seen, so this
    # is safe to run here AND on the dedicated 01:45 schedule — whichever
    # runs first wins, the other finds nothing unread.
    logger.info("Checking Assortis/ICA newsletter...")
    try:
        assortis_results = check_assortis_newsletter()
        all_new.extend(assortis_results)
        logger.info(f"  Assortis total: {len(assortis_results)} passed filter")
    except Exception as e:
        logger.error(f"Assortis newsletter check failed: {e}")
        try:
            log_agent_action(
                action_type="Error",
                description=f"Assortis newsletter check failed: {e}",
                status="Error",
                error_message=str(e),
            )
        except Exception:
            pass

    # ── DEDUP: REMOVE ANYTHING ALREADY IN THIS RUN ─────────────────────────
    seen_urls: set[str] = set()
    unique_new: list[dict] = []
    for opp in all_new:
        url = canonicalize_url(opp.get("dedup_url") or opp.get("source_url", "")) or (
            opp.get("dedup_url") or opp.get("source_url", "")
        )
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_new.append(opp)
    all_new = unique_new

    if all_new and not opportunity_ledger_available():
        logger.error(
            f"Skipping {len(all_new)} bulk discoveries: opportunity_processing "
            "ledger is missing. Apply supabase_migration_opportunity_state.sql. "
            "Use python main.py --submit-url for a one-off."
        )
        console.print(
            "[red]opportunity_processing table missing in Supabase — "
            "refusing to draft bulk discoveries as new. "
            "Apply supabase_migration_opportunity_state.sql. "
            "Manual --submit-url still works.[/red]"
        )
        all_new = []

    total = len(all_new)
    console.print(
        f"\n[bold]Found {total} new opportunit"
        f"{'y' if total == 1 else 'ies'} after filtering[/bold]"
    )

    # ── CAP THE RUN ────────────────────────────────────────────────────────
    # Deferred, not dropped: only processed opportunities are written to
    # Supabase, so the remainder is rediscovered by the next run.
    if total > MAX_OPPORTUNITIES_PER_RUN:
        deferred = total - MAX_OPPORTUNITIES_PER_RUN
        console.print(
            f"[yellow]Processing the first {MAX_OPPORTUNITIES_PER_RUN} this run — "
            f"{deferred} deferred to the next run. Raise "
            f"MAX_OPPORTUNITIES_PER_RUN in .env to widen this.[/yellow]"
        )
        logger.warning(
            f"Run capped at {MAX_OPPORTUNITIES_PER_RUN} of {total} opportunities "
            f"— {deferred} deferred to the next run"
        )
        all_new = all_new[:MAX_OPPORTUNITIES_PER_RUN]
        total = len(all_new)

    # ── SEND STATUS REPORT IF NOTHING FOUND ───────────────────────────────
    if not all_new:
        console.print(
            "[yellow]No new opportunities this run. "
            "Sending status report.[/yellow]"
        )
        try:
            send_report(new_opportunities=[])
        except Exception as e:
            logger.error(f"Status report email failed: {e}")
        return

    # ── PROCESS EACH OPPORTUNITY ───────────────────────────────────────────
    for opp in track(all_new, description="Processing opportunities..."):
        try:
            result = process_opportunity(opp)

            if result:
                processed_opportunities.append(result)

                # Send individual proposal email immediately after each
                # successful pipeline run — one email per opportunity so
                # each review is self-contained and actionable
                try:
                    send_proposal_email(result)
                except Exception as email_err:
                    logger.error(
                        f"Proposal email failed for "
                        f"'{(result.get('title') or 'Unknown')[:50]}': {email_err}"
                    )

        except Exception as e:
            logger.error(
                f"Pipeline error for "
                f"'{(opp.get('title') or 'Unknown')[:60]}': {e}"
            )
            try:
                log_agent_action(
                    action_type="Error",
                    description=(
                        f"Pipeline exception: "
                        f"{(opp.get('title') or 'Unknown')[:50]}"
                    ),
                    status="Error",
                    error_message=str(e),
                )
            except Exception:
                pass

    # ── SEND PIPELINE SUMMARY EMAIL ────────────────────────────────────────
    # Lightweight summary of the full run — the detailed value is in
    # the individual per-proposal emails sent above
    logger.info("Sending pipeline summary report...")
    summary_list = [
        {
            "title":               r.get("title", ""),
            "client":              r.get("client", ""),
            "relevance_score":     r.get("score", 0),
            "bid_recommendation":  r.get("recommendation", "WATCH"),
            "submission_deadline": r.get("deadline", ""),
        }
        for r in processed_opportunities
    ]

    try:
        send_report(new_opportunities=summary_list)
    except Exception as e:
        logger.error(f"Summary report email failed: {e}")

    # ── RUN SUMMARY ────────────────────────────────────────────────────────
    n = len(processed_opportunities)
    console.print(Panel.fit(
        f"[bold green]Pipeline Complete[/bold green]\n"
        f"Discovered:       {total} opportunities\n"
        f"Proposals drafted: {n}\n"
        f"Emails sent:      {n} individual + 1 summary",
        border_style="green",
    ))

    try:
        log_agent_action(
            action_type="Discovery",
            description=(
                f"Run complete: {total} discovered, "
                f"{n} proposals drafted"
            ),
            tokens_used=0,
            status="Success",
        )
    except Exception:
        pass


def run_pipeline() -> None:
    """Public, monitored full-discovery command."""
    return _run_monitored("pipeline", _run_pipeline, heartbeat=True)


def _run_assortis_check() -> None:
    """
    Runs independently of run_pipeline() — the newsletter arrives on
    its own schedule, not the general discovery cycle's.

    Emails each drafted proposal exactly like run_pipeline() does. A
    draft that only lands in Airtable is a draft nobody reads.
    """
    logger.info("Checking Assortis/ICA newsletter...")
    new_execution_id()
    _start_execution_budget()
    opportunities = check_assortis_newsletter()
    if opportunities and not opportunity_ledger_available():
        logger.error(
            f"Skipping {len(opportunities)} Assortis discoveries: "
            "opportunity_processing ledger is missing. "
            "Apply supabase_migration_opportunity_state.sql."
        )
        opportunities = []
    remaining = _remaining_execution_budget()
    if len(opportunities) > remaining:
        logger.warning(
            f"Assortis run capped at {remaining} of {len(opportunities)} "
            "opportunities; remainder will be rediscovered/retried next run"
        )
        opportunities = opportunities[:remaining]
    drafted = 0
    for opp in opportunities:
        try:
            result = process_opportunity(opp)
            if result:
                drafted += 1
                try:
                    send_proposal_email(result)
                except Exception as email_err:
                    logger.error(
                        f"Proposal email failed for "
                        f"'{(result.get('title') or 'Unknown')[:50]}': {email_err}"
                    )
        except Exception as e:
            logger.error(
                f"Assortis pipeline error for "
                f"'{(opp.get('title') or 'Unknown')[:60]}': {e}"
            )
            try:
                log_agent_action(
                    action_type="Error",
                    description=(
                        f"Assortis pipeline exception: "
                        f"{(opp.get('title') or 'Unknown')[:50]}"
                    ),
                    status="Error",
                    error_message=str(e),
                )
            except Exception:
                pass
    logger.info(
        f"Assortis check complete — {len(opportunities)} opportunity(ies) found, "
        f"{drafted} drafted and emailed"
    )


def run_assortis_check() -> None:
    """Public, monitored newsletter-only command with the same hard cap."""
    return _run_monitored("assortis", _run_assortis_check)


def _run_deadline_check() -> None:
    """
    Independent of run_pipeline() — checks OPPORTUNITIES already in
    Airtable for approaching deadlines and sends an escalation digest.
    Active statuses verified from main.py + email_report.py: Reviewing
    (post-proposal), New, and Bidding — excludes Submitted/Won/Lost/No-bid.
    """
    new_execution_id()
    from database.airtable_client import get_table

    table = get_table("opportunities")
    active = table.all(
        formula=(
            "AND("
            "OR({status}='Reviewing', {status}='New', {status}='Bidding'),"
            "{submission_deadline}!=''"
            ")"
        )
    )

    urgent = []
    for record in active:
        fields = record["fields"]
        deadline = fields.get("submission_deadline", "")
        urgency = get_urgency_level(deadline)
        if urgency["level"] in ("CRITICAL", "URGENT", "HIGH"):
            urgent.append({
                "title": (fields.get("title") or "")[:60],
                "client": fields.get("client", ""),
                "deadline": deadline,
                "urgency": urgency,
            })

    if urgent:
        send_deadline_alert_email(urgent)
        logger.info(f"Deadline alert sent for {len(urgent)} opportunities")
    else:
        logger.info("Deadline check: nothing urgent")


def run_deadline_check() -> None:
    return _run_monitored("deadline", _run_deadline_check)


# ══════════════════════════════════════════════════════════════════════════════
# CONTINUOUS SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

def start_scheduler() -> None:
    """
    Runs the pipeline on a fixed interval (CHECK_INTERVAL_HOURS in .env,
    default 6). Fires once immediately on startup so you don't wait up to
    6 hours for the first run after deployment or restart.
    """
    from config import CHECK_INTERVAL_HOURS

    console.print(Panel.fit(
        f"[bold blue]Cortech BD Intelligence Agent[/bold blue]\n"
        f"[dim]Running every {CHECK_INTERVAL_HOURS} hours[/dim]",
        border_style="blue",
    ))

    # Fire immediately on startup
    run_pipeline()

    # Schedule subsequent runs
    schedule.every(CHECK_INTERVAL_HOURS).hours.do(run_pipeline)

    # Also fire at 07:00 EAT daily so the team has a morning report
    schedule.every().day.at("07:00").do(run_pipeline)

    # The ICA Daily Newsletter is sent at 08:32 UTC every day, which is
    # 11:32 on this machine (Africa/Nairobi, UTC+3) — `schedule` runs in
    # local time. Checking at 11:45 catches it the same morning. Missing
    # this window is not fatal: the check re-reads IMAP_LOOKBACK_DAYS of
    # newsletters, so a skipped or failed run self-heals the next day.
    schedule.every().day.at("11:45").do(run_assortis_check)

    schedule.every().day.at("08:00").do(run_deadline_check)
    schedule.every().day.at("08:15").do(
        lambda: _run_monitored("winloss", process_win_loss_outcomes)
    )

    logger.info(
        f"Scheduler active — running every {CHECK_INTERVAL_HOURS} hours, "
        f"daily at 07:00, Assortis at 11:45, deadline check at 08:00, "
        f"win/loss learning at 08:15"
    )

    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute — negligible CPU cost


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # --help must not fall through to start_scheduler(), which fires a full
    # discovery run immediately (emails + Airtable + Claude).
    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            "Cortech BD Intelligence Agent\n"
            "\n"
            "  python main.py --once                  Run the full discovery pipeline once\n"
            "  python main.py --submit-url <url>      Process one URL immediately\n"
            "  python main.py --run-assortis          Check the ICA/Assortis newsletter once\n"
            "  python main.py --run-deadline-check    Send deadline escalation digest\n"
            "  python main.py --run-winloss           Extract win/loss lessons\n"
            "  python main.py                         Continuous scheduler (legacy)\n"
        )
        sys.exit(0)
    require_env()
    configure_logging()
    if "--submit-url" in sys.argv:
        idx = sys.argv.index("--submit-url")
        if idx + 1 >= len(sys.argv):
            print("Usage: python main.py --submit-url <url>")
            sys.exit(1)
        _run_monitored("manual_submission", lambda: submit_single_url(sys.argv[idx + 1]))
    elif "--once" in sys.argv:
        run_pipeline()
    # The systemd timers (see README) invoke these three individually.
    # Without them the flags fell through to start_scheduler(), so
    # `--run-assortis` started an endless polling loop instead of
    # checking the newsletter once and exiting.
    elif "--run-assortis" in sys.argv:
        run_assortis_check()
    elif "--run-deadline-check" in sys.argv:
        run_deadline_check()
    elif "--run-winloss" in sys.argv:
        _run_monitored("winloss", process_win_loss_outcomes)
    else:
        _run_monitored("scheduler", start_scheduler)
