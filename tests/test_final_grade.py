"""Grade an edited final against stored criteria. Nothing is sent."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from config import CLAUDE_MODEL
from dashboard import app as app_module
from dashboard import auth, settings
from intelligence.final_grade import (
    NOT_SCORED,
    QUOTE_MISSING,
    WARNING,
    NoStoredCriteria,
    grade_final,
    prepare_criteria,
    settle_grade,
)
from starlette.testclient import TestClient

TEXT = (
    "The sample covers four regions with a field team based in Mogadishu "
    "throughout the assignment. Enumerators visit each site twice a quarter."
)


def _reply(rows: list[dict]):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps({"rows": rows}))]
    )


def test_missing_and_invented_criteria_fail_closed_without_raising():
    criteria = [
        {"criterion": "Methodology", "weight_percent": 40},
        {"criterion": "Team", "weight_percent": 60},
    ]
    grade = settle_grade(TEXT, criteria, {"rows": [
        {
            "criterion": "Methodology",
            "mark": 40,
            "reason": "The sample is described.",
            "quote": "The sample covers four regions",
        },
        {
            "criterion": "Budget",
            "mark": 20,
            "reason": "Invented.",
            "quote": "a fee of one thousand",
        },
    ]})
    names = [row["criterion"] for row in grade["rows"]]
    assert names == ["Methodology", "Team"]
    assert grade["rows"][0]["mark"] == 40
    team = grade["rows"][1]
    assert team["mark"] == 0
    assert team["reason"] == NOT_SCORED
    assert grade["percent"] == 40.0
    assert grade["below_bar"] is True
    assert WARNING in grade["headline"]


def test_a_quote_missing_from_the_file_scores_zero():
    criteria = [
        {"criterion": "Methodology", "weight_percent": 40},
        {"criterion": "Team", "weight_percent": 60},
    ]
    grade = settle_grade(TEXT, criteria, {"rows": [
        {
            "criterion": "Methodology",
            "mark": 40,
            "reason": "Claimed.",
            "quote": "this sentence was never written in the file",
        },
        {
            "criterion": "Team",
            "mark": 60,
            "reason": "Named the base.",
            "quote": "a field team based in Mogadishu",
        },
    ]})
    assert grade["rows"][0]["mark"] == 0
    assert grade["rows"][0]["reason"] == QUOTE_MISSING
    assert grade["rows"][1]["mark"] == 60
    assert grade["percent"] == 60.0
    assert grade["below_bar"] is True


def test_weights_sum_and_the_bar_is_only_a_warning():
    criteria = [
        {"criterion": "Methodology", "weight_percent": 40},
        {"criterion": "Team", "weight_percent": 60},
    ]
    grade = settle_grade(TEXT, criteria, {"rows": [
        {"criterion": "Methodology", "mark": 40, "reason": "Covered.", "quote": "The sample covers four regions"},
        {"criterion": "Team", "mark": 60, "reason": "Covered.", "quote": "a field team based in Mogadishu"},
    ]})
    assert grade["percent"] == 100.0
    assert grade["below_bar"] is False
    earned = sum(row["mark"] for row in grade["rows"])
    weights = sum(row["weight"] for row in grade["rows"])
    assert earned == weights == 100


def test_a_mark_above_the_weight_is_cut_back_to_the_weight():
    grade = settle_grade(TEXT, [{"criterion": "Methodology", "weight_percent": 40}], {"rows": [{
        "criterion": "Methodology",
        "mark": 90,
        "reason": "Too high.",
        "quote": "The sample covers four regions",
    }]})
    assert grade["rows"][0]["mark"] == 40
    assert grade["percent"] == 100.0


def test_unweighted_criteria_share_the_points_equally():
    prepared, note = prepare_criteria([
        {"criterion": "Methodology"},
        {"criterion": "Team"},
    ])
    assert [row["weight"] for row in prepared] == [50.0, 50.0]
    assert "equally" in note


def test_grade_final_calls_the_configured_model_once(monkeypatch):
    seen = {}

    def _complete(model, messages, system=None, max_tokens=8192, stage=""):
        seen["model"] = model
        seen["calls"] = seen.get("calls", 0) + 1
        seen["stage"] = stage
        return _reply([{
            "criterion": "Methodology",
            "mark": 40,
            "reason": "Covered.",
            "quote": "The sample covers four regions",
        }])

    monkeypatch.setattr("intelligence.final_grade.complete", _complete)
    grade = grade_final(TEXT, [{"criterion": "Methodology", "weight_percent": 100}])
    assert seen == {"model": CLAUDE_MODEL, "calls": 1, "stage": "final_grade"}
    assert grade["percent"] == 40.0
    assert grade["below_bar"] is True


def test_no_stored_criteria_does_not_call_the_model(monkeypatch):
    def _complete(*_args, **_kwargs):
        raise AssertionError("model called")

    monkeypatch.setattr("intelligence.final_grade.complete", _complete)
    with pytest.raises(NoStoredCriteria):
        grade_final(TEXT, [])


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store):
        self.store = store
        self.mode = "select"
        self.payload = None

    def select(self, *_args, **_kwargs):
        self.mode = "select"
        return self

    def update(self, payload):
        self.mode = "update"
        self.payload = payload
        return self

    def eq(self, *_args):
        return self

    def neq(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        self.store["calls"].append(self.mode)
        if self.mode == "select":
            return _Result([] if self.store.get("missing") else [self.store["row"]])
        if self.store["row"].get("state") == "processing":
            return _Result([])
        self.store["update"] = self.payload
        self.store["row"]["checkpoint"] = self.payload["checkpoint"]
        return _Result([{"ok": True}])


class _Supabase:
    def __init__(self, store):
        self.store = store

    def table(self, _name):
        return _Query(self.store)


def _patch_supabase(monkeypatch, store):
    monkeypatch.setattr(
        "database.supabase_client.get_supabase",
        lambda: _Supabase(store),
    )


def test_save_final_grade_leaves_the_draft_and_stage_untouched(monkeypatch):
    from database.supabase_client import save_final_grade

    store = {
        "calls": [],
        "row": {
            "state": "pending",
            "pipeline_stage": "drafted",
            "checkpoint": {
                "proposal_sections": {"methodology": "The agent draft."},
                "analysis": {"evaluation_criteria": [{"criterion": "Methodology"}]},
                "final_grade_queued": "true",
                "final_grade_request": {
                    "request_id": "r1",
                    "text": TEXT,
                    "filename": "final.txt",
                },
                "final_grade_error": "earlier failure",
            },
        },
    }
    _patch_supabase(monkeypatch, store)
    monkeypatch.setattr("database.airtable_client.log_agent_action", lambda *_a, **_k: None)
    ok, reason = save_final_grade(
        "https://example.org/tor",
        {
            "filename": "final.txt",
            "rows": [{"criterion": "Methodology", "mark": 10, "weight": 40}],
            "percent": 25,
            "text": TEXT,
            "request_id": "r1",
        },
        uploaded_by="amina@cortechconsultinggroup.com",
    )
    assert (ok, reason) == (True, "saved")
    payload = store["update"]
    assert set(payload) == {"checkpoint", "updated_at"}
    assert "pipeline_stage" not in payload
    checkpoint = payload["checkpoint"]
    assert checkpoint["proposal_sections"]["methodology"] == "The agent draft."
    assert checkpoint["final_grade"]["percent"] == 25
    assert checkpoint["final_grade"]["text"] == TEXT
    assert checkpoint["final_grade"]["uploaded_by"] == "amina@cortechconsultinggroup.com"
    assert "final_grade_request" not in checkpoint
    assert "final_grade_queued" not in checkpoint


def test_save_final_grade_refuses_while_a_run_is_in_progress(monkeypatch):
    from database.supabase_client import save_final_grade

    store = {
        "calls": [],
        "row": {"state": "processing", "pipeline_stage": "drafted", "checkpoint": {}},
    }
    _patch_supabase(monkeypatch, store)
    ok, reason = save_final_grade(
        "https://example.org/tor",
        {"rows": [{"criterion": "Methodology"}], "percent": 10},
    )
    assert ok is False
    assert "in progress" in reason
    assert "update" not in store["calls"]


def test_resaving_an_outline_keeps_the_final_grade(monkeypatch):
    from database.supabase_client import save_accepted_outline

    store = {
        "calls": [],
        "row": {
            "state": "pending",
            "pipeline_stage": "drafted",
            "checkpoint": {
                "analysis": {"bid_analysis": {"is_consultancy_contract": True}},
                "proposal_sections": {"methodology": "The agent draft."},
                "final_grade": {"percent": 40, "rows": [{"criterion": "Methodology"}]},
            },
        },
    }
    _patch_supabase(monkeypatch, store)
    ok, _reason = save_accepted_outline("https://example.org/tor", {
        "id": "house",
        "name": "Technical proposal",
        "sections": [{"heading": "Methodology", "page_limit": "4", "must_include": []}],
    })
    assert ok is True
    checkpoint = store["update"]["checkpoint"]
    assert "proposal_sections" not in checkpoint
    assert checkpoint["final_grade"]["percent"] == 40


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "grade-test-secret")
    monkeypatch.setattr(settings, "AUTH_ALLOWED_DOMAIN", "cortechconsultinggroup.com")
    return TestClient(app_module.app, follow_redirects=False)


def _signed(client):
    token = auth.issue_session("amina@cortechconsultinggroup.com")
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    return auth.read_session(token)


def _detail():
    return {
        "has_draft": True,
        "source_url": "https://example.org/tor",
        "evaluation_criteria": [{"criterion": "Methodology", "weight_percent": 100}],
    }


def test_upload_queues_the_file_for_the_worker_and_signoff_still_works(client, monkeypatch):
    queued = {}
    reviewed = {}

    def _queue(url, *, text, filename, uploaded_by, criteria, criteria_source="tor"):
        queued["url"] = url
        queued["text"] = text
        queued["filename"] = filename
        queued["by"] = uploaded_by
        queued["criteria"] = criteria
        return True, "queued"

    def _grade(*_args, **_kwargs):
        raise AssertionError("the web process must not call the model")

    def _record(url, stage):
        reviewed["url"] = url
        reviewed["stage"] = stage
        return True

    monkeypatch.setattr("dashboard.queries.opportunity_detail", lambda url: _detail())
    monkeypatch.setattr("database.supabase_client.queue_final_grade", _queue)
    monkeypatch.setattr("intelligence.final_grade.grade_final", _grade)
    monkeypatch.setattr("database.supabase_client.record_human_pipeline_stage", _record)
    monkeypatch.setattr(
        "dashboard.triggers.aggregate_cap_reached",
        lambda window=None: (False, "under the cap"),
    )
    session = _signed(client)
    body = (TEXT + " ") * 3
    response = client.post(
        "/opportunity/final-grade",
        data={"source_url": "https://example.org/tor", "csrf_token": auth.csrf_token(session)},
        files={"final_file": ("final.txt", body.encode(), "text/plain")},
    )
    assert response.status_code == 303
    assert queued["by"] == "amina@cortechconsultinggroup.com"
    assert queued["filename"] == "final.txt"
    assert queued["criteria"][0]["criterion"] == "Methodology"
    assert "Mogadishu" in queued["text"]
    assert "queued for grading" in unquote(response.headers["location"]).lower()
    assert "send" not in response.headers["location"].lower()

    again = client.post(
        "/opportunity/reviewed",
        data={"source_url": "https://example.org/tor", "csrf_token": auth.csrf_token(session)},
    )
    assert again.status_code == 303
    assert reviewed == {"url": "https://example.org/tor", "stage": "reviewed"}


def test_empty_criteria_and_a_blocked_window_do_not_queue_the_file(client, monkeypatch):
    calls = {"grade": 0}

    def _queue(*_args, **_kwargs):
        calls["grade"] += 1
        return True, "queued"

    monkeypatch.setattr("database.supabase_client.queue_final_grade", _queue)
    session = _signed(client)
    body = {"source_url": "https://example.org/tor", "csrf_token": auth.csrf_token(session)}
    files = {"final_file": ("final.txt", (TEXT + " ").encode(), "text/plain")}

    empty = dict(_detail())
    empty["evaluation_criteria"] = []
    monkeypatch.setattr("dashboard.queries.opportunity_detail", lambda url: empty)
    response = client.post("/opportunity/final-grade", data=body, files=files)
    assert response.status_code == 303
    assert "not graded" in unquote(response.headers["location"]).lower()

    monkeypatch.setattr("dashboard.queries.opportunity_detail", lambda url: _detail())
    monkeypatch.setattr(
        "dashboard.triggers.aggregate_cap_reached",
        lambda window=None: (True, "aggregate dashboard spend has reached the cap"),
    )
    blocked = client.post("/opportunity/final-grade", data=body, files=files)
    assert blocked.status_code == 303
    assert "cap" in blocked.headers["location"].lower()
    assert calls == {"grade": 0}


def test_a_failed_grade_stores_nothing(monkeypatch):
    from dashboard.worker import grade_one_pending_final

    saved = {"called": False}
    finished = {}

    def _grade(*_args, **_kwargs):
        raise RuntimeError("model unavailable")

    def _save(*_args, **_kwargs):
        saved["called"] = True
        return True, "saved"

    def _finish(url, request_id, error):
        finished["url"] = url
        finished["request_id"] = request_id
        finished["error"] = error
        return True

    monkeypatch.setattr(
        "database.supabase_client.claim_final_grade_request",
        lambda: {
            "source_url": "https://example.org/tor",
            "request_id": "r1",
            "text": TEXT,
            "filename": "final.txt",
            "uploaded_by": "amina@cortechconsultinggroup.com",
            "criteria": [{"criterion": "Methodology", "weight_percent": 100}],
        },
    )
    monkeypatch.setattr("intelligence.final_grade.grade_final", _grade)
    monkeypatch.setattr("database.supabase_client.save_final_grade", _save)
    monkeypatch.setattr("database.supabase_client.finish_final_grade_request", _finish)
    monkeypatch.setattr(
        "dashboard.triggers.aggregate_cap_reached",
        lambda window=None: (False, "under"),
    )
    monkeypatch.setattr("utils.observability.start_run_spend_cap", lambda *a, **k: {})
    monkeypatch.setattr("utils.observability.reset_run_spend_cap", lambda *a, **k: None)
    assert grade_one_pending_final() is True
    assert saved["called"] is False
    assert finished["request_id"] == "r1"
    assert "Nothing was stored" in finished["error"]


def test_the_worker_saves_a_grade_from_the_queued_file(monkeypatch):
    from dashboard.worker import grade_one_pending_final

    saved = {}

    def _grade(text, criteria, criteria_source="tor"):
        return settle_grade(text, criteria, {"rows": [{
            "criterion": "Methodology",
            "mark": 40,
            "reason": "Partial.",
            "quote": "a field team based in Mogadishu",
        }]})

    def _save(url, record, uploaded_by=""):
        saved["url"] = url
        saved["record"] = record
        saved["by"] = uploaded_by
        return True, "saved"

    monkeypatch.setattr(
        "database.supabase_client.claim_final_grade_request",
        lambda: {
            "source_url": "https://example.org/tor",
            "request_id": "r1",
            "text": (TEXT + " ") * 3,
            "filename": "final.txt",
            "uploaded_by": "amina@cortechconsultinggroup.com",
            "criteria": [{"criterion": "Methodology", "weight_percent": 100}],
        },
    )
    monkeypatch.setattr("intelligence.final_grade.grade_final", _grade)
    monkeypatch.setattr("database.supabase_client.save_final_grade", _save)
    monkeypatch.setattr(
        "database.supabase_client.finish_final_grade_request",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("failure path")),
    )
    monkeypatch.setattr(
        "dashboard.triggers.aggregate_cap_reached",
        lambda window=None: (False, "under"),
    )
    monkeypatch.setattr("utils.observability.start_run_spend_cap", lambda *a, **k: {})
    monkeypatch.setattr("utils.observability.reset_run_spend_cap", lambda *a, **k: None)
    assert grade_one_pending_final() is True
    assert saved["by"] == "amina@cortechconsultinggroup.com"
    assert saved["record"]["request_id"] == "r1"
    assert saved["record"]["rows"][0]["mark"] == 40
    assert saved["record"]["below_bar"] is True


def test_queue_final_grade_keeps_the_draft_and_stores_the_text(monkeypatch):
    from database.supabase_client import queue_final_grade

    store = {
        "calls": [],
        "row": {
            "state": "pending",
            "pipeline_stage": "drafted",
            "checkpoint": {"proposal_sections": {"methodology": "The agent draft."}},
        },
    }
    _patch_supabase(monkeypatch, store)
    monkeypatch.setattr("database.airtable_client.log_agent_action", lambda *_a, **_k: None)
    ok, reason = queue_final_grade(
        "https://example.org/tor",
        text=TEXT,
        filename="final.txt",
        uploaded_by="amina@cortechconsultinggroup.com",
        criteria=[{"criterion": "Methodology", "weight_percent": 100}],
    )
    assert (ok, reason) == (True, "queued")
    checkpoint = store["update"]["checkpoint"]
    assert checkpoint["proposal_sections"]["methodology"] == "The agent draft."
    assert checkpoint["final_grade_queued"] == "true"
    assert checkpoint["final_grade_request"]["text"] == TEXT
    assert checkpoint["final_grade_request"]["filename"] == "final.txt"
    assert "pipeline_stage" not in store["update"]
