#!/usr/bin/env python3
"""Local UI harness: the real app and templates, fed by in-memory fixtures.

It never reads or writes Supabase. Every data function the pages call is
replaced with a fixture, a dummy session secret signs the cookie, and the
process refuses to start if it is pointed at anything but the loopback address.
This is a design and screenshot aid, not shipped UI.

    python dashboard/design/ui_harness.py serve 8765
    python dashboard/design/ui_harness.py shoot output/ui-v3/after

``shoot`` starts the server itself, logs in with a locally minted cookie and
writes one PNG per page and width (1440, 1024, 390).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ["DASHBOARD_SESSION_SECRET"] = "ui-harness-secret-not-real"
os.environ["DASHBOARD_COOKIE_SECURE"] = "false"

NOW = datetime.now(timezone.utc)


def _ts(days: float = 0, hours: float = 0) -> str:
    return (NOW - timedelta(days=days, hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ── fixtures ────────────────────────────────────────────────────────────────

U = "https://tenders.example.org/"
URL_SCORED = U + "unicef-wash-assessment-turkana"
URL_DRAFTED = U + "giz-climate-resilience-evaluation"
URL_FLIGHT = U + "world-bank-urban-water-review"
URL_SPEND = U + "undp-youth-employment-baseline"
URL_DEAD = U + "fao-livestock-value-chain-study"
URL_SKIP = U + "ifad-rural-finance-audit"
URL_CACHE_1 = U + "eu-education-sector-support"
URL_CACHE_2 = U + "afdb-energy-access-mission"


def _row(url, title, client, donor, stage, state, rec=None, fit=None, win=None, deadline=None,
         last_error=None, fails=0, sub="FULL_PROPOSAL"):
    return {
        "source_url": url, "title": title, "state": state, "pipeline_stage": stage,
        "draft_fail_count": fails, "last_error": last_error, "attempt_count": 1 + fails,
        "discovered_at": _ts(days=9), "updated_at": _ts(hours=2), "completed_at": None,
        "cp_title": title, "cp_client": client, "cp_donor": donor, "cp_deadline": deadline,
        "cp_recommendation": rec, "cp_fit": fit, "cp_win": win, "cp_submission_type": sub,
        "cp_airtable": None,
    }


LEDGER = [
    _row(URL_SCORED, "Water, sanitation and hygiene assessment, Turkana County", "UNICEF Kenya",
         "UNICEF", "scored", "pending", "BID", 82, 61, "2026-11-20"),
    _row(URL_DRAFTED, "Mid-term evaluation of the climate resilience programme", "GIZ",
         "BMZ", "drafted", "completed", "BID", 76, 54, "2026-11-12"),
    _row(URL_FLIGHT, "Urban water utilities performance review", "World Bank",
         "World Bank", "extracted", "processing", None, None, None, None),
    _row(URL_SPEND, "Youth employment baseline survey", "UNDP", "UNDP", "scored", "failed",
         "WATCH", 58, 33, "2026-12-02", last_error="per-run LLM spend cap reached"),
    _row(URL_DEAD, "Livestock value chain study", "FAO", "FAO", "scored", "dead_letter",
         "BID", 71, 48, "2026-11-30", last_error="draft came back empty after 3 attempts", fails=3),
    _row(URL_SKIP, "Rural finance audit services", "IFAD", "IFAD", "scored", "completed",
         "NO-BID", 31, 9, "2026-11-05"),
]
CACHE = [
    {"source_url": URL_CACHE_1, "title": "Education sector support programme, technical assistance",
     "donor": "European Union", "discovered_at": _ts(days=1), "processed_at": None,
     "airtable_opportunity_id": None},
    {"source_url": URL_CACHE_2, "title": "Unknown", "donor": None, "discovered_at": _ts(days=2),
     "processed_at": None, "airtable_opportunity_id": None},
]

ANALYSIS = {
    "opportunity": {
        "title": LEDGER[0]["title"], "client": "UNICEF Kenya", "donor": "UNICEF",
        "submission_deadline": "2026-11-20", "reference_number": "LRPS-2026-9140311",
        "estimated_budget_usd": 185000, "duration_months": 8,
        "purpose": "Assess household water access, sanitation coverage and hygiene practice across six sub-counties to guide the next programme cycle.",
        "project_location": ["Turkana County, Kenya"],
    },
    "requirements": {
        "thematic_areas": ["WASH", "Public health", "Monitoring and evaluation"],
        "language_requirements": ["English", "Swahili"],
    },
    "evaluation_criteria": [
        {"criterion": "Technical approach and methodology", "weight_percent": 40,
         "description": "Clarity, feasibility and fit with the terms of reference."},
        {"criterion": "Team qualifications", "weight_percent": 35},
        {"criterion": "Past performance", "weight_percent": 25},
    ],
    "deliverables": [
        {"name": "Inception report", "description": "Within three weeks of contract signature."},
        {"name": "Household survey dataset", "description": "Cleaned and documented."},
        {"name": "Final assessment report", "description": ""},
    ],
    "team_requirements": [{"role": "Team leader", "min_experience_years": 12, "qualifications": "Advanced degree in public health or a related field"}],
    "bid_analysis": {"key_strengths": ["Ten completed WASH assessments in arid counties",
                                       "Swahili-speaking field team on the roster"],
                     "win_themes": "Local delivery, evidence of prior baselines, a lean budget."},
    "submission_requirements": {"prescribed_proposal_sections": [{"heading": "Methodology", "page_limit": "8"}]},
    "bid_intelligence": {
        "score_version": "2.3",
        "why": "Strong sector and country fit. One senior role has no roster match, so the bid carries staffing risk.",
        "fit": {"score": 82, "status": "VERIFIED", "coverage": 0.9, "evidence": "Matched on sector, country and donor history."},
        "risk": {"score": 38, "status": "INFERRED", "unit": "0 to 100, higher is riskier"},
        "strategic_value": {"score": 64, "status": "INFERRED"},
        "commercial_value": {"contract_value_usd": 185000, "status": "VERIFIED", "evidence": "Ceiling stated in the terms of reference."},
        "win_probability": 61,
        "confidence": {"score": 72, "status": "INFERRED", "verified_factors": 6, "inferred_factors": 3, "unknown_factors": 2},
        "factor_values": {"sector_match": 90, "country_experience": 80, "donor_history": 70, "language": 100, "past_award": None},
        "factor_evidence": [
            {"name": "sector_match", "value": 90, "status": "VERIFIED", "evidence": "WASH is a core Cortech sector."},
            {"name": "country_experience", "value": 80, "status": "VERIFIED", "evidence": "Four past assignments in Kenya."},
            {"name": "donor_history", "value": 70, "status": "INFERRED", "evidence": "Two UNICEF proposals on file, outcomes unknown."},
            {"name": "past_award", "value": None, "status": "UNKNOWN", "evidence": ""},
        ],
        "gaps": ["No hydrogeologist on the current roster"],
        "risks": ["Short mobilisation window", "Insecurity in two target wards"],
        "weights": {"fit": {"sector_match": 0.3, "country_experience": 0.25}},
        "llm_audit": {"cortech_fit_score": 79, "win_probability": 58, "bid_recommendation": "BID",
                      "note": "The model reading is kept for audit."},
    },
}

SECTIONS = {
    "executive_summary": "Cortech will deliver a household assessment across six sub-counties. " * 12,
    "methodology": "A mixed-methods design with a stratified household survey. " * 14,
    "team": "A senior public health lead supported by two field coordinators. " * 8,
    "quality_score": {"overall_score": 81, "weakest_section": "team"},
    "claim_grounding": {"summary": "Eleven claims checked, two could not be traced.",
                        "claims": [{"status": "Verified", "claim": "Ten WASH assessments completed since 2019."},
                                   {"status": "NOT VERIFIED", "claim": "Operates a regional data centre in Nairobi."}]},
    "committee_grade": {"warning": "A model reading of the draft, not a panel's score.", "bar_percent": 80,
                        "score_percent": 76, "points_short": 4,
                        "rows": [{"criterion": "Technical approach", "mark": 32, "weight": 40,
                                  "reason": "Sound design, thin on sampling.",
                                  "improvement": "State the sampling frame and the replacement rule."},
                                 {"criterion": "Team", "mark": 27, "weight": 35,
                                  "reason": "One role unfilled.", "improvement": "Name the hydrogeologist."}]},
    "requirement_alignment": {"headline": "5 of 6 requirements addressed.", "criteria_score_percent": 83,
                              "criteria_bar_percent": 80,
                              "rows": [{"label": "Inception report in three weeks", "status": "addressed", "section": "methodology", "quote": "Inception report within three weeks."},
                                       {"label": "Swahili-speaking enumerators", "status": "partially_addressed", "section": "team", "quote": "Field coordinators speak Swahili."},
                                       {"label": "Hydrogeology input", "status": "missing", "section": "", "evidence": "No chapter names the role."}]},
}

TEAM = {"team_strength_score": 78, "roles": [
    {"role_title": "Team leader", "recommended": {"name": "Amina Njoroge", "match_score": 92, "availability": "Available",
                                                  "title": "Senior public health specialist", "sectors": ["WASH", "Health"],
                                                  "countries": ["Kenya", "Uganda"], "languages": ["English", "Swahili"],
                                                  "justification": "Led four comparable baselines."}},
    {"role_title": "WASH engineer", "recommended": {"name": "Peter Mugo", "match_score": 64, "availability": "Check dates"}},
    {"role_title": "Hydrogeologist", "gap_flagged": True, "gap_reason": "No hydrogeologist on file."},
]}

CLIENT_INTEL = {"client": {"headline": "Three stored records for UNICEF Kenya.", "outcome_note": "Outcomes are unknown, not lost.",
                           "citations": [{"title": "Nutrition survey 2023", "role": "Prime", "outcome": "UNKNOWN", "outcome_status": "UNKNOWN", "source_kind": "proposal", "source_id": "p-104"},
                                         {"title": "WASH baseline 2024", "role": "Sub", "outcome": "Won", "outcome_status": "VERIFIED", "source_kind": "proposal", "source_id": "p-131"}]}}

CHECKPOINTS = {
    URL_SCORED: {"analysis": ANALYSIS, "matched_team_result": TEAM, "client_intelligence": CLIENT_INTEL},
    URL_DRAFTED: {"analysis": {**ANALYSIS, "opportunity": {**ANALYSIS["opportunity"], "title": LEDGER[1]["title"], "client": "GIZ", "donor": "BMZ"}},
                  "proposal_sections": SECTIONS, "matched_team_result": TEAM, "client_intelligence": CLIENT_INTEL,
                  "budget": {"lines": [{"role": "Team leader", "days": 40, "daily_rate_usd": None, "subtotal_usd": None},
                                       {"role": "Field coordinator", "days": 60, "daily_rate_usd": 310, "subtotal_usd": 18600}]},
                  "final_grade": {"rows": [{"criterion": "Technical approach", "weight": 40, "mark": 31, "reason": "Clear.", "quote": "A stratified household design."}],
                                  "headline": "78% against the stored criteria.", "below_bar": True, "filename": "final-v3.docx",
                                  "uploaded_by": "staff@cortechconsultinggroup.com", "percent": 78}},
}

JOBS = [
    {"id": "9d3c1a7e-0001-4a8b-9c1d-111111111111", "source_url": URL_FLIGHT, "trigger_kind": "submit_url", "status": "running",
     "requested_by": "staff@cortechconsultinggroup.com", "requested_at": _ts(hours=1), "started_at": _ts(hours=1),
     "finished_at": None, "attempt_count": 1, "error_kind": None, "error_message": None, "execution_id": "ex-1",
     "result_summary": None, "cancel_requested_at": None, "cancelled_by": None, "last_heartbeat_at": _ts(hours=0.01),
     "spend_usd": 0.4120, "spend_limit_usd": 2.0, "provider_calls": 7, "spend_known": True},
    {"id": "5b2e0c44-0002-4a8b-9c1d-222222222222", "source_url": URL_DRAFTED, "trigger_kind": "draft_existing", "status": "succeeded",
     "requested_by": "staff@cortechconsultinggroup.com", "requested_at": _ts(days=1), "started_at": _ts(days=1),
     "finished_at": _ts(days=1, hours=-0.2), "attempt_count": 1, "error_kind": None, "error_message": None, "execution_id": "ex-2",
     "result_summary": {}, "cancel_requested_at": None, "cancelled_by": None, "last_heartbeat_at": _ts(days=1),
     "spend_usd": 1.2840, "spend_limit_usd": 2.0, "provider_calls": 19, "spend_known": True},
    {"id": "7a9f3d10-0003-4a8b-9c1d-333333333333", "source_url": URL_SPEND, "trigger_kind": "submit_url", "status": "failed",
     "requested_by": "ali@cortechconsultinggroup.com", "requested_at": _ts(days=2), "started_at": _ts(days=2),
     "finished_at": _ts(days=2), "attempt_count": 1, "error_kind": "SPEND_CAP_ERROR", "error_message": "per-run LLM spend cap reached",
     "execution_id": "ex-3", "result_summary": {}, "cancel_requested_at": None, "cancelled_by": None, "last_heartbeat_at": _ts(days=2),
     "spend_usd": 2.0100, "spend_limit_usd": 2.0, "provider_calls": 24, "spend_known": True},
    {"id": "3c8d5e22-0004-4a8b-9c1d-444444444444", "source_url": URL_DEAD, "trigger_kind": "draft_existing", "status": "failed",
     "requested_by": "ali@cortechconsultinggroup.com", "requested_at": _ts(days=3), "started_at": _ts(days=3),
     "finished_at": _ts(days=3), "attempt_count": 3, "error_kind": "DEAD_LETTER", "error_message": "draft came back empty after 3 attempts",
     "execution_id": "ex-4", "result_summary": {}, "cancel_requested_at": None, "cancelled_by": None, "last_heartbeat_at": _ts(days=3),
     "spend_usd": None, "spend_limit_usd": 2.0, "provider_calls": None, "spend_known": False},
    {"id": "1e7a6b33-0005-4a8b-9c1d-555555555555", "source_url": URL_SCORED, "trigger_kind": "submit_url", "status": "queued",
     "requested_by": "staff@cortechconsultinggroup.com", "requested_at": _ts(hours=0.05), "started_at": None,
     "finished_at": None, "attempt_count": 0, "error_kind": None, "error_message": None, "execution_id": None,
     "result_summary": None, "cancel_requested_at": None, "cancelled_by": None, "last_heartbeat_at": None,
     "spend_usd": None, "spend_limit_usd": 2.0, "provider_calls": None, "spend_known": None},
]

LIBRARY_ROWS = [
    {"id": "d1", "title": "UNICEF WASH baseline, Kenya", "original_filename": "unicef-wash.pdf", "client": "UNICEF", "donor": "UNICEF",
     "country": "Kenya", "proposal_type": "technical", "submission_date": "2024-03-18", "outcome": "won",
     "uploaded_by_name": "Amina Njoroge", "uploaded_by_email": "staff@cortechconsultinggroup.com", "status": "ready", "error_message": None, "kind": "proposal"},
    {"id": "d2", "title": "GIZ climate evaluation EOI", "original_filename": "giz-eoi.docx", "client": "GIZ", "donor": "BMZ",
     "country": "Ethiopia", "proposal_type": "eoi", "submission_date": "2023-11-02", "outcome": "unknown",
     "uploaded_by_name": "Ali Hassan", "uploaded_by_email": "ali@cortechconsultinggroup.com", "status": "processing", "error_message": None, "kind": "proposal"},
    {"id": "d3", "title": "FAO value chain proposal", "original_filename": "fao.pdf", "client": "FAO", "donor": None,
     "country": "Somalia", "proposal_type": None, "submission_date": None, "outcome": "lost",
     "uploaded_by_name": "Ali Hassan", "uploaded_by_email": "ali@cortechconsultinggroup.com", "status": "failed", "error_message": "the PDF has no text layer", "kind": "proposal"},
]


def _digest():
    from intelligence.market_trends import ObservedOpportunity, build_market_digest

    themes = [("WASH",), ("Health",), ("Education",), ("Climate",), ("WASH", "Climate"), ("Governance",)]
    places = [("Kenya",), ("Uganda",), ("Ethiopia",), ("Somalia",), ("Kenya", "Uganda")]
    recs = []
    for i in range(34):
        recs.append(ObservedOpportunity(
            source_id=f"s{i}", source="supabase", source_url=U + f"d{i}", title=f"Opportunity {i}",
            discovered_on=date.today() - timedelta(days=i * 2), themes=themes[i % len(themes)],
            locations=places[i % len(places)], donor=("UNICEF", "GIZ", "World Bank", "EU")[i % 4],
        ))
    return build_market_digest(recs, as_of=date.today(), org_index=[], orgs_available=False,
                               truncated=False, cited_awards=[])


def install_fixtures():
    from dashboard import app as app_module
    from dashboard import library as library_mod
    from dashboard import queries, triggers

    queries.list_ledger_items = lambda limit=200: [queries._ledger_row_to_item(r) for r in LEDGER]
    queries.list_cache_without_ledger = lambda limit=50, scan=2000: (
        [queries._cache_row_to_item(r) for r in CACHE], 1210)
    queries._ledger_one = lambda url: next(
        (queries._ledger_row_to_item(r) for r in LEDGER if r["source_url"] == url), None)
    queries._cache_one = lambda url: next((r for r in CACHE if r["source_url"] == url), None)
    queries._checkpoint = lambda url: CHECKPOINTS.get(url, {})
    queries.draft_sections = lambda url: SECTIONS if url == URL_DRAFTED else {}
    queries.organization_history = lambda name: {
        "available": True, "note": "", "match": {"canonical_name": name, "status": "VERIFIED", "method": "exact", "confidence": 0.98},
        "records": [{"title": "Nutrition survey 2023", "role": "Prime", "outcome": "UNKNOWN", "outcome_status": "UNKNOWN",
                     "source_kind": "proposal", "source_id": "p-104"}]}
    queries.outcome_census = lambda: {"won": 3, "lost": 0, "unknown": 12, "labelled_total": 3,
                                      "threshold_note": "Calibration needs n_labelled of at least 30 and at least 10 per class (ADR 010). Current: 3 WON / 0 LOST, so calibrated_win_probability stays null.",
                                      "notes": ["win_loss_memory table does not exist, so no VERIFIED LOST source"]}

    def portfolio_view():
        by_stage = {s: 0 for s in queries.ALL_STAGES}
        by_stage.update({"discovered": 8, "extracted": 4, "scored": 6, "drafted": 7, "reviewed": 2, "outcome": 1})
        return {"by_stage": by_stage, "no_stage": 0,
                "by_state": {"pending": 5, "processing": 2, "completed": 18, "failed": 2, "dead_letter": 1, "spend_cap": 1},
                "by_recommendation": {"BID": 10, "WATCH": 9, "NO-BID": 6}, "unscored_ledger_rows": 2,
                "ledger_rows": 28, "cache_rows": 1210, "outcomes": queries.outcome_census()}

    queries.portfolio_view = portfolio_view
    queries.market_digest = lambda force=False: _digest()

    from dashboard import settings

    def decorate(row):
        return triggers._decorate(row)

    triggers.recent = lambda limit=15: [decorate(j) for j in JOBS][:limit]
    triggers.in_flight_by_url = lambda: {JOBS[0]["source_url"]: decorate(JOBS[0])}
    triggers.for_url = lambda url, limit=5: [decorate(j) for j in JOBS if j["source_url"] == url][:limit]
    triggers.get = lambda tid: next((decorate(j) for j in JOBS if j["id"] == tid), None)
    triggers.queue_available = lambda: True
    triggers.spend_window = lambda hours=None: {
        "spent_usd": 3.71, "runs": 5, "runs_unknown_cost": 0, "window_start": _ts(hours=24),
        "hours": 24, "cap_usd": settings.AGGREGATE_CAP_USD, "available": True}
    triggers.aggregate_cap_reached = lambda window=None: (False, "3.71 of 10.00 USD used in the last 24h")

    app_module._library_client = lambda: object()
    library_mod.list_documents = lambda sb, tab="proposals", filters=None: (
        [r for r in LIBRARY_ROWS if (tab != "cvs")] if tab in ("proposals", "mine") else
        ([r for r in LIBRARY_ROWS if r["status"] == "processing"] if tab == "processing" else []))
    return app_module


PAGES = [
    ("queue", "/"),
    ("detail-scored", "/opportunity?url=" + URL_SCORED),
    ("detail-drafted", "/opportunity?url=" + URL_DRAFTED),
    ("detail-halted", "/opportunity?url=" + URL_DEAD),
    ("job-running", "/job/" + JOBS[0]["id"]),
    ("job-failed", "/job/" + JOBS[3]["id"]),
    ("portfolio", "/portfolio"),
    ("library", "/library"),
    ("redesign", "/design-preview"),
    ("login", "/auth/login"),
    ("denied", "/auth/denied-preview"),
]


def serve(port: int) -> None:
    import uvicorn

    app_module = install_fixtures()
    from fastapi import Request
    from fastapi.responses import HTMLResponse

    globals()["Request"] = Request  # postponed annotations resolve against module globals

    @app_module.app.get("/auth/denied-preview", response_class=HTMLResponse)
    def denied_preview(request: Request):  # pragma: no cover - harness only
        return app_module._denied(
            request, "That account is not allowed", "outside.person@gmail.com is not on an approved domain.", 403,
            hint="Sign in with your Cortech Google account.")

    uvicorn.run(app_module.app, host="127.0.0.1", port=port, log_level="warning")


def shoot(outdir: str, port: int = 8765, only: list[str] | None = None) -> None:
    from playwright.sync_api import sync_playwright

    from dashboard import auth, settings

    settings.SESSION_SECRET = os.environ["DASHBOARD_SESSION_SECRET"]
    token = auth.issue_session("staff@cortechconsultinggroup.com")
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, __file__, "serve", str(port)], cwd=str(ROOT))
    try:
        import urllib.request

        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            for width, height in ((1440, 900), (1024, 768), (390, 844)):
                ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                ctx.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": token,
                                  "url": f"http://127.0.0.1:{port}"}])
                anon = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                page = ctx.new_page()
                for name, path in PAGES:
                    if only and name not in only:
                        continue
                    # The sign-in screen redirects a signed-in visitor, so it is shot anonymously.
                    page = anon.new_page() if name == "login" else ctx.new_page()
                    page.goto(f"http://127.0.0.1:{port}{path}", wait_until="networkidle")
                    page.evaluate("document.fonts.ready")
                    page.add_style_tag(content="*{scroll-behavior:auto!important}")
                    if name.startswith("detail") or name.startswith("job"):
                        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
                    page.screenshot(path=str(out / f"{name}-{width}.png"), full_page=True)
                    page.close()
                anon.close()
                ctx.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve(int(sys.argv[2]) if len(sys.argv) > 2 else 8765)
    elif cmd == "shoot":
        shoot(sys.argv[2], only=sys.argv[3].split(",") if len(sys.argv) > 3 else None)
    else:
        raise SystemExit(__doc__)
