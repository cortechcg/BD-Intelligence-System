"""Final-version grading end to end: queue, claim, grade, show.

2026-10-28 incident: "Grade this version" queued the file, then the worker
always failed. The reply budget was 2,000 tokens; seven criteria with quotes
ran past it, the JSON was cut off, and the worker reported only "The file
could not be graded". Tenders with no ToR criteria were refused outright.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import unquote

from dashboard import auth, queries
from intelligence import final_grade as fg
from intelligence.quality_gate import SOURCE_STANDARD, SOURCE_TOR
from tests.test_final_grade import TEXT, _signed, client  # noqa: F401  (fixture)

CRITERIA = [{"criterion": "Methodology", "weight_percent": 60}, {"criterion": "Team", "weight_percent": 40}]
QUOTE = "The sample covers four regions"


def _reply(rows, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps({"rows": rows}))])


def _good_rows():
    return [
        {"criterion": "Methodology", "mark": 50, "reason": "Clear sample.", "quote": QUOTE},
        {"criterion": "Team", "mark": 30, "reason": "Field team named.", "quote": "a field team based in Mogadishu"},
    ]


# ── The grader ─────────────────────────────────────────────────────────────

def test_the_reply_budget_fits_a_committee_reading(monkeypatch):
    budgets = []
    monkeypatch.setattr(fg, "complete", lambda *a, **k: budgets.append(k["max_tokens"]) or _reply(_good_rows()))
    grade = fg.grade_final(TEXT, CRITERIA)
    assert budgets == [fg._REPLY_TOKENS] and fg._REPLY_TOKENS >= 6000
    assert grade["percent"] == 80.0
    assert grade["bar_percent"] == 70
    assert grade["below_bar"] is False


def test_a_cut_off_reply_is_retried_with_a_larger_budget(monkeypatch):
    replies = [SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(type="text", text='{"rows":[{"criterion":"Meth')]),
               _reply(_good_rows())]
    budgets = []
    monkeypatch.setattr(fg, "complete", lambda *a, **k: budgets.append(k["max_tokens"]) or replies.pop(0))
    grade = fg.grade_final(TEXT, CRITERIA)
    assert budgets == [fg._REPLY_TOKENS, fg._RETRY_TOKENS]
    assert grade["percent"] == 80.0


def test_a_reply_cut_off_twice_fails_with_the_reason(monkeypatch):
    cut = SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(type="text", text='{"rows":[')])
    monkeypatch.setattr(fg, "complete", lambda *a, **k: cut)
    try:
        fg.grade_final(TEXT, CRITERIA)
    except fg.GradeReplyError as exc:
        assert "cut off" in str(exc)
    else:
        raise AssertionError("expected GradeReplyError")


def test_inferred_criteria_are_named_in_the_headline(monkeypatch):
    monkeypatch.setattr(fg, "complete", lambda *a, **k: _reply([
        {"criterion": "Technical approach and methodology (inferred)", "mark": 50, "reason": "ok", "quote": QUOTE},
    ]))
    grade = fg.grade_final(TEXT, [{"criterion": "Technical approach and methodology (inferred)"}], SOURCE_STANDARD)
    assert grade["criteria_source"] == SOURCE_STANDARD
    assert "INFERRED" in grade["headline"]


# ── The worker reports the real reason ─────────────────────────────────────

def test_the_worker_reports_why_a_grade_failed(monkeypatch):
    from dashboard.worker import grade_one_pending_final

    finished = {}
    monkeypatch.setattr("database.supabase_client.claim_final_grade_request", lambda: {
        "source_url": "https://example.org/tor", "request_id": "r1", "text": TEXT, "filename": "final.docx",
        "uploaded_by": "amina@cortechconsultinggroup.com", "criteria": CRITERIA, "criteria_source": SOURCE_TOR,
    })
    monkeypatch.setattr("dashboard.triggers.aggregate_cap_reached", lambda window=None: (False, ""))

    def _grade(*_a, **_k):
        raise fg.GradeReplyError("the committee reply was cut off at 12000 tokens")

    monkeypatch.setattr("intelligence.final_grade.grade_final", _grade)
    monkeypatch.setattr("database.supabase_client.finish_final_grade_request",
                        lambda url, rid, error: finished.update(error=error) or True)
    assert grade_one_pending_final() is True
    assert "cut off at 12000 tokens" in finished["error"]
    assert "Grade it again" in finished["error"]


# ── A tender without ToR criteria is still graded ──────────────────────────

def test_grading_criteria_fall_back_instead_of_refusing():
    got = queries._grading_criteria({"evaluation_criteria": []}, {"tender_brief": ""})
    assert got["grading_criteria_source"] == SOURCE_STANDARD
    assert got["grading_criteria"] and all(c["criterion"].endswith("(inferred)") for c in got["grading_criteria"])
    tor = queries._grading_criteria({"evaluation_criteria": CRITERIA}, {})
    assert tor["grading_criteria_source"] == SOURCE_TOR


def test_route_queues_a_tender_with_no_tor_criteria(client, monkeypatch):  # noqa: F811
    queued = {}
    fallback = queries._grading_criteria({}, {})
    monkeypatch.setattr("dashboard.queries.opportunity_detail", lambda url: {
        "has_draft": True, "source_url": "https://example.org/tor", "evaluation_criteria": [], **fallback,
    })
    monkeypatch.setattr("dashboard.triggers.aggregate_cap_reached", lambda window=None: (False, ""))
    monkeypatch.setattr("database.supabase_client.queue_final_grade", lambda url, **kw: queued.update(kw) or (True, "queued"))
    session = _signed(client)
    response = client.post(
        "/opportunity/final-grade",
        data={"source_url": "https://example.org/tor", "csrf_token": auth.csrf_token(session)},
        files={"final_file": ("final.txt", ((TEXT + " ") * 4).encode(), "text/plain")},
    )
    assert response.status_code == 303
    assert "queued for grading" in unquote(response.headers["location"])
    assert queued["criteria_source"] == SOURCE_STANDARD
    assert len(queued["criteria"]) == len(fallback["grading_criteria"])


# ── Progress and failure are visible ───────────────────────────────────────

def test_pending_state_separates_queued_from_reading():
    queued = queries._final_grade_pending({"final_grade_queued": "true", "final_grade_request": {
        "text": "x", "filename": "f.docx", "uploaded_at": "2026-10-28T15:00:00+00:00", "criteria": CRITERIA}})
    assert queued["claimed_at"] == "" and queued["criteria_count"] == 2
    reading = queries._final_grade_pending({"final_grade_queued": "true", "final_grade_request": {
        "text": "x", "filename": "f.docx", "claimed_at": "2026-10-28T15:00:05+00:00", "criteria": CRITERIA}})
    assert reading["claimed_at"].startswith("2026-10-28T15:00:05")


def _render(detail):
    """Render only the review-pack macro (the grading panel) from detail.html."""
    from pathlib import Path

    from dashboard.app import templates

    source = Path("dashboard/templates/detail.html").read_text()
    start = source.index("{% macro review_pack() %}")
    end = source.index("{% endmacro %}", start) + len("{% endmacro %}")
    tmpl = templates.env.from_string(
        '{% import "_macros.html" as m %}' + source[start:end] + "{{ review_pack() }}"
    )
    return tmpl.render(d=detail, csrf_token="t", url_final_grade="/opportunity/final-grade", url_draft="/draft")


def test_the_panel_shows_each_step_and_a_failure():
    field = {"value": None, "why": "", "label": ""}
    base = {
        "source_url": "u", "section_keys": [], "grading_criteria": CRITERIA, "grading_criteria_note": "",
        "has_draft": True, "title": field, "client": field, "donor": field, "deadline": field,
        "recommendation": field, "fit": field, "why": field, "win_calibrated": field, "budget_usd": field,
        "submission_type": field, "client_draft_sections": [], "committee_grade": {}, "quality_gate": {},
        "requirement_alignment": {}, "matched_team_result": {},
    }
    step1 = str(_render({**base, "final_grade_pending": {"filename": "f.docx", "uploaded_at": "2026-10-28T15:00:00", "claimed_at": "", "criteria_count": 2}}))
    assert "Step 1 of 2" in step1 and "Queued" in step1
    step2 = str(_render({**base, "final_grade_pending": {"filename": "f.docx", "claimed_at": "2026-10-28T15:00:05", "criteria_count": 2}}))
    assert "Step 2 of 2" in step2 and "committee is reading f.docx" in step2
    failed = str(_render({**base, "final_grade_error": "The file could not be graded: the committee reply was cut off."}))
    assert "Grading failed." in failed and "cut off" in failed
