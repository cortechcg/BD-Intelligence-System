"""Library uploads through the real /library/upload route (fake Supabase).

2026-10-28 incident: every proposal upload failed with "submission_date must be
YYYY-MM-DD" although the field showed 2026-10-29. The text field carried an
invisible direction mark or a non-breaking hyphen (pasted or autocorrected),
and the server accepted only strict ASCII. A retry after the file was edited
then failed in the browser (ERR_UPLOAD_FILE_CHANGED) and read as a connection
problem. These tests pin the server side; the browser side was verified live.
"""
from __future__ import annotations

import uuid

import pytest

from dashboard import app as app_module
from dashboard import auth, library
from intelligence.library_ingest import content_hash
from tests.test_library import _DB, _docx, _session, client  # noqa: F401  (fixture)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def db(monkeypatch):
    fake = _DB()
    monkeypatch.setattr(app_module, "_library_client", lambda: fake)
    monkeypatch.setattr(library.triggers, "enqueue", lambda *a, **k: "job-1")
    monkeypatch.setattr(library.settings, "TRIGGERS_ENABLED", True)
    return fake


def _file(name: str, body: bytes | None = None):
    return ("files", (name, body or _docx(f"<w:t>{uuid.uuid4().hex}</w:t>".encode()), DOCX_MIME))


def _post(client, session, data, files):  # noqa: F811
    payload = {"csrf_token": auth.csrf_token(session), **data}
    return client.post("/library/upload", data=payload, files=files, headers={"Accept": "application/json"})


def _stored(db, table="library_documents"):
    return [row for name, row in db.inserts if name == table]


# ── Proposals ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("typed, stored", [
    ("2026-10-29", "2026-10-29"),            # what the date picker sends
    ("", None),                              # optional, left empty
    ("‎2026-10-29", "2026-10-29"),      # pasted, invisible LTR mark
    ("2026‑10‑29", "2026-10-29"),  # autocorrected non-breaking hyphens
    ("29/10/2026", "2026-10-29"),            # typed by hand, day first
    ("29 October 2026", "2026-10-29"),
    (" 2026-10-29 ", "2026-10-29"),
])
def test_proposal_upload_stores_the_date_however_it_was_entered(client, db, typed, stored):  # noqa: F811
    session = _session(client)
    response = _post(client, session, {"kind": "proposal", "title": "Water baseline", "submission_date": typed}, [_file("p.docx")])
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True, body
    row = _stored(db)[0]
    assert row["submission_date"] == stored
    assert row["title"] == "Water baseline"
    assert row["status"] == "queued"
    assert db.uploads and db.uploads[0][0].startswith("proposals/")


def test_a_real_non_date_gets_a_precise_reason_not_a_connection_error(client, db):  # noqa: F811
    session = _session(client)
    body = _post(client, session, {"kind": "proposal", "submission_date": "31/02/2026"}, [_file("p.docx")]).json()
    assert body["ok"] is False
    reason = body["results"][0]["reason"]
    assert "'31/02/2026' is not a date" in reason
    assert "YYYY-MM-DD" in reason
    assert _stored(db) == [] and db.uploads == []


def test_a_duplicate_is_skipped_and_the_rest_of_the_batch_is_added(client, db):  # noqa: F811
    session = _session(client)
    existing = _docx(b"<w:t>already stored proposal</w:t>")
    db.documents.append({"id": "doc-old", "kind": "proposal", "content_hash": content_hash(existing), "title": "Old"})
    body = _post(client, session, {"kind": "proposal"}, [_file("old.docx", existing), _file("new.docx")]).json()
    assert body["ok"] is True
    old, new = body["results"]
    assert old["duplicate"] is True and old["existing_id"] == "doc-old"
    assert new["ok"] is True and new.get("document_id")
    assert [row["original_filename"] for row in _stored(db)] == ["new.docx"]


def test_metadata_is_stored_with_the_file(client, db):  # noqa: F811
    session = _session(client)
    _post(client, session, {
        "kind": "proposal", "title": "KAP survey", "client": "UNICEF", "donor": "ECHO",
        "country": "Somalia", "sectors": "WASH, Child protection", "submission_date": "2026-10-29",
        "proposal_type": "technical", "outcome": "won",
    }, [_file("kap.docx")])
    row = _stored(db)[0]
    assert (row["client"], row["donor"], row["country"]) == ("UNICEF", "ECHO", "Somalia")
    assert row["sectors"] == ["WASH", "Child protection"]
    assert (row["proposal_type"], row["outcome"]) == ("technical", "won")
    assert row["uploaded_by_email"] == "ada@cortechcgl.com"


# ── CVs ────────────────────────────────────────────────────────────────────

def test_cv_upload_for_me_links_the_session_consultant(client, db):  # noqa: F811
    session = _session(client)
    db.consultants.append({"id": "11111111-1111-4111-8111-111111111111", "email": "ada@cortechcgl.com", "full_name": "Ada"})
    body = _post(client, session, {"kind": "cv", "whose": "me"}, [_file("ada-cv.docx")]).json()
    assert body["ok"] is True, body
    row = _stored(db)[0]
    assert row["kind"] == "cv"
    assert row["consultant_id"] == "11111111-1111-4111-8111-111111111111"
    assert db.uploads[0][0].startswith("cvs/")


def test_cv_upload_for_someone_on_the_team_by_list_id(client, db):  # noqa: F811
    session = _session(client)
    cid = "22222222-2222-4222-8222-222222222222"
    db.consultants.append({"id": cid, "full_name": "Amina Hassan"})
    body = _post(client, session, {"kind": "cv", "whose": "existing", "consultant_id": cid}, [_file("amina.docx")]).json()
    assert body["ok"] is True, body
    assert _stored(db)[0]["consultant_id"] == cid


def test_cv_upload_with_a_typed_name_instead_of_a_list_pick_says_so(client, db):  # noqa: F811
    session = _session(client)
    response = _post(client, session, {"kind": "cv", "whose": "existing", "consultant_id": "Amina Hassan"}, [_file("amina.docx")])
    assert response.status_code == 400
    assert "'Amina Hassan' is not on the team list" in response.json()["reason"]
    assert _stored(db) == [] and db.uploads == []


def test_cv_upload_for_a_new_team_member_creates_the_consultant(client, db):  # noqa: F811
    session = _session(client)
    body = _post(client, session, {
        "kind": "cv", "whose": "new", "new_full_name": "Hodan Ali", "new_email": "hodan@cortechcgl.com",
        "new_role": "Statistician", "new_years": "7",
    }, [_file("hodan.docx")]).json()
    assert body["ok"] is True, body
    consultant = _stored(db, "consultants")[0]
    assert consultant["full_name"] == "Hodan Ali"
    assert "day_rate_usd" not in consultant and "availability_status" not in consultant
    assert _stored(db)[0]["kind"] == "cv"


def test_cv_upload_without_choosing_whose_is_refused_precisely(client, db):  # noqa: F811
    session = _session(client)
    response = _post(client, session, {"kind": "cv"}, [_file("cv.docx")])
    assert response.status_code == 400
    assert "Choose whose CV this is" in response.json()["reason"]


def test_the_form_uses_a_date_picker(client, db):  # noqa: F811
    _session(client)
    db.documents = []
    html = client.get("/library").text
    assert 'name="submission_date" type="date"' in html
