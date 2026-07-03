# main.py
"""
CORTECH BD INTELLIGENCE AGENT — MAIN ORCHESTRATOR
==================================================
Entry points:
  python main.py --once     → single run, for manual testing and cron
  python main.py            → continuous scheduler, runs every CHECK_INTERVAL_HOURS

Pipeline per opportunity (all opportunities that pass the RSS filter):
  1. RSS feed monitoring + three-gate keyword filter
  2. Document download + text extraction
  3. Claude RFP analysis → structured JSON
  4. Airtable opportunity record creation
  5. CV semantic matching (Supabase pgvector)
  6. Budget calculation (rate card × effort estimate)
  7. Compliance matrix generation (Claude)
  8. Full technical proposal draft (Claude, section-by-section)
  9. Individual proposal email to full team (with TOR link + budget)
 10. Pipeline summary email at end of each run
"""

import sys
import uuid
import schedule
import time
from datetime import datetime

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.progress import track

from config import SCORE_THRESHOLDS, CORTECH_PROFILE
from monitors.rss_monitor import monitor_rss_feeds
from processors.downloader import fetch_and_extract
from intelligence.analyzer import analyze_rfp, generate_compliance_matrix
from intelligence.cv_matcher import match_team_to_requirements
from intelligence.budget_calculator import calculate_budget
from intelligence.proposal_writer import generate_proposal
from database.supabase_client import check_opportunity_exists, store_opportunity
from database.airtable_client import (
    create_opportunity,
    update_opportunity,
    log_agent_action,
)
from reporting.email_report import send_report, send_proposal_email

console = Console()


# ══════════════════════════════════════════════════════════════════════════════
# CORE PIPELINE — process one opportunity end-to-end
# ══════════════════════════════════════════════════════════════════════════════

def process_opportunity(raw_opportunity: dict) -> dict | None:
    """
    Full pipeline for a single opportunity.
    Returns a result dict on success, None on failure.
    Every opportunity that reaches this function gets the full treatment —
    no score threshold gates. The score is informational only.
    """
    title      = raw_opportunity.get("title", "Unknown")
    source_url = raw_opportunity.get("source_url", "")

    console.print(f"\n[bold blue]Processing:[/bold blue] {title[:70]}")

    # ── STEP 1: FETCH FULL DOCUMENT ────────────────────────────────────────
    logger.info("  Step 1: Fetching full document...")
    opp_id = str(uuid.uuid4())

    full_text = fetch_and_extract(source_url, opportunity_id=opp_id)

    if not full_text or len(full_text) < 200:
        logger.warning(
            f"  Insufficient text extracted ({len(full_text)} chars). Skipping."
        )
        return None

    console.print(f"  Extracted [green]{len(full_text):,}[/green] characters")

    # ── STEP 2: STORE IN SUPABASE CACHE ───────────────────────────────────
    try:
        store_opportunity(source_url, title, full_text)
    except Exception as e:
        logger.warning(f"  Supabase cache write failed (non-fatal): {e}")

    # ── STEP 3: ANALYZE WITH CLAUDE ───────────────────────────────────────
    logger.info("  Step 2: Analyzing with Claude...")
    analysis = analyze_rfp(full_text, opportunity_id=opp_id, title=title)

    if not analysis:
        logger.error("  Analysis returned empty. Skipping.")
        return None

    opportunity    = analysis.get("opportunity", {})
    bid_analysis   = analysis.get("bid_analysis", {})
    fit_score      = bid_analysis.get("cortech_fit_score", 0)
    win_prob       = bid_analysis.get("win_probability", 0)
    recommendation = bid_analysis.get("bid_recommendation", "WATCH")

    console.print(
        f"  [green]Score: {fit_score}/100 | "
        f"Recommendation: {recommendation}[/green]"
    )

    # ── STEP 4: CREATE AIRTABLE RECORD ────────────────────────────────────
    airtable_record_id = create_opportunity({
        "title":                opportunity.get("title") or title,
        "client":               opportunity.get("client", ""),
        "donor":                opportunity.get("donor", ""),
        "source_portal":        raw_opportunity.get("source_portal", "Unknown"),
        "source_url":           source_url,
        "submission_deadline":  opportunity.get("submission_deadline", ""),
        "estimated_budget_usd": opportunity.get("estimated_budget_usd", 0),
        "location":             opportunity.get("project_location", []),
        "thematic_areas":       analysis.get("requirements", {}).get(
                                    "thematic_areas", []
                                ),
        "relevance_score":      fit_score,
        "win_probability":      win_prob,
        "bid_recommendation":   recommendation,
        "claude_analysis":      str(analysis),
        "key_strengths":        "\n".join(
                                    bid_analysis.get("key_strengths", [])
                                ),
        "key_gaps":             "\n".join(
                                    bid_analysis.get("key_gaps", [])
                                ),
        "status":               "New",
    })

    if airtable_record_id is None:
        console.print(
            "  [red]Skipping — could not save opportunity to Airtable[/red]"
        )
        return None

    # ── STEP 5: MATCH TEAM (semantic CV search) ────────────────────────────
    logger.info("  Step 3: Matching team from CV database...")
    matched_team_result = {
        "matched_team": {},
        "gaps": [],
        "coverage_percent": 0,
    }
    team_requirements = analysis.get("team_requirements", [])

    if team_requirements:
        matched_team_result = match_team_to_requirements(
            team_requirements,
            opportunity_id=opp_id,
            opportunity_title=title,
        )
        try:
            update_opportunity(airtable_record_id, {
                "matched_team": str(matched_team_result),
            })
        except Exception as e:
            logger.warning(f"  Airtable matched_team update failed (non-fatal): {e}")

    # ── STEP 6: CALCULATE BUDGET ───────────────────────────────────────────
    logger.info("  Step 4: Calculating budget...")
    primary_location = (
        opportunity.get("project_location", ["Nairobi"])[0]
        if opportunity.get("project_location")
        else "Nairobi"
    )
    budget = calculate_budget(
        analysis,
        matched_team_result.get("matched_team", {}),
        primary_location,
    )

    # ── STEP 7: COMPLIANCE MATRIX ──────────────────────────────────────────
    logger.info("  Step 5: Generating compliance matrix...")
    compliance_matrix = generate_compliance_matrix(
        analysis,
        list(matched_team_result.get("matched_team", {}).values()),
    )

    # ── STEP 8: WRITE FULL PROPOSAL DRAFT ─────────────────────────────────
    logger.info("  Step 6: Writing proposal draft (section-by-section)...")
    proposal_sections = generate_proposal(
        analysis,
        matched_team_result,
        budget,
        compliance_matrix,
        opportunity_id=opp_id,
    )

    # ── STEP 9: UPDATE AIRTABLE STATUS ────────────────────────────────────
    try:
        update_opportunity(airtable_record_id, {
            "compliance_matrix": compliance_matrix,
            "status":            "Reviewing",
        })
    except Exception as e:
        logger.warning(f"  Airtable status update failed (non-fatal): {e}")

    console.print(
        f"  [bold green]✅ Proposal draft ready — "
        f"flagged for team review[/bold green]"
    )

    return {
        "airtable_id":      airtable_record_id,
        "title":            title,
        "score":            fit_score,
        "recommendation":   recommendation,
        "source_url":       source_url,
        "deadline":         opportunity.get("submission_deadline", "TBD"),
        "client":           opportunity.get("client", ""),
        "budget_cap":       opportunity.get("estimated_budget_usd", 0),
        "analysis":         analysis,
        "matched_team":     matched_team_result,
        "budget":           budget,
        "proposal_sections": proposal_sections,
        "compliance_matrix": compliance_matrix,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE RUNNER — called by scheduler and --once flag
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline() -> None:
    """
    Main pipeline execution.
    Discovers new opportunities, processes each one fully,
    sends individual proposal emails, then sends a summary report.
    """
    console.print(Panel.fit(
        f"[bold blue]🤖 Cortech BD Agent Running[/bold blue]\n"
        f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]",
        border_style="blue",
    ))

    all_new: list[dict] = []
    processed_opportunities: list[dict] = []

    # ── COLLECT FROM RSS FEEDS ─────────────────────────────────────────────
    # ReliefWeb Playwright scraper has been removed — the RSS feed covers
    # ReliefWeb reliably without fragile browser-based DOM selectors.
    logger.info("Collecting from tender sources...")

    try:
        rss_opportunities = monitor_rss_feeds()
        all_new.extend(rss_opportunities)
    except Exception as e:
        logger.error(f"RSS monitor failed: {e}")
        log_agent_action(
            action_type="Error",
            description=f"RSS monitor failed: {e}",
            status="Error",
            error_message=str(e),
        )

    console.print(
        f"\n[bold]Found {len(all_new)} new opportunit"
        f"{'y' if len(all_new) == 1 else 'ies'} after filtering[/bold]"
    )

    # ── PROCESS EACH OPPORTUNITY ───────────────────────────────────────────
    if not all_new:
        console.print(
            "[yellow]No new opportunities. Sending status report.[/yellow]"
        )
        send_report(new_opportunities=[])
        return

    for opp in track(all_new, description="Processing opportunities..."):
        try:
            result = process_opportunity(opp)
            if result:
                processed_opportunities.append(result)
                # Send individual proposal email immediately —
                # one email per opportunity so each review is self-contained
                try:
                    send_proposal_email(result)
                except Exception as email_err:
                    logger.error(
                        f"Proposal email failed for "
                        f"'{result.get('title', 'Unknown')[:50]}': {email_err}"
                    )
        except Exception as e:
            logger.error(
                f"Failed to process "
                f"'{opp.get('title', 'Unknown')[:60]}': {e}"
            )
            log_agent_action(
                action_type="Error",
                description=(
                    f"Pipeline failed for: "
                    f"{opp.get('title', 'Unknown')[:50]}"
                ),
                status="Error",
                error_message=str(e),
            )

    # ── SEND PIPELINE SUMMARY REPORT ───────────────────────────────────────
    # This is a lightweight summary — the real value is the per-proposal
    # emails sent above during processing.
    logger.info("Sending pipeline summary report...")

    summary_opportunities = [
        {
            "title":              r.get("title", ""),
            "client":             r.get("client", ""),
            "relevance_score":    r.get("score", 0),
            "bid_recommendation": r.get("recommendation", "WATCH"),
            "submission_deadline": r.get("deadline", ""),
        }
        for r in processed_opportunities
    ]

    try:
        send_report(new_opportunities=summary_opportunities)
    except Exception as e:
        logger.error(f"Summary report email failed: {e}")

    # ── FINAL SUMMARY ──────────────────────────────────────────────────────
    console.print(Panel.fit(
        f"[bold green]✅ Pipeline Complete[/bold green]\n"
        f"Processed: {len(processed_opportunities)} opportunit"
        f"{'y' if len(processed_opportunities) == 1 else 'ies'}\n"
        f"Proposal emails sent: {len(processed_opportunities)}\n"
        f"Summary report sent.",
        border_style="green",
    ))

    log_agent_action(
        action_type="Discovery",
        description=(
            f"Pipeline run complete: "
            f"{len(processed_opportunities)} opportunities processed, "
            f"{len(processed_opportunities)} proposal drafts emailed"
        ),
        tokens_used=0,
        status="Success",
    )


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULER — continuous mode (python main.py, no flags)
# ══════════════════════════════════════════════════════════════════════════════

def start_scheduler() -> None:
    """
    Runs the pipeline on a fixed interval defined by CHECK_INTERVAL_HOURS
    in .env (default 6). Also fires once immediately on startup so you
    don't have to wait up to 6 hours for the first run after deployment.
    """
    from config import CHECK_INTERVAL_HOURS

    console.print(Panel.fit(
        f"[bold blue]🤖 Cortech BD Intelligence Agent[/bold blue]\n"
        f"[dim]Running every {CHECK_INTERVAL_HOURS} hours[/dim]",
        border_style="blue",
    ))

    # Fire immediately on startup
    run_pipeline()

    # Then schedule subsequent runs
    schedule.every(CHECK_INTERVAL_HOURS).hours.do(run_pipeline)

    while True:
        schedule.run_pending()
        time.sleep(60)  # Poll every minute — negligible CPU cost


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        # Single run — used by crontab and manual testing
        run_pipeline()
    else:
        # Continuous scheduler — used when running on a VPS foreground process
        start_scheduler()