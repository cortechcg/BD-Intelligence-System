# main.py
"""
CORTECH BD INTELLIGENCE AGENT — MAIN ORCHESTRATOR
==================================================

Entry points:
  python main.py --once        → single run (used by crontab + manual testing)
  python main.py --submit-url  → process one URL on demand (web page, PDF, or
                                 a Google Drive folder with multiple annexes)
                                 also accepts: --submit -url <url>
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
import schedule
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.progress import track

from config import CHECK_INTERVAL_HOURS, MAX_OPPORTUNITIES_PER_RUN, RSS_FEEDS, require_env
from monitors.rss_monitor import monitor_rss_feeds
from monitors.scraper import scrape_non_rss_sources
from monitors.assortis_email import check_assortis_newsletter
from processors.downloader import fetch_and_extract
from intelligence.analyzer import analyze_rfp
from intelligence.bid_scorer import apply_bid_intelligence
from intelligence.compliance import build_compliance_matrix
from intelligence.cv_matcher import match_team_to_requirements
from intelligence.budget_calculator import calculate_budget
from intelligence.proposal_writer import (
    DraftingError,
    assert_usable_client_draft,
    generate_proposal,
    generate_eoi,
)
from intelligence.pipeline_stages import (
    MAX_DRAFT_FAILURES,
    MIN_BLURB_CHARS,
    MIN_FETCHED_CHARS,
    ProcessingSnapshot,
    StageDeps,
    run_opportunity_pipeline,
)
from utils.errors import SpendCapError
from intelligence.organizations import (
    build_client_intelligence,
    empty_client_intelligence,
    known_client_factor_from_analysis,
)
from reporting.docx_builder import iter_client_sections
from intelligence.learning import process_win_loss_outcomes, save_draft_memory
from database.supabase_client import (
    check_opportunity_exists,
    claim_opportunity_processing,
    complete_opportunity_processing,
    dead_letter_opportunity_processing,
    fail_opportunity_processing,
    find_opportunity_by_content_hash,
    load_processing_snapshot,
    opportunity_ledger_available,
    persist_opportunity_stage,
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
    send_market_digest_email,
    get_urgency_level,
)
from utils.observability import (
    configure_logging,
    log_stage,
    new_execution_id,
    start_run_spend_cap,
)
from utils.urls import canonicalize_url
from utils.healthcheck import ping_healthcheck

console = Console()

# A fetched ToR/listing page below this is a fetch failure, not a short
# document — matches MIN_USEFUL_CHARS in processors/downloader.py.
# Re-exported from pipeline_stages so existing imports keep working.


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
_pipeline_error: ContextVar[str] = ContextVar(
    "pipeline_error", default=""
)
_pipeline_increment_draft: ContextVar[bool] = ContextVar(
    "pipeline_increment_draft", default=False
)


def _start_execution_budget() -> _ExecutionBudget:
    """Start a per-command hard cap at the actual processing boundary."""
    start_run_spend_cap()
    budget = _ExecutionBudget(limit=max(0, int(MAX_OPPORTUNITIES_PER_RUN)))
    _execution_budget.set(budget)
    return budget


def _remaining_execution_budget() -> int:
    budget = _execution_budget.get()
    if budget is None:
        return max(0, int(MAX_OPPORTUNITIES_PER_RUN))
    return max(0, budget.limit - budget.used)


def _safe_client_intelligence(
    analysis: dict | None,
    *,
    opportunity_id: str = "",
    title: str = "",
) -> dict:
    """Match client/donor and roll up observed history. Never raises."""
    try:
        opportunity = {}
        if isinstance(analysis, dict) and isinstance(analysis.get("opportunity"), dict):
            opportunity = analysis["opportunity"]
        return build_client_intelligence(
            client=opportunity.get("client") or "",
            donor=opportunity.get("donor") or "",
            title=title,
            opportunity_id=opportunity_id,
            known_client_factor=known_client_factor_from_analysis(analysis),
        )
    except Exception as e:
        logger.warning(f"  Client intelligence failed (fail-open): {e}")
        return empty_client_intelligence()


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

def process_opportunity(
    raw_opportunity: dict, force: bool = False, *, retry_dead_letter: bool = False,
) -> dict | None:
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

    claim_token = claim_opportunity_processing(
        dedup_url, title, force=force, retry_dead_letter=retry_dead_letter,
    )
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
    err_token = _pipeline_error.set("")
    draft_token = _pipeline_increment_draft.set(False)
    try:
        result = _process_opportunity_pipeline(
            raw_opportunity, force=force, claim_token=claim_token,
        )
        outcome = _pipeline_outcome.get()
        stage_error = _pipeline_error.get()
    except BaseException as exc:
        fail_opportunity_processing(dedup_url, claim_token, f"unhandled pipeline error: {exc}")
        raise
    finally:
        _pipeline_outcome.reset(token)
        _pipeline_error.reset(err_token)
        _pipeline_increment_draft.reset(draft_token)

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
            analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
            opportunity = (
                analysis.get("opportunity")
                if isinstance(analysis.get("opportunity"), dict)
                else {}
            )
            requirements = (
                analysis.get("requirements")
                if isinstance(analysis.get("requirements"), dict)
                else {}
            )
            store_opportunity(
                dedup_url,
                cache_title,
                cache_text,
                facts={
                    "thematic_areas": requirements.get("thematic_areas") or [],
                    "locations": opportunity.get("project_location") or [],
                    "donor": opportunity.get("donor") or "",
                    "discovered_at": datetime.now().strftime("%Y-%m-%d"),
                },
            )
        except Exception as exc:
            logger.warning(f"Supabase successful-content cache write failed: {exc}")
        return result

    if outcome == "terminal":
        # A confirmed staff vacancy or deterministic NO-BID is intentionally
        # terminal, unlike an infrastructure/analysis failure.
        complete_opportunity_processing(dedup_url, claim_token)
    elif outcome == "dead_letter":
        logger.error(
            f"  Drafting dead-lettered after {MAX_DRAFT_FAILURES} failures: "
            f"{title[:60]} — {stage_error}"
        )
        dead_letter_opportunity_processing(
            dedup_url, claim_token, stage_error or "drafting dead-lettered",
        )
    else:
        fail_opportunity_processing(
            dedup_url,
            claim_token,
            stage_error or "pipeline did not complete",
        )
    return None


def _draft_via_main_hooks(
    analysis,
    matched_team_result,
    budget=None,
    opportunity_id=None,
    tor_text="",
    submission_type="FULL_PROPOSAL",
):
    """Draft through main.generate_proposal / generate_eoi so tests can patch them."""
    try:
        if (submission_type or "FULL_PROPOSAL") == "EOI":
            sections = generate_eoi(
                analysis,
                matched_team_result,
                opportunity_id=opportunity_id,
                tor_text=tor_text,
            )
        else:
            sections = generate_proposal(
                analysis,
                matched_team_result,
                budget or {},
                opportunity_id=opportunity_id,
                tor_text=tor_text,
            )
    except SpendCapError:
        raise
    except DraftingError:
        raise
    except Exception as e:
        raise DraftingError(f"proposal writer failed: {e}") from e
    return assert_usable_client_draft(sections)


def _process_opportunity_pipeline(
    raw_opportunity: dict,
    force: bool = False,
    snapshot: ProcessingSnapshot | None = None,
    claim_token: str = "",
) -> dict | None:
    """Stage runner. Resume from the last persisted pipeline_stage."""
    raw_opportunity = raw_opportunity or {}
    source_url = raw_opportunity.get("source_url", "")
    dedup_url = raw_opportunity.get("dedup_url") or source_url
    dedup_url = canonicalize_url(dedup_url) or dedup_url

    if snapshot is None:
        loaded = load_processing_snapshot(dedup_url) or {}
        if force:
            snapshot = ProcessingSnapshot()
        else:
            checkpoint = loaded.get("checkpoint") or {}
            draft_fails = int(loaded.get("draft_fail_count") or 0)
            if not draft_fails:
                draft_fails = int(checkpoint.get("draft_fail_count") or 0)
            snapshot = ProcessingSnapshot(
                pipeline_stage=loaded.get("pipeline_stage") or "discovered",
                checkpoint=checkpoint if isinstance(checkpoint, dict) else {},
                draft_fail_count=draft_fails,
                resume_available=bool(loaded.get("resume_available")),
            )

    deps = StageDeps(
        fetch_and_extract=fetch_and_extract,
        analyze_rfp=analyze_rfp,
        apply_bid_intelligence=apply_bid_intelligence,
        find_opportunity_by_content_hash=find_opportunity_by_content_hash,
        create_opportunity=create_opportunity,
        update_opportunity=update_opportunity,
        match_team_to_requirements=match_team_to_requirements,
        calculate_budget=calculate_budget,
        draft_bid_or_watch_proposal=_draft_via_main_hooks,
        build_compliance_matrix=build_compliance_matrix,
        save_draft_memory=save_draft_memory,
        build_client_intelligence=build_client_intelligence,
        persist_stage=persist_opportunity_stage,
        log_stage=log_stage,
    )
    outcome = run_opportunity_pipeline(
        raw_opportunity,
        force=force,
        snapshot=snapshot,
        claim_token=claim_token,
        deps=deps,
    )
    if outcome.disposition == "success":
        _pipeline_outcome.set("retryable")
    else:
        _pipeline_outcome.set(outcome.disposition)
    _pipeline_error.set(outcome.error or "")
    _pipeline_increment_draft.set(bool(outcome.increment_draft_fail))
    return outcome.result


def _refresh_outcome_learning() -> None:
    """Pull Won/Lost lessons before drafting so this run can use them."""
    try:
        process_win_loss_outcomes()
    except Exception as e:
        logger.warning(f"Win/loss learning refresh failed (non-fatal): {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MANUAL URL SUBMISSION
# ══════════════════════════════════════════════════════════════════════════════

def submit_single_url(
    url: str, *, force: bool = True, retry_dead_letter: bool = False,
) -> dict | None:
    """
    Manual on-demand entry point. Runs one URL through the full pipeline
    immediately. Skips quick_relevance_check() and find_similar_opportunity()
    deliberately — those exist to filter noise out of AUTOMATED discovery;
    a human explicitly choosing this URL has already made that call.
    Exact-URL dedup (check_opportunity_exists) still applies, so
    re-submitting something already processed doesn't waste a second
    full run.

    ``force=True`` (the CLI default) resets the ledger to ``discovered`` and
    reprocesses from scratch. The dashboard passes ``force=False`` when a human
    clicks "draft this" on an already-discovered opportunity, so the Phase 7
    checkpoint is honoured and analysis is not paid for twice; it uses the
    default for a pasted URL, which is byte-for-byte the CLI path.

    ``retry_dead_letter=True`` (dashboard Retry) claims a ``dead_letter`` row
    without resetting it, so drafting resumes from the persisted checkpoint
    with a fresh ``MAX_DRAFT_FAILURES`` budget. It has no effect on rows in
    any other state.

    Returns the pipeline result dict (with ``_draft_path`` added), or ``None``
    if the run did not complete. The CLI ignores the return value; the
    dashboard worker uses it. This is a new *caller*, not a second code path —
    see ``tests/test_dashboard_parity.py``.
    """
    console.print(Panel(f"Manual submission: {url}", style="bold cyan"))
    new_execution_id()
    _start_execution_budget()
    _refresh_outcome_learning()

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

    result = process_opportunity(
        raw_opportunity, force=force, retry_dead_letter=retry_dead_letter,
    )

    if result is None:
        console.print(
            "[red]Could not process this URL — check the log above: "
            "usually a fetch failure or insufficient extracted text.[/red]"
        )
        return None

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
    _skip_keys = {
        "lightweight", "lightweight_reason", "submission_type", "quality_score",
        "claim_grounding", "tender_brief", "win_strategy", "document_lock",
        "section_order", "omitted_financial", "submission_outline",
    }
    with open(out_path, "w") as f:
        written = set()
        for key, heading, content in iter_client_sections(sections):
            if key in _skip_keys:
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

    # Private key, same convention as the popped `_cache_text`. The local file
    # is ephemeral on a container host, so the dashboard serves the draft from
    # the durable Supabase checkpoint rather than from this path.
    result["_draft_path"] = out_path
    return result


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
    _refresh_outcome_learning()

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
    _refresh_outcome_learning()
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


def _run_market_digest() -> None:
    """Observed-data trend digest from stored opportunities. No LLM. No live send in tests."""
    new_execution_id()
    from database.market_store import load_observed_opportunities, load_org_index_for_digest
    from intelligence.market_trends import build_market_digest

    records, truncated = load_observed_opportunities()
    org_index, orgs_available, _org_count = load_org_index_for_digest()
    cited_awards = []
    try:
        from database.intelligence_facts import load_award_observations

        cited_awards = load_award_observations(limit=50)
    except Exception:
        cited_awards = []
    digest = build_market_digest(
        records,
        as_of=datetime.now().date(),
        org_index=org_index,
        orgs_available=orgs_available,
        truncated=truncated,
        cited_awards=cited_awards,
    )
    send_market_digest_email(digest)
    try:
        log_agent_action(
            action_type="Report",
            description=(
                f"Observed-data market digest: store_rows={digest.store_row_count} "
                f"dated={digest.dated_row_count} trend={digest.overall_is_trend()}"
            ),
            status="Success",
        )
    except Exception:
        pass
    logger.info(
        f"Market digest complete — store_rows={digest.store_row_count}, "
        f"dated={digest.dated_row_count}, sources={digest.sources}"
    )


def run_market_digest() -> None:
    return _run_monitored("market_digest", _run_market_digest)


def _run_extract_relationships() -> None:
    """Scan data/proposals (and stored proposal chunks) for cited JV/consortium edges."""
    from pathlib import Path

    from intelligence.relationships import (
        extract_from_proposal_embeddings,
        extract_from_proposals_dir,
    )

    new_execution_id()
    edges = extract_from_proposals_dir(Path("data/proposals"), persist=True)
    extra = []
    try:
        from database.intelligence_facts import load_proposal_embedding_rows

        extra = extract_from_proposal_embeddings(
            load_proposal_embedding_rows(), persist=True
        )
    except Exception:
        extra = []
    logger.info(
        f"Relationship extract complete — {len(edges)} file edge(s), "
        f"{len(extra)} embedding chunk edge(s)"
    )
    try:
        log_agent_action(
            action_type="Extract",
            description=(
                f"Cited relationship edges: files={len(edges)} "
                f"embeddings={len(extra)}"
            ),
            status="Success",
        )
    except Exception:
        pass


def run_extract_relationships() -> None:
    return _run_monitored("relationships", _run_extract_relationships)


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
    # Weekly observed-data market digest (trailing 30/90 days). Daily send
    # would repeat the same windows; Monday follows the morning digest cluster.
    schedule.every().monday.at("08:30").do(run_market_digest)

    logger.info(
        f"Scheduler active — running every {CHECK_INTERVAL_HOURS} hours, "
        f"daily at 07:00, Assortis at 11:45, deadline check at 08:00, "
        f"win/loss learning at 08:15, market digest Mondays at 08:30"
    )

    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute — negligible CPU cost


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

_CLI_USAGE = (
    "Cortech BD Intelligence Agent\n"
    "\n"
    "  python main.py --once                  Run the full discovery pipeline once\n"
    "  python main.py --submit-url <url>      Process one URL immediately\n"
    "  python main.py --submit -url <url>     Same (common mistype of --submit-url)\n"
    "  python main.py --run-assortis          Check the ICA/Assortis newsletter once\n"
    "  python main.py --run-deadline-check    Send deadline escalation digest\n"
    "  python main.py --run-winloss           Extract win/loss lessons\n"
    "  python main.py --run-market-digest     Send observed-data market digest\n"
    "  python main.py --extract-relationships Extract cited JV/consortium edges from data/proposals/\n"
    "  python main.py                         Continuous scheduler (legacy)\n"
)

_SINGLE_FLAGS = {
    "--once": "once",
    "--run-assortis": "assortis",
    "--run-deadline-check": "deadline",
    "--run-winloss": "winloss",
    "--run-market-digest": "market",
    "--extract-relationships": "relationships",
}


class CliError(ValueError):
    """Invalid argv. Must not fall through to the continuous scheduler."""


#: Set to "1" to run the in-process scheduler in a headless context anyway.
#: There is no legitimate deployment that needs this today: production
#: scheduling is `main.py --once` from systemd/cron (README), and the
#: dashboard worker consumes `dashboard_triggers` only.
SCHEDULER_OVERRIDE_ENV = "CORTECH_ALLOW_SCHEDULER"

#: Environment variables a hosting platform injects into a long-lived service.
#: Any one of them present means "this is a web/worker process on a PaaS",
#: where the always-on scheduler must never start.
_PAAS_MARKERS = ("PORT", "RENDER", "RENDER_SERVICE_ID", "RAILWAY_ENVIRONMENT", "DYNO")


def refuse_headless_scheduler(env: dict | None = None, *, stdin_is_tty: bool | None = None) -> str | None:
    """Return a refusal message if bare `main.py` (scheduler mode) is running
    somewhere it must not, else None.

    Bare `main.py` starts the always-on scheduler, which fires a FULL discovery
    run immediately and every CHECK_INTERVAL_HOURS. Run inside a hosted web
    service that is an unattended, indefinitely repeating pipeline — exactly
    the 2026-09-21 Render incident and the earlier Railway one. The scheduler
    is only ever legitimate in an interactive terminal on the local machine,
    and even there systemd `--once` timers superseded it.

    Pure function of its inputs so it is unit-testable; `__main__` wires the
    real environment and TTY in.
    """
    env = os.environ if env is None else env
    if str(env.get(SCHEDULER_OVERRIDE_ENV, "")).strip() == "1":
        return None
    if stdin_is_tty is None:
        try:
            stdin_is_tty = sys.stdin.isatty()
        except (AttributeError, ValueError):
            stdin_is_tty = False
    markers = [m for m in _PAAS_MARKERS if env.get(m)]
    if not markers and stdin_is_tty:
        return None
    where = (
        f"hosting-platform environment detected ({', '.join(markers)})"
        if markers else "no interactive terminal (stdin is not a TTY)"
    )
    return (
        "REFUSING to start the in-process scheduler: " + where + ".\n"
        "Bare `python main.py` runs a full discovery pass NOW and then every "
        f"{CHECK_INTERVAL_HOURS} hours, unattended — LLM spend, review emails, Airtable "
        "writes. That is the Railway/Render incident, not a deployment.\n"
        "Use one of:\n"
        "  dashboard web    : uvicorn dashboard.app:app --host 0.0.0.0 --port $PORT\n"
        "  dashboard worker : python -m dashboard.worker\n"
        "  one discovery run: python main.py --once   (from systemd/cron)\n"
        f"If you truly want the loop here, set {SCHEDULER_OVERRIDE_ENV}=1."
    )


def parse_main_argv(argv: list[str]) -> dict:
    """Parse CLI tokens. Unknown or incomplete submit flags never mean scheduler."""
    args = list(argv[1:])
    if not args:
        return {"mode": "scheduler"}
    if "-h" in args or "--help" in args:
        return {"mode": "help"}

    url = None
    wanted_submit = False
    mode_flags: list[str] = []
    leftovers: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in ("-h", "--help"):
            return {"mode": "help"}
        if token in ("--submit-url", "--url", "-url"):
            if i + 1 >= len(args):
                raise CliError(
                    "Usage: python main.py --submit-url <url>\n"
                    "   or: python main.py --submit -url <url>"
                )
            nxt = args[i + 1]
            if nxt.startswith("-") and not nxt.lower().startswith("http"):
                raise CliError(
                    "Usage: python main.py --submit-url <url>\n"
                    "   or: python main.py --submit -url <url>"
                )
            url = nxt
            wanted_submit = True
            i += 2
            continue
        if token.startswith("--submit-url="):
            url = token.split("=", 1)[1]
            wanted_submit = True
            i += 1
            continue
        if token == "--submit":
            wanted_submit = True
            i += 1
            continue
        if token in _SINGLE_FLAGS:
            mode_flags.append(token)
            i += 1
            continue
        if token.startswith("-"):
            raise CliError(f"Unknown option: {token}\n\n{_CLI_USAGE}")
        leftovers.append(token)
        i += 1

    if wanted_submit:
        if not url and leftovers:
            url = leftovers[0]
            leftovers = leftovers[1:]
        if not url:
            raise CliError(
                "Usage: python main.py --submit-url <url>\n"
                "   or: python main.py --submit -url <url>"
            )
        return {"mode": "submit", "url": url}

    if len(mode_flags) > 1:
        raise CliError(
            "Use only one of: "
            + ", ".join(_SINGLE_FLAGS)
            + f"\n\n{_CLI_USAGE}"
        )
    if mode_flags:
        return {"mode": _SINGLE_FLAGS[mode_flags[0]]}
    if leftovers:
        raise CliError(f"Unknown arguments: {' '.join(leftovers)}\n\n{_CLI_USAGE}")
    return {"mode": "scheduler"}


if __name__ == "__main__":
    # --help / incomplete --submit must not fall through to start_scheduler(),
    # which fires a full discovery run immediately (emails + Airtable + Claude).
    try:
        parsed = parse_main_argv(sys.argv)
    except CliError as exc:
        print(exc)
        sys.exit(1)
    if parsed["mode"] == "help":
        print(_CLI_USAGE)
        sys.exit(0)
    require_env()
    configure_logging()
    mode = parsed["mode"]
    if mode == "submit":
        _run_monitored(
            "manual_submission",
            lambda submitted=parsed["url"]: submit_single_url(submitted),
        )
    elif mode == "once":
        run_pipeline()
    # The systemd timers (see README) invoke these individually.
    # Without them the flags fell through to start_scheduler(), so
    # `--run-assortis` started an endless polling loop instead of
    # checking the newsletter once and exiting.
    elif mode == "assortis":
        run_assortis_check()
    elif mode == "deadline":
        run_deadline_check()
    elif mode == "winloss":
        _run_monitored("winloss", process_win_loss_outcomes)
    elif mode == "market":
        run_market_digest()
    elif mode == "relationships":
        run_extract_relationships()
    else:
        refusal = refuse_headless_scheduler()
        if refusal:
            print(refusal, file=sys.stderr)
            logger.critical(refusal.splitlines()[0])
            sys.exit(2)
        _run_monitored("scheduler", start_scheduler)
