# main.py
"""
CORTECH BD INTELLIGENCE AGENT — MAIN ORCHESTRATOR
==================================================

Entry points:
  python main.py --once        → single run (used by crontab + manual testing)
  python main.py --submit-url  → process one URL on demand (web page, PDF, or
                                 a Google Drive folder with multiple annexes)
  python main.py               → continuous scheduler every CHECK_INTERVAL_HOURS

Pipeline (runs for EVERY opportunity passing the RSS filter):
  1.  RSS feed monitoring + three-gate keyword filter
  2.  Document download and text extraction
  3.  Claude analysis → structured JSON + is_consultancy_contract gate
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
import uuid
import schedule
import time
from datetime import datetime

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.progress import track

from config import CORTECH_PROFILE
from monitors.rss_monitor import monitor_rss_feeds
from monitors.scraper import scrape_non_rss_sources
from monitors.assortis_email import check_assortis_newsletter
from processors.downloader import fetch_and_extract
from intelligence.analyzer import analyze_rfp, generate_compliance_matrix
from intelligence.cv_matcher import match_team_to_requirements, filter_by_availability
from intelligence.budget_calculator import calculate_budget
from intelligence.proposal_writer import generate_proposal, generate_eoi
from intelligence.learning import process_win_loss_outcomes
from database.supabase_client import (
    check_opportunity_exists,
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

console = Console()


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE OPPORTUNITY PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def process_opportunity(raw_opportunity: dict, force: bool = False) -> dict | None:
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
    title      = raw_opportunity.get("title", "Unknown")
    source_url = raw_opportunity.get("source_url", "")
    opp_id     = str(uuid.uuid4())

    console.print(f"\n[bold blue]Processing:[/bold blue] {title[:70]}")

    # ── STEP 1: FETCH AND EXTRACT DOCUMENT TEXT ────────────────────────────
    logger.info("  Step 1: Fetching document...")
    if raw_opportunity.get("skip_fetch"):
        # Email-based sources (e.g. Assortis/ICA newsletter) carry the full
        # blurb text inline — there is no URL to download.
        full_text = raw_opportunity.get("raw_text", "")
        if not full_text or len(full_text) < 40:  # matches MIN_BLURB_LENGTH
            logger.warning(
                f"  Insufficient text ({len(full_text)} chars) — skipping"
            )
            return None
    else:
        full_text = fetch_and_extract(source_url, opportunity_id=opp_id)
        if not full_text or len(full_text) < 200:
            logger.warning(
                f"  Insufficient text ({len(full_text)} chars) — skipping"
            )
            return None

    console.print(
        f"  Extracted [green]{len(full_text):,}[/green] characters"
    )

    # ── STEP 2: CACHE IN SUPABASE ──────────────────────────────────────────
    try:
        store_opportunity(source_url, title, full_text)
    except Exception as e:
        logger.warning(f"  Supabase cache write failed (non-fatal): {e}")

    # ── STEP 3: CLAUDE ANALYSIS ────────────────────────────────────────────
    logger.info("  Step 2: Analyzing with Claude...")
    analysis = analyze_rfp(full_text, opportunity_id=opp_id, title=title)

    if not analysis:
        logger.error("  Analysis returned empty — skipping")
        return None

    opportunity    = analysis.get("opportunity", {})
    bid_analysis   = analysis.get("bid_analysis", {})
    fit_score      = bid_analysis.get("cortech_fit_score", 0)
    win_prob       = bid_analysis.get("win_probability", 0)
    recommendation = bid_analysis.get("bid_recommendation", "WATCH")

    console.print(
        f"  Score: [green]{fit_score}/100[/green] | "
        f"Recommendation: [green]{recommendation}[/green]"
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
                "key_strengths":      "\n".join(bid_analysis.get("key_strengths", [])),
                "key_gaps":           "\n".join(bid_analysis.get("key_gaps", [])),
                "claude_analysis":    str(analysis)[:50000],
                "status":             "No-bid",
            })
        except Exception:
            pass
        return None
    elif recommendation == "NO-BID" and force:
        logger.warning(
            f"  NO-BID recommendation, but force=True — proceeding anyway: "
            f"{title[:60]}"
        )

    title = opportunity.get("title") or title

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
            analysis.get("requirements", {}).get("thematic_areas", [])
        ),
        "relevance_score": fit_score,
        "win_probability": win_prob,
        "bid_recommendation": recommendation,
        "claude_analysis": str(analysis)[:50000],  # Airtable long-text limit
        "key_strengths": "\n".join(
            bid_analysis.get("key_strengths", [])
        ),
        "key_gaps": "\n".join(
            bid_analysis.get("key_gaps", [])
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
        matched_team_result = match_team_to_requirements(
            team_requirements,
            opportunity_id=opp_id,
            opportunity_title=title,
        )
        team = matched_team_result.get("matched_team", {})
        if team:
            for role, match in team.items():
                match["_sort_role"] = role
            reordered = filter_by_availability(list(team.values()))
            matched_team_result["matched_team"] = {
                m.pop("_sort_role"): m for m in reordered
            }
        try:
            if airtable_record_id:
                update_opportunity(airtable_record_id, {
                    "matched_team": str(matched_team_result),
                })
        except Exception as e:
            logger.warning(f"  Airtable matched_team update failed: {e}")

    # ── STEPS 6–8: BUDGET / COMPLIANCE / DRAFT (branch on submission type) ───
    submission_type = analysis.get("bid_analysis", {}).get(
        "submission_type", "FULL_PROPOSAL"
    )
    budget = {}
    compliance_matrix = ""

    if submission_type == "EOI":
        logger.info("  Submission type: EOI — lightweight path")
        console.print(
            "  [cyan]EOI submission — skipping budget & compliance[/cyan]"
        )
        logger.info("  Step 4: Skipping budget (EOI stage)")
        logger.info("  Step 5: Skipping compliance matrix (EOI stage)")
        logger.info("  Step 6: Writing Expression of Interest...")
        proposal_sections = generate_eoi(
            analysis,
            matched_team_result,
            opportunity_id=opp_id,
        )
    else:
        logger.info("  Submission type: Full technical proposal")

        logger.info("  Step 4: Calculating budget...")
        project_locations = opportunity.get("project_location", [])
        primary_location  = project_locations[0] if project_locations else "Nairobi"

        try:
            budget = calculate_budget(
                analysis,
                matched_team_result.get("matched_team", {}),
                primary_location,
            )
        except Exception as e:
            logger.warning(f"  Budget calculation failed (non-fatal): {e}")
            budget = {}

        if recommendation == "BID":
            logger.info("  Step 5: Generating compliance matrix...")
            compliance_matrix = generate_compliance_matrix(
                analysis,
                list(matched_team_result.get("matched_team", {}).values()),
            )
        else:
            logger.info("  Step 5: Skipping compliance matrix (WATCH quick-flag)")

        logger.info("  Step 6: Writing proposal draft...")
        proposal_sections = generate_proposal(
            analysis,
            matched_team_result,
            budget,
            compliance_matrix,
            opportunity_id=opp_id,
        )

    # ── STEP 9: UPDATE AIRTABLE STATUS ────────────────────────────────────
    try:
        if airtable_record_id:
            update_opportunity(airtable_record_id, {
                "compliance_matrix": compliance_matrix,
                "status":            "Reviewing",
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
        "matched_team":      matched_team_result,
        "budget":            budget,
        "proposal_sections": proposal_sections,
        "compliance_matrix": compliance_matrix,
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
        for section_name, content in sections.items():
            if section_name in _skip_keys or not isinstance(content, str):
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

def run_pipeline() -> None:
    """
    Main pipeline execution — called by scheduler and --once flag.

    Discovers new opportunities from all sources, runs each through
    the full pipeline, sends individual proposal emails per opportunity,
    and sends a summary report at the end of each run.
    """
    console.print(Panel.fit(
        f"[bold blue]Cortech BD Agent Running[/bold blue]\n"
        f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]",
        border_style="blue",
    ))

    all_new: list[dict]              = []
    processed_opportunities: list[dict] = []

    # ── SOURCE 1: RSS FEEDS ────────────────────────────────────────────────
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

    # ── SOURCE 2: NON-RSS SCRAPERS ─────────────────────────────────────────
    # Somalia Jobs, DRC, Save the Children, CARE, GIZ, USAID, etc.
    # Uses Playwright browser to handle JavaScript-rendered pages.
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
        url = opp.get("source_url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_new.append(opp)
    all_new = unique_new

    total = len(all_new)
    console.print(
        f"\n[bold]Found {total} new opportunit"
        f"{'y' if total == 1 else 'ies'} after filtering[/bold]"
    )

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
                        f"'{result.get('title', 'Unknown')[:50]}': {email_err}"
                    )

        except Exception as e:
            logger.error(
                f"Pipeline error for "
                f"'{opp.get('title', 'Unknown')[:60]}': {e}"
            )
            try:
                log_agent_action(
                    action_type="Error",
                    description=(
                        f"Pipeline exception: "
                        f"{opp.get('title', 'Unknown')[:50]}"
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


def run_assortis_check() -> None:
    """
    Runs independently of run_pipeline() — the newsletter arrives on
    its own schedule, not the general discovery cycle's.
    """
    logger.info("Checking Assortis/ICA newsletter...")
    opportunities = check_assortis_newsletter()
    for opp in opportunities:
        try:
            process_opportunity(opp)
        except Exception as e:
            logger.error(
                f"Assortis pipeline error for "
                f"'{opp.get('title', 'Unknown')[:60]}': {e}"
            )
            try:
                log_agent_action(
                    action_type="Error",
                    description=(
                        f"Assortis pipeline exception: "
                        f"{opp.get('title', 'Unknown')[:50]}"
                    ),
                    status="Error",
                    error_message=str(e),
                )
            except Exception:
                pass
    logger.info(
        f"Assortis check complete — {len(opportunities)} opportunity(ies) processed"
    )


def run_deadline_check() -> None:
    """
    Independent of run_pipeline() — checks OPPORTUNITIES already in
    Airtable for approaching deadlines and sends an escalation digest.
    Active statuses verified from main.py + email_report.py: Reviewing
    (post-proposal), New, and Bidding — excludes Submitted/Won/Lost/No-bid.
    """
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

    # Assortis/ICA newsletter arrives ~01:32 — check a few minutes after.
    # NOTE: `schedule` runs in the server’s local timezone; confirm the
    # Railway/VPS timezone matches the observed arrival time before locking
    # this in. Adjust "01:45" as needed.
    schedule.every().day.at("01:45").do(run_assortis_check)

    schedule.every().day.at("08:00").do(run_deadline_check)
    schedule.every().day.at("08:15").do(process_win_loss_outcomes)

    logger.info(
        f"Scheduler active — running every {CHECK_INTERVAL_HOURS} hours, "
        f"daily at 07:00, Assortis at 01:45, deadline check at 08:00, "
        f"win/loss learning at 08:15"
    )

    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute — negligible CPU cost


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--submit-url" in sys.argv:
        idx = sys.argv.index("--submit-url")
        if idx + 1 >= len(sys.argv):
            print("Usage: python main.py --submit-url <url>")
            sys.exit(1)
        submit_single_url(sys.argv[idx + 1])
    elif "--once" in sys.argv:
        run_pipeline()
    else:
        start_scheduler()