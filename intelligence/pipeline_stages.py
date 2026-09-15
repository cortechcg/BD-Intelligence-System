"""Tested opportunity pipeline stages (Phase 7 / ADR 011).

Lease ownership stays in ``opportunity_processing.state``. Progress uses
``pipeline_stage``: discovered → extracted → scored → drafted → reviewed →
outcome. ``reviewed`` / ``outcome`` are human/Airtable transitions, not
autonomous submit.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger
from rich.console import Console

from intelligence.bid_scorer import apply_bid_intelligence
from intelligence.budget_calculator import calculate_budget
from intelligence.compliance import build_compliance_matrix
from intelligence.cv_matcher import match_team_to_requirements
from intelligence.learning import save_draft_memory
from intelligence.organizations import (
    build_client_intelligence,
    empty_client_intelligence,
    known_client_factor_from_analysis,
)
from intelligence.proposal_writer import DraftingError, assert_usable_client_draft, draft_bid_or_watch_proposal
from processors.document_quality import assess_extraction
from processors.downloader import fetch_and_extract
from intelligence.analyzer import analyze_rfp
from utils.errors import ErrorType
from utils.hashing import content_hash
from utils.observability import (
    get_execution_id,
    log_stage,
    opportunity_usage,
    reset_opportunity_usage,
)
from utils.urls import canonicalize_url

console = Console()

# Matches the historical constants in main.py (fetch vs newsletter blurb).
MIN_FETCHED_CHARS = 200
MIN_BLURB_CHARS = 40

PIPELINE_STAGES = (
    "discovered",
    "extracted",
    "scored",
    "drafted",
    "reviewed",
    "outcome",
)
HUMAN_STAGES = frozenset({"reviewed", "outcome"})
MAX_DRAFT_FAILURES = 3


class TerminalSkip(Exception):
    """Not a failure: vacancy, NO-BID, or content-hash identity hit."""

    def __init__(self, reason: str = ""):
        super().__init__(reason)
        self.reason = reason


class RetryableStageError(Exception):
    """Stage failed; the lease should be released as ``failed``."""

    def __init__(self, reason: str, *, error_type: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.error_type = error_type


@dataclass
class ProcessingSnapshot:
    pipeline_stage: str = "discovered"
    checkpoint: dict = field(default_factory=dict)
    draft_fail_count: int = 0
    resume_available: bool = False


@dataclass
class PipelineOutcome:
    result: dict | None = None
    disposition: str = "retryable"  # success | terminal | retryable | dead_letter
    error: str = ""
    increment_draft_fail: bool = False


@dataclass
class StageDeps:
    fetch_and_extract: Callable = fetch_and_extract
    analyze_rfp: Callable = analyze_rfp
    apply_bid_intelligence: Callable = apply_bid_intelligence
    find_opportunity_by_content_hash: Callable | None = None
    create_opportunity: Callable | None = None
    update_opportunity: Callable | None = None
    match_team_to_requirements: Callable = match_team_to_requirements
    calculate_budget: Callable = calculate_budget
    draft_bid_or_watch_proposal: Callable = draft_bid_or_watch_proposal
    build_compliance_matrix: Callable = build_compliance_matrix
    save_draft_memory: Callable = save_draft_memory
    build_client_intelligence: Callable = build_client_intelligence
    persist_stage: Callable | None = None
    log_stage: Callable = log_stage


@dataclass
class OpportunityContext:
    raw: dict
    force: bool = False
    title: str = "Unknown"
    source_url: str = ""
    dedup_url: str = ""
    opp_id: str = ""
    full_text: str = ""
    analysis: dict = field(default_factory=dict)
    opportunity: dict = field(default_factory=dict)
    bid_analysis: dict = field(default_factory=dict)
    intelligence: dict = field(default_factory=dict)
    fit_score: Any = 0
    win_prob: Any = 0
    recommendation: str = "WATCH"
    airtable_record_id: str | None = None
    client_intelligence: dict = field(default_factory=dict)
    matched_team_result: dict = field(default_factory=lambda: {
        "matched_team": {},
        "gaps": [],
        "coverage_percent": 0,
    })
    budget: dict = field(default_factory=dict)
    proposal_sections: dict = field(default_factory=dict)
    matrix: list = field(default_factory=list)
    submission_type: str = "FULL_PROPOSAL"

    def to_checkpoint(self) -> dict:
        return {
            "full_text": self.full_text,
            "analysis": self.analysis,
            "title": self.title,
            "opp_id": self.opp_id,
            "airtable_record_id": self.airtable_record_id,
            "recommendation": self.recommendation,
            "fit_score": self.fit_score,
            "win_prob": self.win_prob,
            "matched_team_result": self.matched_team_result,
            "budget": self.budget,
            "proposal_sections": self.proposal_sections,
            "client_intelligence": self.client_intelligence,
            "intelligence": self.intelligence,
            "matrix": self.matrix,
            "submission_type": self.submission_type,
            "source_url": self.source_url,
            "dedup_url": self.dedup_url,
        }

    def apply_checkpoint(self, checkpoint: dict | None) -> None:
        data = checkpoint if isinstance(checkpoint, dict) else {}
        if data.get("full_text"):
            self.full_text = str(data["full_text"])
        if isinstance(data.get("analysis"), dict):
            self.analysis = data["analysis"]
            self._refresh_from_analysis()
        if data.get("title"):
            self.title = str(data["title"])
        if data.get("opp_id"):
            self.opp_id = str(data["opp_id"])
        if data.get("airtable_record_id"):
            self.airtable_record_id = data["airtable_record_id"]
        if data.get("recommendation"):
            self.recommendation = str(data["recommendation"])
        if "fit_score" in data:
            self.fit_score = data["fit_score"]
        if "win_prob" in data:
            self.win_prob = data["win_prob"]
        if isinstance(data.get("matched_team_result"), dict):
            self.matched_team_result = data["matched_team_result"]
        if isinstance(data.get("budget"), dict):
            self.budget = data["budget"]
        if isinstance(data.get("proposal_sections"), dict):
            self.proposal_sections = data["proposal_sections"]
        if isinstance(data.get("client_intelligence"), dict):
            self.client_intelligence = data["client_intelligence"]
        if isinstance(data.get("intelligence"), dict):
            self.intelligence = data["intelligence"]
        if isinstance(data.get("matrix"), list):
            self.matrix = data["matrix"]
        if data.get("submission_type"):
            self.submission_type = str(data["submission_type"])

    def _refresh_from_analysis(self) -> None:
        analysis = self.analysis if isinstance(self.analysis, dict) else {}
        opportunity = analysis.get("opportunity") or {}
        if not isinstance(opportunity, dict):
            opportunity = {}
        bid_analysis = analysis.get("bid_analysis") or {}
        if not isinstance(bid_analysis, dict):
            bid_analysis = {}
        intelligence = analysis.get("bid_intelligence") or {}
        if not isinstance(intelligence, dict):
            intelligence = {}
        self.opportunity = opportunity
        self.bid_analysis = bid_analysis
        self.intelligence = intelligence
        self.fit_score = bid_analysis.get("cortech_fit_score", self.fit_score)
        self.win_prob = bid_analysis.get("win_probability", self.win_prob)
        self.recommendation = bid_analysis.get("bid_recommendation") or self.recommendation
        self.submission_type = bid_analysis.get("submission_type") or self.submission_type or "FULL_PROPOSAL"

    def to_result(self) -> dict:
        usage = opportunity_usage()
        est_cost = usage.get("estimated_cost_usd") if usage.get("cost_known") else None
        return {
            "airtable_id": self.airtable_record_id,
            "title": self.title,
            "score": self.fit_score,
            "recommendation": self.recommendation,
            "source_url": self.source_url,
            "deadline": self.opportunity.get("submission_deadline", "TBD"),
            "client": self.opportunity.get("client", ""),
            "budget_cap": self.opportunity.get("estimated_budget_usd", 0),
            "analysis": self.analysis,
            "bid_intelligence": self.intelligence,
            "compliance_matrix": self.matrix,
            "execution_id": get_execution_id(),
            "matched_team": self.matched_team_result,
            "budget": self.budget,
            "proposal_sections": self.proposal_sections,
            "client_intelligence": self.client_intelligence,
            "llm_usage": usage,
            "estimated_cost_usd": est_cost,
            "_cache_text": self.full_text,
        }


def context_from_raw(raw_opportunity: dict, *, force: bool = False) -> OpportunityContext:
    raw_opportunity = raw_opportunity or {}
    title = raw_opportunity.get("title") or "Unknown"
    if not isinstance(title, str):
        title = "Unknown"
    source_url = raw_opportunity.get("source_url", "")
    dedup_url = raw_opportunity.get("dedup_url") or source_url
    dedup_url = canonicalize_url(dedup_url) or dedup_url
    return OpportunityContext(
        raw=raw_opportunity,
        force=force,
        title=title,
        source_url=source_url,
        dedup_url=dedup_url,
        opp_id=str(uuid.uuid4()),
    )


def _safe_client_intelligence(ctx: OpportunityContext, deps: StageDeps) -> dict:
    try:
        opportunity = ctx.opportunity if isinstance(ctx.opportunity, dict) else {}
        return deps.build_client_intelligence(
            client=opportunity.get("client") or "",
            donor=opportunity.get("donor") or "",
            title=str(opportunity.get("title") or ctx.title or ""),
            opportunity_id=ctx.opp_id,
            known_client_factor=known_client_factor_from_analysis(ctx.analysis),
        )
    except Exception as e:
        logger.warning(f"  Client intelligence failed (fail-open): {e}")
        return empty_client_intelligence()


def _persist(deps: StageDeps, source_url: str, claim_token: str, stage: str, ctx: OpportunityContext) -> None:
    persist = deps.persist_stage
    if not persist or not claim_token:
        return
    persist(source_url, claim_token, stage, ctx.to_checkpoint())


def run_extract_stage(ctx: OpportunityContext, deps: StageDeps) -> OpportunityContext:
    """Fetch + quality gate + content-hash identity. Resume starts here from discovered."""
    logger.info("  Step 1: Fetching document...")
    fallback_text = ctx.raw.get("fallback_text", "")
    full_text = ""
    if ctx.source_url:
        full_text = deps.fetch_and_extract(ctx.source_url, opportunity_id=ctx.opp_id) or ""

    if len(full_text) >= MIN_FETCHED_CHARS and fallback_text:
        full_text = (
            f"===== SOURCE FILE: newsletter listing =====\n\n{fallback_text}\n\n"
            f"===== SOURCE FILE: {ctx.source_url} =====\n\n{full_text}"
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
        deps.log_stage("fetch", "error", error_type=ErrorType.DOCUMENT_ERROR, chars=len(full_text))
        raise RetryableStageError(
            f"insufficient extracted text ({len(full_text)} chars)",
            error_type=ErrorType.DOCUMENT_ERROR,
        )

    min_quality = MIN_BLURB_CHARS if (
        fallback_text and full_text == fallback_text
    ) else MIN_FETCHED_CHARS
    quality = assess_extraction(full_text, source=ctx.source_url, min_chars=min_quality)
    if not quality["ok"]:
        logger.error(
            f"  Document quality check failed: {quality['reason']} "
            f"error_type={quality['error_type']}"
        )
        deps.log_stage("fetch", "error", error_type=quality["error_type"], reason=quality["reason"])
        raise RetryableStageError(
            f"document quality: {quality['reason']}",
            error_type=str(quality.get("error_type") or ErrorType.DOCUMENT_ERROR),
        )

    console.print(f"  Extracted [green]{len(full_text):,}[/green] characters")
    deps.log_stage("fetch", "ok", chars=len(full_text), content_sha256=content_hash(full_text)[:12])
    ctx.full_text = full_text

    digest = content_hash(full_text)
    finder = deps.find_opportunity_by_content_hash
    if not ctx.force and digest and finder:
        existing_body = finder(digest)
        existing_url = ""
        if isinstance(existing_body, dict):
            existing_url = existing_body.get("source_url") or ""
            existing_url = canonicalize_url(existing_url) or existing_url
        if existing_url and existing_url != ctx.dedup_url:
            logger.info(
                f"  Same document body already cached under {existing_url[:80]} "
                f"(content_hash {digest[:12]}) — skipping re-analysis"
            )
            deps.log_stage("analyze", "skip", reason="content_hash_duplicate")
            raise TerminalSkip("content_hash_duplicate")
    return ctx


def run_score_stage(ctx: OpportunityContext, deps: StageDeps) -> OpportunityContext:
    """Analyze + deterministic score + consultancy/NO-BID gates.

    BID/WATCH also creates the Airtable New record so a resume from scored
    does not duplicate CRM rows.
    """
    logger.info("  Step 2: Analyzing...")
    analysis = deps.analyze_rfp(ctx.full_text, opportunity_id=ctx.opp_id, title=ctx.title)
    if not analysis:
        logger.error("  Analysis returned empty — skipping")
        deps.log_stage("analyze", "error", error_type=ErrorType.ANALYSIS_ERROR)
        raise RetryableStageError("analysis returned empty", error_type=ErrorType.ANALYSIS_ERROR)

    analysis = deps.apply_bid_intelligence(analysis)
    ctx.analysis = analysis
    ctx._refresh_from_analysis()

    console.print(
        f"  Score: [green]{ctx.fit_score}/100[/green] | "
        f"Recommendation: [green]{ctx.recommendation}[/green] "
        f"[dim]({ctx.intelligence.get('score_version', '')})[/dim]"
    )
    deps.log_stage(
        "score",
        "ok",
        fit=ctx.fit_score,
        win=ctx.win_prob,
        recommendation=ctx.recommendation,
        score_version=ctx.intelligence.get("score_version", ""),
    )

    # Default True when missing — fail open, never miss a real consultancy.
    is_consultancy = ctx.bid_analysis.get("is_consultancy_contract", True)
    if not is_consultancy and not ctx.force:
        rationale = ctx.bid_analysis.get(
            "rationale",
            "Staff vacancy — not a firm-level consultancy contract",
        )
        console.print(
            f"  [red]Staff vacancy — stopping pipeline[/red]\n"
            f"  [dim]{str(rationale)[:100]}[/dim]"
        )
        raise TerminalSkip("not_consultancy")
    elif not is_consultancy and ctx.force:
        logger.warning(
            f"  Not flagged as a consultancy contract, but force=True — "
            f"proceeding anyway: {ctx.title[:60]}"
        )
    else:
        console.print(
            "  [green]Confirmed consultancy contract — running full pipeline[/green]"
        )

    ctx.client_intelligence = _safe_client_intelligence(ctx, deps)

    if ctx.recommendation == "NO-BID" and not ctx.force:
        rationale = ctx.bid_analysis.get(
            "rationale",
            "Low fit — not recommended for bid",
        )
        console.print(
            f"  [yellow]NO-BID recommendation — stopping before CV/proposal[/yellow]\n"
            f"  [dim]{str(rationale)[:100]}[/dim]"
        )
        if deps.create_opportunity:
            try:
                deps.create_opportunity({
                    "title": ctx.opportunity.get("title") or ctx.title,
                    "client": ctx.opportunity.get("client", ""),
                    "source_portal": ctx.raw.get("source_portal", "Unknown"),
                    "source_url": ctx.source_url,
                    "relevance_score": ctx.fit_score,
                    "win_probability": ctx.win_prob,
                    "bid_recommendation": "NO-BID",
                    "key_strengths": "\n".join(ctx.bid_analysis.get("key_strengths") or []),
                    "key_gaps": "\n".join(ctx.bid_analysis.get("key_gaps") or []),
                    "claude_analysis": str(ctx.analysis)[:50000],
                    "status": "New",
                })
            except Exception:
                pass
        deps.log_stage(
            "opportunity",
            "skipped",
            recommendation="NO-BID",
            decision_state="HUMAN_REVIEW_REQUIRED",
        )
        raise TerminalSkip("no_bid")
    elif ctx.recommendation == "NO-BID" and ctx.force:
        logger.warning(
            f"  NO-BID recommendation, but force=True — proceeding anyway: "
            f"{ctx.title[:60]}"
        )

    title = ctx.opportunity.get("title") or ctx.title or "Unknown"
    if not isinstance(title, str):
        title = "Unknown"
    ctx.title = title

    if deps.create_opportunity:
        ctx.airtable_record_id = deps.create_opportunity({
            "title": ctx.opportunity.get("title") or ctx.title,
            "client": ctx.opportunity.get("client", ""),
            "donor": ctx.opportunity.get("donor", ""),
            "source_portal": ctx.raw.get("source_portal", "Unknown"),
            "source_url": ctx.source_url,
            "submission_deadline": ctx.opportunity.get("submission_deadline", ""),
            "estimated_budget_usd": ctx.opportunity.get("estimated_budget_usd", 0),
            "location": ctx.opportunity.get("project_location", []),
            "thematic_areas": (
                (ctx.analysis.get("requirements") or {}).get("thematic_areas") or []
                if isinstance(ctx.analysis.get("requirements") or {}, dict)
                else []
            ),
            "relevance_score": ctx.fit_score,
            "win_probability": ctx.win_prob,
            "bid_recommendation": ctx.recommendation,
            "claude_analysis": str(ctx.analysis)[:50000],
            "key_strengths": "\n".join(ctx.bid_analysis.get("key_strengths") or []),
            "key_gaps": "\n".join(ctx.bid_analysis.get("key_gaps") or []),
            "status": "New",
        })
        if ctx.airtable_record_id is None:
            console.print(
                "  [yellow]Could not save to Airtable (rate-limited or down) — "
                "continuing anyway. No CRM record will exist for this run, but "
                "the draft will still be generated and emailed.[/yellow]"
            )
    return ctx


def run_draft_stage(ctx: OpportunityContext, deps: StageDeps) -> OpportunityContext:
    """CV match, budget (fail-open), and BID/WATCH drafting. Empty drafts fail."""
    logger.info("  Step 3: Matching team from CV database...")
    team_requirements = ctx.analysis.get("team_requirements", [])
    if team_requirements:
        reqs = ctx.analysis.get("requirements") or {}
        if not isinstance(reqs, dict):
            reqs = {}
        ctx.matched_team_result = deps.match_team_to_requirements(
            team_requirements,
            opportunity_id=ctx.opp_id,
            opportunity_title=ctx.title,
            opportunity_context={
                "thematic_areas": reqs.get("thematic_areas") or [],
                "language_requirements": reqs.get("language_requirements") or [],
                "geographic_experience": (
                    reqs.get("geographic_experience")
                    or ctx.opportunity.get("project_location")
                    or []
                ),
            },
        )
        try:
            if ctx.airtable_record_id and deps.update_opportunity:
                deps.update_opportunity(ctx.airtable_record_id, {
                    "matched_team": str(ctx.matched_team_result),
                })
        except Exception as e:
            logger.warning(f"  Airtable matched_team update failed: {e}")

        ctx.analysis = deps.apply_bid_intelligence(ctx.analysis, ctx.matched_team_result)
        ctx._refresh_from_analysis()
        try:
            if ctx.airtable_record_id and deps.update_opportunity:
                deps.update_opportunity(ctx.airtable_record_id, {
                    "relevance_score": ctx.fit_score,
                    "win_probability": ctx.win_prob,
                    "bid_recommendation": ctx.recommendation,
                    "claude_analysis": str(ctx.analysis)[:50000],
                })
        except Exception as e:
            logger.warning(f"  Airtable score refresh failed (non-fatal): {e}")

    ctx.submission_type = (ctx.analysis.get("bid_analysis") or {}).get(
        "submission_type"
    ) or "FULL_PROPOSAL"

    logger.info("  Step 4: Preparing internal budget for team review...")
    project_locations = ctx.opportunity.get("project_location") or []
    if isinstance(project_locations, str):
        project_locations = [project_locations]
    elif not isinstance(project_locations, list):
        project_locations = []
    primary_location = project_locations[0] if project_locations else "Nairobi"
    try:
        ctx.budget = deps.calculate_budget(
            ctx.analysis,
            ctx.matched_team_result.get("matched_team", {}),
            primary_location,
        )
    except Exception as e:
        logger.warning(f"  Budget calculation failed (non-fatal): {e}")
        ctx.budget = {}

    if ctx.submission_type == "EOI":
        logger.info("  Submission type: EOI — full shortlisting draft")
        console.print(
            "  [cyan]EOI submission — writing a complete shortlisting draft "
            "(financial figures stay in the review email only)[/cyan]"
        )
        logger.info("  Step 5: Reading the tender documents, then writing the EOI...")
    else:
        logger.info("  Submission type: Full technical proposal")
        logger.info("  Step 5: Reading the tender documents, then drafting...")

    try:
        ctx.proposal_sections = deps.draft_bid_or_watch_proposal(
            ctx.analysis,
            ctx.matched_team_result,
            ctx.budget,
            opportunity_id=ctx.opp_id,
            tor_text=ctx.full_text,
            submission_type=ctx.submission_type,
        )
        ctx.proposal_sections = assert_usable_client_draft(ctx.proposal_sections)
    except DraftingError:
        raise
    except Exception as e:
        raise DraftingError(f"proposal writer failed: {e}") from e

    ctx.matrix = deps.build_compliance_matrix(
        ctx.analysis, ctx.matched_team_result, ctx.proposal_sections
    )
    usage = opportunity_usage()
    try:
        if ctx.airtable_record_id and deps.update_opportunity:
            deps.update_opportunity(ctx.airtable_record_id, {
                "status": "Reviewing",
                "compliance_matrix": json.dumps(ctx.matrix),
            })
    except Exception as e:
        logger.warning(f"  Airtable status update failed (non-fatal): {e}")

    if ctx.submission_type == "EOI":
        console.print(
            "  [bold green]Expression of Interest draft complete — ready for review[/bold green]"
        )
    elif (ctx.proposal_sections or {}).get("lightweight"):
        console.print(
            "  [bold yellow]WATCH quick-flag sent — not a full draft[/bold yellow]"
        )
    elif ctx.recommendation == "BID":
        console.print(
            "  [bold green]Proposal draft complete — ready for team review[/bold green]"
        )
    else:
        console.print(
            "  [bold green]Full WATCH draft complete — ready for team review[/bold green]"
        )

    est_cost = usage.get("estimated_cost_usd") if usage.get("cost_known") else None
    deps.log_stage(
        "opportunity",
        "ok",
        recommendation=ctx.recommendation,
        estimated_cost_usd=est_cost if est_cost is not None else "UNKNOWN",
        tokens_in=usage.get("input", 0),
        tokens_out=usage.get("output", 0),
    )

    try:
        deps.save_draft_memory(
            opportunity_id=ctx.airtable_record_id or "",
            title=ctx.title,
            client=ctx.opportunity.get("client") or "",
            donor=ctx.opportunity.get("donor") or "",
            sections=ctx.proposal_sections,
        )
    except Exception as e:
        logger.warning(f"  Draft memory save failed (non-fatal): {e}")
    return ctx


def run_opportunity_pipeline(
    raw_opportunity: dict,
    *,
    force: bool = False,
    snapshot: ProcessingSnapshot | None = None,
    claim_token: str = "",
    deps: StageDeps | None = None,
) -> PipelineOutcome:
    """Run or resume agent stages. Human stages are not executed here."""
    deps = deps or StageDeps()
    snapshot = snapshot or ProcessingSnapshot()
    ctx = context_from_raw(raw_opportunity, force=force)
    reset_opportunity_usage(ctx.opp_id)
    console.print(f"\n[bold blue]Processing:[/bold blue] {ctx.title[:70]}")

    stage = "discovered"
    if not force and snapshot.resume_available:
        stage = snapshot.pipeline_stage or "discovered"
        ctx.apply_checkpoint(snapshot.checkpoint)
        if snapshot.checkpoint.get("opp_id"):
            reset_opportunity_usage(ctx.opp_id)

    if stage in HUMAN_STAGES:
        logger.info(
            f"  Ledger already at human stage {stage} — not reprocessing"
        )
        return PipelineOutcome(result=None, disposition="terminal", error=f"human_stage:{stage}")

    if stage == "drafted":
        if ctx.proposal_sections and ctx.full_text:
            logger.info("  Resuming: reconstructing result from drafted checkpoint")
            return PipelineOutcome(result=ctx.to_result(), disposition="success")
        logger.warning("  drafted checkpoint unusable — retrying draft from scored")
        stage = "scored"

    try:
        if stage == "discovered":
            ctx = run_extract_stage(ctx, deps)
            _persist(deps, ctx.dedup_url, claim_token, "extracted", ctx)
            stage = "extracted"

        if stage == "extracted":
            if not ctx.full_text:
                ctx = run_extract_stage(ctx, deps)
                _persist(deps, ctx.dedup_url, claim_token, "extracted", ctx)
            ctx = run_score_stage(ctx, deps)
            _persist(deps, ctx.dedup_url, claim_token, "scored", ctx)
            stage = "scored"

        if stage == "scored":
            if not ctx.analysis:
                ctx = run_score_stage(ctx, deps)
                _persist(deps, ctx.dedup_url, claim_token, "scored", ctx)
            ctx = run_draft_stage(ctx, deps)
            _persist(deps, ctx.dedup_url, claim_token, "drafted", ctx)
            return PipelineOutcome(result=ctx.to_result(), disposition="success")
    except TerminalSkip as skip:
        return PipelineOutcome(result=None, disposition="terminal", error=str(skip.reason or skip))
    except DraftingError as exc:
        count = snapshot.draft_fail_count + 1
        persist = deps.persist_stage
        if persist and claim_token:
            ckpt = ctx.to_checkpoint()
            ckpt["draft_fail_count"] = count
            persist(ctx.dedup_url, claim_token, "scored", ckpt)
        disposition = "dead_letter" if count >= MAX_DRAFT_FAILURES else "retryable"
        return PipelineOutcome(
            result=None,
            disposition=disposition,
            error=f"drafting failed: {exc}",
            increment_draft_fail=True,
        )
    except RetryableStageError as exc:
        return PipelineOutcome(result=None, disposition="retryable", error=exc.reason)

    return PipelineOutcome(result=None, disposition="retryable", error="pipeline did not complete")
