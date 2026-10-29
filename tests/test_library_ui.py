"""Knowledge Library page: the add panel, the outcome control, and what must not change.

The backend is patched in memory. Nothing here talks to Supabase.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dashboard import app as app_module
from dashboard import auth, library as library_mod, settings

ROOT = Path(__file__).resolve().parents[1]

PROPOSAL = {
    "id": "d1", "kind": "proposal", "title": "WASH baseline, Kenya", "client": "UNICEF", "donor": "UNICEF",
    "country": "Kenya", "outcome": "won", "status": "ready", "proposal_type": "technical",
    "submission_date": "2024-03-18", "uploaded_by": "ada@cortechcgl.com", "uploader_name": "Ada",
    "original_filename": "wash.pdf", "created_at": "2026-10-01T09:00:00+00:00",
}
UNMARKED = dict(PROPOSAL, id="d2", title="Livelihoods study", outcome="unknown")
CV = {
    "id": "c1", "kind": "cv", "title": "Amina Njoroge", "status": "ready", "consultant_id": "k1",
    "uploaded_by": "ada@cortechcgl.com", "original_filename": "amina.pdf", "created_at": "2026-10-01T09:00:00+00:00",
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "library-ui-secret")
    monkeypatch.setattr(settings, "AUTH_ALLOWED_DOMAIN", "cortechcgl.com")
    c = TestClient(app_module.app, follow_redirects=False)
    c.cookies.set(settings.SESSION_COOKIE_NAME, auth.issue_session("ada@cortechcgl.com", "Ada"))
    return c


def _patch(monkeypatch, rows):
    monkeypatch.setattr(app_module, "_library_client", lambda: object())
    monkeypatch.setattr(library_mod, "list_documents", lambda _s, tab="proposals", filters=None, limit=200: [
        r for r in rows if (r["kind"] == "cv") == (tab == "cvs")
    ] if tab in ("proposals", "cvs") else list(rows) if tab in ("mine", "all") else [])
    monkeypatch.setattr(library_mod, "census_rows", lambda _s: list(rows))
    monkeypatch.setattr(library_mod, "counts", lambda _r: {
        "proposals_ready": 2, "proposals_processing": 0, "cv_consultants": 1, "cvs_ready": 1,
    })


def test_page_has_the_add_button_and_the_panel_with_unchanged_field_names(client, monkeypatch):
    _patch(monkeypatch, [PROPOSAL, UNMARKED])
    html = client.get("/library").text
    assert "Add a CV or past proposal" in html
    assert 'data-lib-panel' in html and 'action="/library/upload"' in html
    assert 'enctype="multipart/form-data"' in html
    for name in ("csrf_token", "kind", "files", "title", "client", "donor", "country", "sectors",
                 "submission_date", "proposal_type", "outcome", "whose", "consultant_id"):
        assert f'name="{name}"' in html, name
    assert 'accept=".pdf,.docx"' in html
    assert "25 MiB" in html


def test_core_content_is_in_the_html_without_script(client, monkeypatch):
    _patch(monkeypatch, [PROPOSAL, UNMARKED])
    html = client.get("/library").text
    assert "WASH baseline, Kenya" in html and "Download original" in html
    assert "<noscript" not in html or "Add a CV" in html
    # the panel is hidden only when the script marks the page as scripted
    css = (ROOT / "dashboard/static/library.css").read_text()
    assert ".js .lib-panel:not(.is-open)" in css
    assert not re.search(r"(?m)^\.lib-panel\s*\{[^}]*display:\s*none", css)


def test_outcome_control_posts_to_the_existing_update_endpoint(client, monkeypatch):
    _patch(monkeypatch, [PROPOSAL, UNMARKED])
    html = client.get("/library").text
    assert 'action="/library/d1/update"' in html and 'action="/library/d2/update"' in html
    form = html.split('action="/library/d2/update"', 1)[1].split("</form>", 1)[0]
    for name in ("csrf_token", "title", "client", "donor", "country"):
        assert f'name="{name}"' in form
    assert form.count('name="outcome"') == 3
    for value in ("won", "lost", "pending"):
        assert f'value="{value}"' in form
    assert "Not marked" in form
    won = html.split('action="/library/d1/update"', 1)[1].split("</form>", 1)[0]
    assert "disabled" in won and 'aria-pressed="true"' in won


def test_cv_rows_have_no_outcome_control(client, monkeypatch):
    _patch(monkeypatch, [CV])
    html = client.get("/library?tab=cvs").text
    assert "Amina Njoroge" in html
    assert "/library/c1/update" not in html


def test_kpi_counts_cvs_on_the_proposals_tab(client, monkeypatch):
    monkeypatch.setattr(app_module, "_library_client", lambda: object())
    rows = [PROPOSAL, UNMARKED, CV]

    def fake_list(_s, tab="proposals", filters=None, limit=200):
        if tab == "cvs":
            return [CV]
        if tab == "proposals":
            return [PROPOSAL, UNMARKED]
        return []

    monkeypatch.setattr(library_mod, "list_documents", fake_list)
    monkeypatch.setattr(library_mod, "census_rows", lambda _s: rows)
    html = client.get("/library").text
    assert "WASH baseline, Kenya" in html
    assert "Amina Njoroge" not in html
    cvs = html.split('figure__label">CVs</div>', 1)[1].split("figure__sub", 1)[0]
    assert ">1</div>" in cvs
    assert "consultants · 1 ready" in html


def test_empty_and_unavailable_states(client, monkeypatch):
    _patch(monkeypatch, [])
    html = client.get("/library").text
    assert "No past proposals yet" in html
    assert "Add a CV or past proposal" in html

    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(app_module, "_library_client", boom)
    html = client.get("/library").text
    assert "not available yet" in html


def test_anonymous_requests_are_redirected_and_uploads_refused(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "library-ui-secret")
    anon = TestClient(app_module.app, follow_redirects=False)
    r = anon.get("/library")
    assert r.status_code == 302 and "/auth/login" in r.headers["location"]
    assert anon.post("/library/upload").status_code in (302, 401, 403)
    assert anon.post("/library/d1/update").status_code in (302, 401, 403)


def test_upload_without_csrf_is_refused_as_json(client):
    r = client.post("/library/upload", headers={"Accept": "application/json"}, data={"kind": "cv"})
    assert r.status_code in (400, 403)


def test_scripts_and_styles_are_local_and_the_page_names_them(client, monkeypatch):
    _patch(monkeypatch, [PROPOSAL])
    html = client.get("/library").text
    assert "library.css" in html and "library.js" in html
    assert not re.search(r'(?:src|href)="https?://', html)
    js = (ROOT / "dashboard/static/library.js").read_text()
    assert "/library/upload" in js or "action" in js
    assert "http://" not in js and "https://" not in js
