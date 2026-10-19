"""Dashboard view-layer guarantees.

Three things are pinned here:

* the app cannot take an outward action (no submit/send route, no import of
  the client-facing senders);
* every route is behind the session gate;
* a missing value renders as a grey "n/a" with its reason, and never as a
  plausible-looking number.

The fixtures below build view dicts in memory. That is a *test* fixture, not
shipped UI — the shipped views are fed by ``dashboard.queries``, which reads
real rows only. Rendering states like ``dead_letter`` deterministically is the
only way to assert on them without corrupting a production row.
"""
from __future__ import annotations

import inspect

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.testclient import TestClient

from dashboard import app as app_module
from dashboard import auth, queries, settings

TEMPLATES = "dashboard/templates"


@pytest.fixture
def env():
    return Environment(
        loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"])
    )


# ── the tool cannot act outward ─────────────────────────────────────────────

def test_no_route_can_submit_or_send():
    paths = [r.path for r in app_module.app.routes if hasattr(r, "path")]
    for path in paths:
        low = path.lower()
        for forbidden in ("submit", "send", "email", "publish", "award", "apply"):
            assert forbidden not in low, f"route {path} looks like an outward action"


def test_app_does_not_import_the_client_facing_senders():
    """The web service must have no way to email or submit anything."""
    src = inspect.getsource(app_module)
    for name in (
        "send_proposal_email",
        "send_report",
        "send_deadline_alert_email",
        "send_market_digest_email",
        "smtplib",
    ):
        assert name not in src, f"dashboard/app.py references {name}"


def test_post_routes_are_limited_to_queueing():
    posts = sorted(
        r.path
        for r in app_module.app.routes
        if hasattr(r, "methods") and "POST" in (r.methods or set())
    )
    assert posts == [
        "/api/opportunity/outline",
        "/opportunity/final-grade",
        "/opportunity/outcome", "/opportunity/outline", "/opportunity/reviewed",
        "/trigger/bulk", "/trigger/cancel", "/trigger/existing",
        "/trigger/rerun", "/trigger/retry", "/trigger/url",
    ]
    # Queue posts enqueue or cancel. Review posts record a person's decision.
    # The outline post stores chapters for a later draft. The final-grade post
    # scores an uploaded file and stores the reading. None calls the pipeline
    # inline and none can act outward.
    src = inspect.getsource(app_module)
    assert "submit_single_url" not in src
    assert "process_opportunity" not in src


def test_scored_rows_wait_for_an_outline_instead_of_looking_in_flight():
    item = {"stage": {"value": "scored"}, "ui_state": "pending"}
    assert queries.queue_bucket(item) == "choose_outline"
    item["ui_state"] = "processing"
    assert queries.queue_bucket(item) == "in_flight"
    assert queries.queue_bucket({"stage": {"value": "extracted"}, "ui_state": "pending"}) == "in_flight"
    assert queries.queue_bucket({"stage": {"value": "drafted"}, "ui_state": "pending"}) == "drafted"
    assert queries.queue_bucket({"stage": {"value": "scored"}, "ui_state": "failed"}) == "halted"


def test_pipeline_stages_the_dashboard_exposes_stop_at_drafted():
    """`reviewed` and `outcome` are human transitions; the rail must not
    present them as something the agent (or this tool) advances."""
    assert queries.AGENT_STAGES == ("discovered", "extracted", "scored", "drafted")
    assert queries.HUMAN_STAGES == ("reviewed", "outcome")
    rail = queries.stage_rail("drafted")
    assert [s["name"] for s in rail] == list(queries.AGENT_STAGES)


# ── every route is gated ────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "view-test-secret")
    monkeypatch.setattr(settings, "AUTH_ALLOWED_DOMAIN", "cortechconsultinggroup.com")
    return TestClient(app_module.app, follow_redirects=False)


def test_pages_redirect_anonymous_users_to_login(client):
    for path in ("/", "/portfolio", "/opportunity?url=x", "/draft?url=x"):
        r = client.get(path)
        assert r.status_code == 302, path
        assert r.headers["location"] == "/auth/login"


def test_api_returns_401_for_anonymous_users(client):
    for path in ("/api/queue", "/api/portfolio", "/api/opportunity?url=x"):
        r = client.get(path)
        assert r.status_code == 401, path


def test_trigger_posts_reject_anonymous_users(client):
    r = client.post("/trigger/url", data={"url": "https://example.org/x"})
    assert r.status_code == 302
    r = client.post("/trigger/existing", data={"source_url": "https://example.org/x"})
    assert r.status_code == 302


def test_healthz_is_public_and_touches_nothing(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_security_headers_are_present(client):
    r = client.get("/healthz")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_trigger_requires_a_valid_csrf_token(client, monkeypatch):
    token = auth.issue_session("staff@cortechconsultinggroup.com")
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    r = client.post(
        "/trigger/url",
        data={"url": "https://example.org/x", "csrf_token": "wrong"},
    )
    assert r.status_code == 403


def test_trigger_with_a_valid_csrf_token_gets_past_the_check(client, monkeypatch):
    """A correct token must not be rejected — otherwise the check is untested."""
    enqueued = {}

    def _enqueue(url, kind, by):
        enqueued.update({"url": url, "kind": kind, "by": by})
        return "trigger-id"

    monkeypatch.setattr(app_module.triggers, "enqueue", _enqueue)
    monkeypatch.setattr(app_module.triggers, "queue_available", lambda: True)
    monkeypatch.setattr(app_module.triggers, "aggregate_cap_reached", lambda: (False, ""))
    monkeypatch.setattr(settings, "TRIGGERS_ENABLED", True)

    token = auth.issue_session("staff@cortechconsultinggroup.com")
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    session = auth.read_session(token)

    r = client.post(
        "/trigger/url",
        data={"url": "https://example.org/x", "csrf_token": auth.csrf_token(session)},
    )
    assert r.status_code == 303
    assert enqueued["kind"] == "submit_url"
    assert enqueued["by"] == "staff@cortechconsultinggroup.com"


def test_draft_now_stores_the_chapters_and_does_not_force_a_rescore(client, monkeypatch):
    seeded = {}
    enqueued = {}

    def _seed(url, outline):
        seeded["url"] = url
        seeded["outline"] = outline
        return True, "saved"

    def _enqueue(url, kind, by):
        enqueued.update({"url": url, "kind": kind, "by": by})
        return "trigger-id"

    monkeypatch.setattr(app_module, "seed_direct_draft", _seed)
    monkeypatch.setattr(app_module.triggers, "enqueue", _enqueue)
    monkeypatch.setattr(app_module.triggers, "queue_available", lambda: True)
    monkeypatch.setattr(app_module.triggers, "aggregate_cap_reached", lambda: (False, ""))
    monkeypatch.setattr(app_module.triggers, "in_flight_by_url", lambda: {})
    monkeypatch.setattr(settings, "TRIGGERS_ENABLED", True)

    token = auth.issue_session("staff@cortechconsultinggroup.com")
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    session = auth.read_session(token)
    response = client.post(
        "/trigger/url",
        data={
            "url": "https://example.org/tor",
            "csrf_token": auth.csrf_token(session),
            "intent": "draft",
            "template_id": "house_eoi",
            "submission_type": "EOI",
            "outline_name": "Cortech EOI",
            "heading": ["Letter of interest", "Firm profile"],
            "page_limit": ["1", ""],
            "must_include": ["why Cortech", ""],
        },
    )
    assert response.status_code == 303
    assert enqueued["kind"] == "draft_existing"
    assert seeded["outline"]["submission_type"] == "EOI"
    assert seeded["outline"]["sections"][0]["heading"] == "Letter of interest"
    assert seeded["outline"]["sections"][0]["must_include"] == ["why Cortech"]


def test_mark_reviewed_records_a_human_stage_and_does_not_send(client, monkeypatch):
    seen = {}

    def _record(url, stage):
        seen["url"] = url
        seen["stage"] = stage
        return True

    monkeypatch.setattr("database.supabase_client.record_human_pipeline_stage", _record)
    token = auth.issue_session("staff@cortechconsultinggroup.com")
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    session = auth.read_session(token)
    response = client.post(
        "/opportunity/reviewed",
        data={
            "source_url": "https://example.org/tor",
            "csrf_token": auth.csrf_token(session),
        },
    )
    assert response.status_code == 303
    assert seen == {"url": "https://example.org/tor", "stage": "reviewed"}
    assert "send" not in response.headers["location"].lower()


def test_mark_won_sets_crm_status_and_outcome(client, monkeypatch):
    seen = {}

    def _record(url, stage):
        seen["stage"] = stage
        seen["url"] = url
        return True

    def _update(record_id, fields):
        seen["record_id"] = record_id
        seen["status"] = fields.get("status")
        return True

    monkeypatch.setattr("database.supabase_client.record_human_pipeline_stage", _record)
    monkeypatch.setattr("database.airtable_client.update_opportunity", _update)
    token = auth.issue_session("staff@cortechconsultinggroup.com")
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    session = auth.read_session(token)
    response = client.post(
        "/opportunity/outcome",
        data={
            "source_url": "https://example.org/tor",
            "outcome": "Won",
            "csrf_token": auth.csrf_token(session),
        },
    )
    assert response.status_code == 303
    assert seen["status"] == "Won"
    assert seen["stage"] == "outcome"
    assert seen["record_id"] == "https://example.org/tor"


# ── the value primitive never invents ───────────────────────────────────────

@pytest.mark.parametrize("missing", [None, "", "   "])
def test_cell_treats_blank_as_absent_and_keeps_the_reason(missing):
    c = queries.cell(missing, why="not yet scored", source="tbl.col")
    assert c["value"] is None
    assert c["why"] == "not yet scored"


@pytest.mark.parametrize("present", [0, 0.0, "0", False, "BID", 75])
def test_cell_keeps_real_falsy_values(present):
    """A real zero is data. It must not be swallowed as 'missing'."""
    c = queries.cell(present, why="not yet scored", source="tbl.col")
    assert c["value"] == present
    assert c["why"] == ""


def test_num_never_invents_a_zero():
    assert queries._num(None) is None
    assert queries._num("") is None
    assert queries._num("not a number") is None
    assert queries._num("nan") is None
    assert queries._num("inf") is None
    assert queries._num("0") == 0
    assert queries._num("75.0") == 75
    assert queries._num("75.5") == 75.5


def test_value_macro_renders_a_marked_absence_with_its_reason(env):
    t = env.from_string(
        '{% import "_macros.html" as m %}{{ m.value(c) }}'
    )
    out = t.render(c=queries.cell(None, why="not yet scored", source="tbl.col"))
    assert '<span class="v-missing" title="tbl.col">n/a</span>' in out
    assert "—" not in out
    assert "not yet scored" in out
    # Nothing that could read as a value.
    assert ">0<" not in out and "N/A" not in out


def test_value_macro_escapes_untrusted_document_text(env):
    t = env.from_string('{% import "_macros.html" as m %}{{ m.doc_value(c) }}')
    out = t.render(
        c=queries.cell("<script>alert(1)</script>", source="tbl.col")
    )
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_terse_macro_keeps_the_reason_in_the_tooltip(env):
    t = env.from_string('{% import "_macros.html" as m %}{{ m.value_terse(c) }}')
    out = t.render(c=queries.cell(None, why="no ledger row", source="tbl.col"))
    assert ">n/a<" in out and "—" not in out
    assert 'title="no ledger row' in out


def test_recommendation_chip_uses_no_status_colour(env):
    """NO-BID must not be rendered in the red reserved for machine failure."""
    t = env.from_string('{% import "_macros.html" as m %}{{ m.rec_chip(c) }}')
    for rec, klass in (("BID", "chip--bid"), ("WATCH", "chip--watch"), ("NO-BID", "chip--nobid")):
        out = t.render(c=queries.cell(rec, source="s"))
        assert klass in out
        assert "state--" not in out
    out = t.render(c=queries.cell(None, why="not yet scored", source="s"))
    assert "chip--none" in out
    assert "not scored" in out


# ── halt states are visible in the UI, not only in logs ─────────────────────

def _detail(ui_state: str, last_error: str, stage: str = "scored", fails: int = 0) -> dict:
    d = {
        "source_url": "https://example.org/t",
        "has_ledger": True,
        "ui_state": ui_state,
        "draft_fail_count": fails,
        "attempt_count": 1,
        "has_draft": False,
        "scored": False,
        "rail": queries.stage_rail(stage),
        "section_keys": [],
        "factors": [], "gaps": [], "risks": [],
        "confidence": {}, "llm_audit": {}, "weights": {},
        "locations": [], "thematic_areas": [],
        "client_intelligence": {},
        "in_cache": False,
        "discovered_at": None, "updated_at": None, "completed_at": None,
        "stage": queries.cell(stage, source="opportunity_processing.pipeline_stage"),
        "lease_state": queries.cell(ui_state, source="opportunity_processing.state"),
        "last_error": queries.cell(last_error, source="opportunity_processing.last_error"),
    }
    for key in (
        "title", "client", "donor", "deadline", "recommendation", "fit",
        "win_probability", "submission_type", "airtable_record_id",
        "score_version", "why", "expected_value", "budget_usd",
        "reference_number", "content_hash",
    ):
        d[key] = queries.cell(None, why="not available", source="x")
    d["win_heuristic"] = queries.cell(
        None, why="not yet scored", source="x", label="heuristic (see docs/SCORING_MODEL.md)"
    )
    d["win_calibrated"] = queries.cell(
        None, why="INSUFFICIENT DATA", source="x", label="calibration harness, ADR 010"
    )
    for key in ("fit_dim", "risk_dim", "strategic_dim", "commercial_dim"):
        d[key] = {
            "score": queries.cell(None, why="not extracted", source="x"),
            "status": "UNKNOWN", "unit": "", "coverage": None,
            "evidence": queries.cell(None, why="none", source="x"),
        }
    return d


def _render_detail(env, detail):
    return env.get_template("detail.html").render(
        d=detail, org={"available": False, "note": "n/a", "match": None, "records": []},
        view="queue", viewer="t@cortechconsultinggroup.com", csrf_token="t",
        poll_ms=15000, static_base="/static", url_queue="/", url_portfolio="/p",
        url_detail="/o", url_logout="/l", url_trigger_url="/tu",
        url_trigger_existing="/te", url_draft="/draft", url_outline="/opportunity/outline",
        generated_at="now",
        in_flight_urls=set(), triggering_enabled=True,
        triggering_disabled_reason="", flash="", flash_kind="",
    )


def test_dead_letter_state_is_shown_with_its_reason(env):
    html = _render_detail(
        env, _detail("dead_letter", "drafting failed: empty client-facing draft", fails=3)
    )
    assert "Dead-lettered" in html
    assert "empty client-facing draft" in html
    assert "will not retry on its own" in html
    assert "notice--stop" in html


def test_spend_cap_state_is_shown_as_retryable_not_as_a_draft_failure(env):
    html = _render_detail(env, _detail("spend_cap", "per-run LLM spend cap reached"))
    assert "MAX_RUN_COST_USD" in html
    assert "per-run LLM spend cap reached" in html
    assert "not</em> a drafting failure" in html or "not a drafting failure" in html
    assert "no exemption from the cap" in html


def test_retryable_failure_says_the_checkpoint_is_preserved(env):
    html = _render_detail(env, _detail("failed", "fetch failed: 503"))
    assert "Retryable failure" in html
    assert "fetch failed: 503" in html
    assert "resumes" in html


def test_win_probability_always_carries_its_heuristic_label(env):
    """A layout change must not separate the number from its caveat."""
    detail = _detail("completed", "")
    detail["win_heuristic"] = queries.cell(
        58, source="x", label="heuristic (see docs/SCORING_MODEL.md)"
    )
    html = _render_detail(env, detail)
    assert "58" in html
    assert "heuristic (see docs/SCORING_MODEL.md)" in html


def test_calibrated_win_probability_is_shown_empty_never_hidden(env):
    html = _render_detail(env, _detail("completed", ""))
    assert "Calibrated P(win)" in html
    assert "INSUFFICIENT DATA" in html


def test_drafted_page_shows_the_human_gate_and_no_send_control(env):
    from intelligence.draft_templates import catalog

    detail = _detail("completed", "", stage="drafted")
    detail["has_draft"] = True
    detail["section_keys"] = ["executive_summary", "methodology"]
    detail["draft_templates"] = catalog()
    html = _render_detail(env, detail)
    assert "Nothing here is sent to the client" in html
    assert "Change the chapters" in html
    assert html.find("Change the chapters") < html.find("WIN PROBABILITY")
    assert "Write the draft" in html
    assert "Mark reviewed" in html
    assert "Mark won" in html
    assert "Mark lost" in html
    assert "Grade this version" in html
    assert "Download draft" in html
    for word in (">Send<", "Submit to", "Send to client"):
        assert word not in html


def test_final_grade_below_the_bar_still_leaves_signoff_open(env):
    from intelligence.draft_templates import catalog
    from intelligence.final_grade import WARNING

    detail = _detail("completed", "", stage="drafted")
    detail["has_draft"] = True
    detail["section_keys"] = ["methodology"]
    detail["draft_templates"] = catalog()
    detail["final_grade"] = {
        "filename": "final.docx",
        "uploaded_by": "amina@cortechconsultinggroup.com",
        "percent": 40,
        "below_bar": True,
        "headline": f"This reading scores 40% against the stored criteria, under the 80% bar. {WARNING}",
        "rows": [{
            "criterion": "Methodology",
            "weight": 40,
            "mark": 0,
            "reason": "the quoted passage is not in the file.",
            "quote": "",
        }],
    }
    html = _render_detail(env, detail)
    assert "Grade the final version" in html
    assert WARNING in html
    assert "Mark reviewed" in html
    assert "Mark won" in html
    assert "Mark lost" in html
    assert html.find("Change the chapters") < html.find("WIN PROBABILITY")


def test_scored_opportunity_leads_with_the_outline(env):
    from intelligence.draft_templates import catalog, recommend_outline

    detail = _detail("pending", "")
    detail["recommended_outline"] = recommend_outline({
        "bid_analysis": {"submission_type": "FULL_PROPOSAL"},
        "submission_requirements": {"prescribed_proposal_sections": [
            {"heading": "Methodology", "page_limit": "8"},
        ]},
    })
    detail["draft_templates"] = catalog()
    detail["accepted_outline"] = None
    html = _render_detail(env, detail)
    assert "Choose the chapters" in html
    assert html.find("Choose the chapters") < html.find("WIN PROBABILITY")
    assert "Methodology" in html
    assert 'action="/opportunity/outline"' in html
    assert "Write the draft" in html
    assert "Score this ToR" not in html


def test_portfolio_bars_are_html_and_survive_without_a_canvas(env):
    from dashboard.queries import AGENT_STAGES, ALL_STAGES, bar_rows

    by_stage = {s: 0 for s in ALL_STAGES}
    by_stage.update({
        "discovered": 8, "extracted": 4, "scored": 6, "drafted": 7, "reviewed": 2,
    })
    stage_bars = bar_rows(
        [(s, by_stage[s]) for s in ALL_STAGES],
        total=27,
        tones=["agent" if s in AGENT_STAGES else "human" for s in ALL_STAGES],
    )
    rec_bars = bar_rows(
        [("BID", 10), ("WATCH", 9), ("NO-BID", 6)],
        total=25,
        tones=["bid", "watch", "nobid"],
    )
    html = env.get_template("portfolio.html").render(
        p={
            "ledger_rows": 27,
            "unscored_ledger_rows": 2,
            "cache_rows": 40,
            "by_stage": by_stage,
            "no_stage": 0,
            "by_state": {"completed": 20, "pending": 0},
            "outcomes": {
                "won": 3, "lost": 0, "unknown": 12,
                "threshold_note": "n is too small",
                "notes": [],
            },
        },
        stage_bars=stage_bars,
        rec_bars=rec_bars,
        scored=25,
        spend={"available": False},
        cap_blocked=False,
        dashboard_runs=[],
        charts={"spend": {"labels": [], "completed": [], "stopped": [], "unknown": [], "runs": [], "run_count": 0, "peak": 0}},
        digest=None,
        view="portfolio",
        viewer="t@cortechconsultinggroup.com",
        csrf_token="t",
        poll_ms=15000,
        static_base="/static",
        static_v="t",
        url_queue="/",
        url_portfolio="/portfolio",
        url_job="/job",
        url_logout="/l",
        generated_at="now",
        flash="",
        flash_kind="",
    )
    assert 'id="chart-stage"' not in html
    assert "chart.js" not in html
    assert "Airtable" not in html
    assert 'class="mix"' in html
    assert "bar__fill--agent" in html
    assert "bar__fill--human" in html
    assert "width: 29.6%" in html or "width: 29.6" in html
    assert stage_bars[0]["share"] == "30%" or stage_bars[0]["label"] == "discovered"
    drafted = next(row for row in stage_bars if row["label"] == "drafted")
    assert drafted["value"] == 7
    assert drafted["pct"] > 20
    assert "8 in the agent's stages" not in html
    assert "25 in the agent's stages" in html
    assert "2 recorded by a person" in html


# ── failure classification ──────────────────────────────────────────────────

def test_classify_failure_recognises_a_spend_cap_halt():
    assert queries.classify_failure("failed", "per-run LLM spend cap reached") == "spend_cap"
    assert queries.classify_failure("failed", "MAX_RUN_COST_USD exceeded") == "spend_cap"
    assert queries.classify_failure("failed", "fetch blew up") == "failed"
    assert queries.classify_failure("dead_letter", "x") == "dead_letter"
    assert queries.classify_failure("completed", None) == "completed"
    assert queries.classify_failure(None, None) == "pending"
    assert queries.classify_failure("nonsense", None) == "pending"


def test_outline_customizer_guards_against_disappearing_text_and_persists_drafts(env):
    from intelligence.draft_templates import catalog, recommend_outline

    detail = _detail("pending", "")
    detail["recommended_outline"] = recommend_outline({
        "bid_analysis": {"submission_type": "FULL_PROPOSAL"},
        "submission_requirements": {"prescribed_proposal_sections": [
            {"heading": "Methodology", "page_limit": "8"},
        ]},
    })
    detail["evaluation_criteria"] = [{"criterion": "Technical approach", "weight_percent": 50}]
    detail["deliverables"] = [{"name": "Inception Report", "description": "Within 2 weeks"}]
    detail["draft_templates"] = catalog()
    detail["accepted_outline"] = None
    html = _render_detail(env, detail)
    assert "cortech_outline_draft:" in html
    assert "isUserEditing()" in html
    assert "outline-input--heading" in html
    assert "outline-input--scope" in html
    assert "data-insert-tor" in html


def test_five_stage_bid_desk_components_render_across_stages(env):
    from intelligence.draft_templates import catalog, recommend_outline

    detail = _detail("completed", "", stage="drafted")
    detail["has_draft"] = True
    detail["fit"] = queries.cell(82, source="bid_scorer")
    detail["recommendation"] = queries.cell("BID", source="bid_scorer")
    detail["section_keys"] = ["executive_summary", "methodology"]
    detail["recommended_outline"] = recommend_outline({
        "bid_analysis": {"submission_type": "FULL_PROPOSAL"},
        "submission_requirements": {"prescribed_proposal_sections": [
            {"heading": "Methodology", "page_limit": "8"},
        ]},
    })
    detail["draft_templates"] = catalog()
    detail["matched_team_result"] = {
        "team_strength_score": 88,
        "roles": [{
            "role_title": "Team Leader",
            "recommended": {
                "name": "Dr. Amina Hassan",
                "match_score": 92,
                "availability": "Available",
            },
        }],
    }
    html = _render_detail(env, detail)
    # Top bar 5-stage stepper & 5-segment progress bar
    assert "Bid desk" in html
    assert "stage-progress" in html
    for label in ("Source", "Opportunities", "Analysis", "Draft", "Review pack"):
        assert label in html
    # Stage 3: 120px Fit Gauge Ring, Document Chip, Client History, Team Match bar
    assert "gauge-ring" in html
    assert "doc-chip" in html
    assert "Internal, not in the Word file" in html
    assert "Counted from stored records" in html
    assert "match-bar" in html
    assert "Dr. Amina Hassan" in html
    # Stage 4: Document Type choice cards, Template Gallery, No-fees rule
    assert "Technical Proposal" in html
    assert "Expression of Interest (EOI)" in html
    assert "Follow the TOR format" in html
    assert "No fees or prices go into the Word file" in html
    # Stage 5: Dark Headline Tiles, Internal Budget, Email Preview Card, Hard Stop
    assert "tile--dark" in html
    assert "Internal budget working" in html
    assert "Insufficient data" in html
    assert "DRAFT PROPOSAL:" in html
    assert "REVIEW REQUIRED" in html
    assert "Nothing here is sent to the client" in html


def test_trigger_url_supports_uploaded_tor_file_and_auto_draft(client, monkeypatch):
    seeded = {}
    enqueued = {}

    def _seed_auto(url, *, title="", full_text="", auto_accept_outline=True, accepted_outline=None):
        seeded.update({
            "url": url,
            "title": title,
            "full_text": full_text,
            "auto_accept_outline": auto_accept_outline,
            "accepted_outline": accepted_outline,
        })
        return True, "saved"

    def _enqueue(url, kind, by):
        enqueued.update({"url": url, "kind": kind, "by": by})
        return "trigger-upload-1"

    monkeypatch.setattr(app_module, "seed_auto_pipeline", _seed_auto)
    monkeypatch.setattr(app_module.triggers, "enqueue", _enqueue)
    monkeypatch.setattr(app_module.triggers, "queue_available", lambda: True)
    monkeypatch.setattr(app_module.triggers, "aggregate_cap_reached", lambda: (False, ""))
    monkeypatch.setattr(app_module.triggers, "in_flight_by_url", lambda: {})
    monkeypatch.setattr(settings, "TRIGGERS_ENABLED", True)

    token = auth.issue_session("staff@cortechconsultinggroup.com")
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    session = auth.read_session(token)

    tor_body = (
        "Terms of Reference: Endline Evaluation of the Resilience and Livelihoods Programme "
        "in Somalia and Kenya. Scope of work, methodology, deliverables, evaluation criteria, "
        "and team leader qualifications are detailed below. "
    ) * 8

    response = client.post(
        "/trigger/url",
        data={
            "csrf_token": auth.csrf_token(session),
            "intent": "auto",
            "auto_draft": "1",
        },
        files={"tor_file": ("UNICEF_Somalia_TOR.txt", tor_body.encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 303
    assert enqueued["kind"] == "draft_existing"
    assert seeded["auto_accept_outline"] is True
    assert "Endline Evaluation" in seeded["full_text"]
    assert "UNICEF Somalia TOR" in seeded["title"]
    assert seeded["url"].startswith("https://upload.cortechconsultinggroup.com/tor/")



