"""Dashboard view-layer guarantees.

Three things are pinned here:

* the app cannot take an outward action (no submit/send route, no import of
  the client-facing senders);
* every route is behind the session gate;
* a missing value renders as an em dash with its reason, and never as a
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
        "/opportunity/outcome", "/opportunity/reviewed",
        "/trigger/bulk", "/trigger/cancel", "/trigger/existing",
        "/trigger/rerun", "/trigger/retry", "/trigger/url",
    ]
    # Queue posts enqueue or cancel. Review posts record a person's decision.
    # None calls the pipeline inline and none can act outward.
    src = inspect.getsource(app_module)
    assert "submit_single_url" not in src
    assert "process_opportunity" not in src


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


def test_value_macro_renders_an_em_dash_with_its_reason(env):
    t = env.from_string(
        '{% import "_macros.html" as m %}{{ m.value(c) }}'
    )
    out = t.render(c=queries.cell(None, why="not yet scored", source="tbl.col"))
    assert "—" in out
    assert "v-missing" in out
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
    assert "—" in out
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
        url_trigger_existing="/te", url_draft="/draft", generated_at="now",
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
    detail = _detail("completed", "", stage="drafted")
    detail["has_draft"] = True
    detail["section_keys"] = ["executive_summary", "methodology"]
    html = _render_detail(env, detail)
    assert "needs human review before anything is sent" in html
    assert "Mark reviewed" in html
    assert "Mark won" in html
    assert "Mark lost" in html
    assert "Download draft" in html
    for word in (">Send<", "Submit to", "Send to client"):
        assert word not in html


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
