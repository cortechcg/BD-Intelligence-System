"""Knowledge Library validation, identity, and worker ingest. No live Supabase."""
from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from dashboard import app as app_module
from dashboard import auth, library, settings
from intelligence.library_ingest import content_hash, detect_type, embedding_model, extract_text

PDF = b"%PDF-1.4\n" + (b"A method paragraph. " * 20)


def _docx(payload: bytes = b"<w:t>A method paragraph for the team.</w:t>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", payload)
    return buf.getvalue()


class _Result:
    def __init__(self, data):
        self.data = data


class _Table:
    def __init__(self, db, name):
        self.db = db
        self.name = name
        self.op = "select"
        self.payload = None
        self.filters = []

    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def is_(self, key, value):
        self.filters.append((key, value))
        return self

    def in_(self, key, value):
        self.filters.append((key, value))
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self.op == "insert":
            self.db.inserts.append((self.name, self.payload))
            row = dict(self.payload)
            row.setdefault("id", "row-1")
            if self.name == "consultants":
                self.db.consultants.append(row)
            return _Result([row])
        if self.op == "update":
            self.db.updates.append((self.name, self.payload, list(self.filters)))
            return _Result([self.payload])
        if self.op == "delete":
            self.db.deletes.append((self.name, list(self.filters)))
            return _Result([])
        if self.name == "library_documents":
            rows = list(self.db.documents)
        elif self.name == "consultants":
            rows = list(self.db.consultants)
        elif self.name == "cv_embeddings":
            rows = list(self.db.cv_rows)
        else:
            rows = []
        for key, value in self.filters:
            if key == "archived_at" and value == "null":
                rows = [row for row in rows if not row.get("archived_at")]
            else:
                rows = [row for row in rows if row.get(key) == value]
        return _Result(rows)


class _DB:
    def __init__(self):
        self.inserts = []
        self.updates = []
        self.deletes = []
        self.uploads = []
        self.documents = []
        self.consultants = []
        self.cv_rows = []
        self.downloaded = b""

    def table(self, name):
        return _Table(self, name)

    @property
    def storage(self):
        return self

    def from_(self, _bucket):
        return self

    def upload(self, path, data, _options):
        self.uploads.append((path, bytes(data)))

    def download(self, _path):
        return self.downloaded


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "library-test-secret")
    monkeypatch.setattr(settings, "AUTH_ALLOWED_DOMAIN", "cortechcgl.com")
    return TestClient(app_module.app, follow_redirects=False)


def _session(client, email="ada@cortechcgl.com", name="Ada"):
    token = auth.issue_session(email, name)
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    session = auth.read_session(token)
    return session


def test_detect_type_requires_extension_and_magic_to_agree():
    assert detect_type("note.pdf", PDF) == "pdf"
    assert detect_type("note.docx", PDF) is None
    assert detect_type("note.pdf", _docx()) is None
    assert detect_type("note.docx", _docx()) == "docx"
    assert detect_type("note.pdf", b"") is None


def test_empty_oversize_and_duplicate_files(monkeypatch):
    monkeypatch.setattr(library, "MAX_DOCUMENT_BYTES", 32)
    db = _DB()
    session = {"email": "ada@cortechcgl.com", "name": "Ada"}
    empty = library.store_upload(db, session, kind="proposal", filename="a.pdf", data=b"", meta={})
    assert empty["ok"] is False
    assert "empty" in empty["reason"].lower()
    big = library.store_upload(db, session, kind="proposal", filename="a.pdf", data=PDF, meta={})
    assert big["ok"] is False
    assert "25" in big["reason"]
    monkeypatch.setattr(library, "MAX_DOCUMENT_BYTES", 25 * 1024 * 1024)
    monkeypatch.setattr(library.triggers, "enqueue", lambda *a, **k: "job-1")
    first = library.store_upload(db, session, kind="proposal", filename="a.pdf", data=PDF, meta={"title": "Water"})
    assert first["ok"] is True
    stored = next(row for name, row in db.inserts if name == "library_documents")
    db.documents.append(stored)
    again = library.store_upload(db, session, kind="proposal", filename="a.pdf", data=PDF, meta={})
    assert again["duplicate"] is True
    assert again["existing_id"] == stored["id"]
    assert again["reason"] == "Already in the library"


def test_uploaded_by_comes_from_the_session_not_the_form(monkeypatch):
    monkeypatch.setattr(library.triggers, "enqueue", lambda *a, **k: "job-1")
    db = _DB()
    library.store_upload(
        db,
        {"email": "ada@cortechcgl.com", "name": "Ada"},
        kind="proposal",
        filename="a.pdf",
        data=PDF,
        meta={"uploaded_by_email": "forged@example.com", "title": "Water"},
    )
    row = next(payload for name, payload in db.inserts if name == "library_documents")
    assert row["uploaded_by_email"] == "ada@cortechcgl.com"
    assert row["uploaded_by_name"] == "Ada"
    assert "forged@example.com" not in row.values()


def test_new_consultant_does_not_write_rate_or_availability():
    db = _DB()
    created = library.create_consultant(
        db,
        {
            "full_name": "Amina Hassan",
            "email": "amina@cortechcgl.com",
            "role_title": "WASH lead",
            "day_rate_usd": 900,
            "availability_status": "free",
            "availability_percentage": 100,
        },
        "ada@cortechcgl.com",
        confirm=True,
    )
    row = created["consultant"]
    assert row["name_key"] == "amina hassan"
    assert "day_rate_usd" not in row
    assert "availability_status" not in row
    assert "availability_percentage" not in row
    again = library.create_consultant(
        db, {"full_name": "Amina Hassan"}, "ada@cortechcgl.com", confirm=True
    )
    assert again["created"] is False
    assert again["consultant"]["id"] == row["id"]


def test_me_resolves_by_email():
    db = _DB()
    db.consultants.append({
        "id": "c-1",
        "email": "ada@cortechcgl.com",
        "full_name": "Ada",
        "name_key": "ada",
        "day_rate_usd": None,
        "availability_status": None,
    })
    found = library.resolve_me(db, {"email": "ada@cortechcgl.com", "name": "Ada"})
    assert found["id"] == "c-1"
    assert found["day_rate_usd"] is None
    assert found["availability_status"] is None


def test_anonymous_library_routes_are_gated(client):
    assert client.get("/library").status_code == 302
    assert client.get("/api/library").status_code == 401
    assert client.get("/api/consultants").status_code == 401


def test_upload_requires_csrf_and_reports_a_partial_batch(client, monkeypatch):
    session = _session(client)
    monkeypatch.setattr(app_module, "_library_client", lambda: _DB())
    refused = client.post(
        "/library/upload",
        data={"kind": "proposal", "csrf_token": "nope"},
        files={"files": ("a.pdf", PDF, "application/pdf")},
    )
    assert refused.status_code == 403

    stored = {}

    def _fake_store(_db, sess, **kwargs):
        stored["email"] = sess["email"]
        if kwargs["filename"].endswith(".txt"):
            return {"ok": False, "filename": kwargs["filename"], "reason": "The file extension does not match its contents"}
        return {"ok": True, "filename": kwargs["filename"], "document_id": "doc-1"}

    monkeypatch.setattr(library, "store_upload", _fake_store)
    response = client.post(
        "/library/upload",
        data={"kind": "proposal", "csrf_token": auth.csrf_token(session), "uploaded_by_email": "forged@example.com"},
        files=[
            ("files", ("a.pdf", PDF, "application/pdf")),
            ("files", ("b.txt", b"hello", "text/plain")),
        ],
        headers={"accept": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert stored["email"] == "ada@cortechcgl.com"
    assert any(row.get("ok") for row in body["results"])
    assert any(not row.get("ok") for row in body["results"])


def test_archive_is_owner_or_admin_only(monkeypatch):
    db = _DB()
    db.documents.append({
        "id": "doc-1",
        "kind": "proposal",
        "uploaded_by_email": "ada@cortechcgl.com",
        "embedding_ref": "library:doc-1",
        "archived_at": None,
        "consultant_id": None,
    })
    ok, _message = library.archive_document(db, {"email": "bey@cortechcgl.com", "name": "Bey"}, "doc-1")
    assert ok is False
    monkeypatch.setenv("LIBRARY_ADMIN_EMAILS", "bey@cortechcgl.com")
    ok, message = library.archive_document(db, {"email": "bey@cortechcgl.com", "name": "Bey"}, "doc-1")
    assert ok is True
    assert "matching" in message.lower()
    assert db.deletes
    assert db.deletes[0][0] == "proposal_embeddings"


def test_worker_proposal_happy_path_and_failures(monkeypatch):
    db = _DB()
    db.downloaded = PDF
    db.documents.append({
        "id": "doc-1",
        "kind": "proposal",
        "title": "Water",
        "original_filename": "water.pdf",
        "storage_path": "proposals/doc-1-water.pdf",
        "outcome": "won",
        "uploaded_by_email": "ada@cortechcgl.com",
        "client": "County",
        "donor": "UNICEF",
        "country": "Kenya",
        "sectors": ["WASH"],
        "proposal_type": "technical",
        "submission_date": "2026-01-15",
        "archived_at": None,
    })
    monkeypatch.setattr("database.supabase_client.get_supabase", lambda: db)
    monkeypatch.setattr(library.triggers, "aggregate_cap_reached", lambda window=None: (False, ""))
    monkeypatch.setattr(
        "intelligence.library_ingest.ingest_proposal",
        lambda supabase, doc, text: "library:doc-1",
    )
    monkeypatch.setattr(
        "intelligence.library_ingest.extract_text",
        lambda kind, filename, data: ("A real paragraph. " * 20, {"ok": True, "chars": 360}),
    )
    outcome = library.run_library_ingest({"source_url": "library://doc-1", "trigger_kind": "library_ingest"})
    assert outcome["status"] == "succeeded"
    statuses = [payload["status"] for name, payload, _filters in db.updates if name == "library_documents" and "status" in payload]
    assert statuses[:2] == ["extracting", "embedding"]
    assert statuses[-1] == "ready"
    ready = next(payload for name, payload, _filters in db.updates if payload.get("status") == "ready")
    assert ready["chunk_count"] == 1
    assert ready["embedding_ref"] == "library:doc-1"

    db.updates.clear()
    monkeypatch.setattr(
        "intelligence.library_ingest.extract_text",
        lambda kind, filename, data: ("", {"ok": False, "reason": "Scanned PDF with no text layer", "chars": 0}),
    )
    failed = library.run_library_ingest({"source_url": "library://doc-1"})
    assert failed["status"] == "failed"
    assert "no text layer" in failed["error_message"]

    db.updates.clear()
    monkeypatch.setattr(library.triggers, "aggregate_cap_reached", lambda window=None: (True, "cap"))
    capped = library.run_library_ingest({"source_url": "library://doc-1"})
    assert capped["status"] == "failed"
    assert "retry later" in capped["error_message"]


def test_extract_password_and_empty_layer(monkeypatch):
    monkeypatch.setattr(
        "intelligence.library_ingest.extract_text_from_pdf",
        lambda data: (_ for _ in ()).throw(RuntimeError("File is password protected")),
    )
    text, meta = extract_text("proposal", "secret.pdf", PDF)
    assert text == ""
    assert meta["reason"] == "File is password-protected"
    monkeypatch.setattr("intelligence.library_ingest.extract_text_from_pdf", lambda data: "")
    text, meta = extract_text("proposal", "scan.pdf", PDF)
    assert meta["ok"] is False
    assert "no text layer" in meta["reason"]


def test_embedding_model_is_the_shared_constant():
    from embed_cvs import EMBEDDING_MODEL

    source = open("intelligence/library_ingest.py", encoding="utf-8").read()
    assert "text-embedding-3-small" not in source
    assert embedding_model() == EMBEDDING_MODEL


def test_proposal_callers_store_the_same_payload(monkeypatch):
    captured = []

    def _store(**kwargs):
        captured.append(kwargs["payload"])
        return ("emb-1", "inserted", 0)

    monkeypatch.setattr("embed_cvs.store_single_embedding_row", _store)
    monkeypatch.setattr("embed_cvs.get_embedding_openai", lambda text: [0.1, 0.2])
    monkeypatch.setattr("embed_cvs.retry_on_transient", lambda fn, label="": fn())
    from intelligence.library_ingest import embed_proposal_text

    meta = {"client": "County", "uploaded_by_email": "ada@cortechcgl.com", "library_id": "doc-1"}
    text = "paragraph " * 30
    first = embed_proposal_text(object(), lookup="library:doc-1", title="Water", text=text, won=True, metadata=meta)
    second = embed_proposal_text(object(), lookup="library:doc-1", title="Water", text=text, won=True, metadata=meta)
    assert first == second == "emb-1"
    assert captured[0] == captured[1]
    assert captured[0]["won"] is True
    assert captured[0]["airtable_proposal_id"] == "library:doc-1"
    assert captured[0]["metadata"]["uploaded_by_email"] == "ada@cortechcgl.com"


def test_triggers_paused_refuses_the_upload(monkeypatch):
    monkeypatch.setattr(settings, "TRIGGERS_ENABLED", False)
    result = library.validate_file("a.pdf", PDF)
    assert result["ok"] is True
    refused = library.store_upload(
        _DB(),
        {"email": "ada@cortechcgl.com", "name": "Ada"},
        kind="proposal",
        filename="a.pdf",
        data=PDF,
        meta={"title": "Water"},
    )
    assert refused["ok"] is False
    assert "paused" in refused["reason"].lower()


def test_library_page_renders_empty(env=None):
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    jinja = Environment(loader=FileSystemLoader("dashboard/templates"), autoescape=select_autoescape(["html"]))
    html = jinja.get_template("library.html").render(
        view="library",
        url_queue="/",
        url_portfolio="/portfolio",
        url_library="/library",
        csrf_token="t",
        viewer="ada@cortechcgl.com",
        flash="",
        flash_kind="",
        available=True,
        unavailable_reason="",
        tab="proposals",
        rows=[],
        counts={"proposals_ready": 0, "proposals_processing": 0, "cv_consultants": 0, "cvs_ready": 0},
        library_admin=False,
        product_name="Cortech BD",
        static_base="/static",
        static_v="1",
        poll_ms=0,
        sidebar_opps=[],
    )
    assert "Knowledge Library" in html
    assert "No past proposals yet" in html
    assert "Rates and availability are never inferred" in html
    assert "forged@" not in html
