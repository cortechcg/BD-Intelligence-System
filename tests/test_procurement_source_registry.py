"""Durable-source repository and mutation security tests."""
from __future__ import annotations

from copy import deepcopy
import inspect
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from fastapi import HTTPException

from dashboard import app as app_module
from dashboard import auth, settings
from database import procurement_sources as registry


class _MemoryRegistry:
    """Tiny PostgREST-shaped fixture shared across repository instances."""
    def __init__(self):
        self.rows: list[dict] = []

    def table(self, _name):
        return _MemoryQuery(self)


class _MemoryQuery:
    def __init__(self, backend):
        self.backend = backend
        self.filters = []
        self.mode = "select"
        self.payload = None
        self.columns = "*"
        self.limit_n = None

    def select(self, columns="*"):
        self.mode, self.columns = "select", columns
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def limit(self, value):
        self.limit_n = value
        return self

    def insert(self, payload):
        self.mode, self.payload = "insert", deepcopy(payload)
        return self

    def execute(self):
        if self.mode == "insert":
            if any(row["url"] == self.payload["url"] for row in self.backend.rows):
                raise RuntimeError("duplicate url")
            row = {"id": f"source-{len(self.backend.rows) + 1}", **self.payload}
            self.backend.rows.append(row)
            return SimpleNamespace(data=[deepcopy(row)])
        rows = self.backend.rows
        for column, value in self.filters:
            rows = [row for row in rows if row.get(column) == value]
        rows = rows[:self.limit_n] if self.limit_n else rows
        if self.columns != "*":
            names = [name.strip() for name in self.columns.split(",")]
            rows = [{name: row.get(name) for name in names} for row in rows]
        return SimpleNamespace(data=deepcopy(rows))


def test_custom_source_is_persisted_disabled_and_survives_repository_restart(monkeypatch):
    backend = _MemoryRegistry()
    monkeypatch.setattr(registry, "get_supabase", lambda: backend)
    created = registry.create_custom_source(
        name="County procurement",
        url="county.example.org/feed.xml",
        capability="rss_atom",
        probe_success=True,
        preview=[{"title": "Somalia evaluation", "url": "https://county.example.org/n/17"}],
        created_by="staff@cortechconsultinggroup.com",
    )
    assert created["enabled"] is False
    assert created["capability"] == "rss_atom"

    # A restarted process gets a new repository call but reads the same
    # durable backend record, as it would from Supabase.
    reloaded = registry.list_sources(include_builtins=False)
    assert len(reloaded) == 1
    assert reloaded[0]["url"] == "https://county.example.org/feed.xml"
    assert reloaded[0]["last_success_at"] is not None
    assert reloaded[0]["last_preview"][0]["title"] == "Somalia evaluation"


def test_failed_source_persists_attempt_and_actionable_error_without_success(monkeypatch):
    backend = _MemoryRegistry()
    monkeypatch.setattr(registry, "get_supabase", lambda: backend)
    created = registry.create_custom_source(
        name="Restricted portal",
        url="https://restricted.example.org/tenders",
        capability="access_restricted",
        probe_success=False,
        error="The portal denied public access (HTTP 403).",
    )
    assert created["enabled"] is False
    assert created["last_attempt_at"] is not None
    assert created["last_success_at"] is None
    assert created["last_error"] == "The portal denied public access (HTTP 403)."


def test_duplicate_normalized_custom_urls_are_rejected(monkeypatch):
    backend = _MemoryRegistry()
    monkeypatch.setattr(registry, "get_supabase", lambda: backend)
    registry.create_pending_custom_source(
        name="Feed one", url="https://www.example.org/feed?id=2&utm_medium=email",
    )
    try:
        registry.create_pending_custom_source(
            name="Feed two", url="https://example.org/feed?id=2",
        )
        raise AssertionError("duplicate source URL should be rejected")
    except registry.DuplicateSource:
        pass
    assert len(backend.rows) == 1


def test_source_mutation_routes_reject_bad_csrf_before_network_or_storage(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "source-test-secret")
    called = []
    monkeypatch.setattr(registry, "create_pending_custom_source", lambda **_kw: called.append("save"))
    monkeypatch.setattr(app_module, "test_saved_source", lambda *_a: called.append("probe"))
    session = {"email": "staff@cortechconsultinggroup.com", "iat": 1}
    with pytest.raises(HTTPException) as caught:
        app_module.sources_add(
            None, "Public tenders", "https://example.org/feed", "invalid", session
        )
    assert caught.value.status_code == 403
    assert called == []


def test_source_mutation_accepts_an_authorized_session_and_valid_csrf(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "source-test-secret")
    saved = []
    monkeypatch.setattr(
        registry,
        "create_pending_custom_source",
        lambda **kwargs: saved.append(kwargs) or {"id": "source-1"},
    )
    monkeypatch.setattr(app_module, "test_saved_source", lambda source_id: {
        "ok": True, "capability": "rss_atom", "preview": [], "items": [], "error": "",
    })
    session = {"email": "staff@cortechconsultinggroup.com", "iat": 1}
    response = app_module.sources_add(
        None,
        "County tenders",
        "county.example.org/feed.xml",
        auth.csrf_token(session),
        session,
    )
    assert response.status_code == 303
    assert saved[0]["url"] == "https://county.example.org/feed.xml"
    assert saved[0]["name"] == "County tenders"


def test_source_writes_require_an_authorized_session_and_all_mutations_check_csrf():
    request = Request({
        "type": "http", "method": "POST", "path": "/sources/add",
        "headers": [], "query_string": b"", "scheme": "https",
        "server": ("test", 443), "client": ("127.0.0.1", 1),
    })
    with pytest.raises(HTTPException) as caught:
        app_module.require_session(request)
    assert caught.value.status_code == 401
    for handler in (
        app_module.sources_add,
        app_module.sources_test,
        app_module.sources_enable,
        app_module.sources_pause,
    ):
        assert "_check_csrf" in handler.__code__.co_names
        assert inspect.signature(handler).parameters["session"].default.dependency is app_module.require_session


def test_sources_screen_lists_builtin_and_custom_health_rows(monkeypatch):
    from starlette.requests import Request
    from database import discovery_schedule

    monkeypatch.setattr(
        app_module, "_builtin_source_definitions",
        lambda: [{"name": "Built-in notices", "url": "https://builtin.example.org/tenders"}],
    )
    monkeypatch.setattr(registry, "ensure_builtin_sources", lambda _defs: {})
    monkeypatch.setattr(registry, "list_sources", lambda: [{
        "id": "custom-1", "source_key": "custom-1", "name": "County feed",
        "url": "https://county.example.org/feed.xml", "source_type": "custom",
        "capability": "rss_atom", "enabled": True,
        "last_attempt_at": "2026-11-06T12:00:00+00:00",
        "last_success_at": "2026-11-06T12:00:00+00:00",
        "discovered_count": 4, "last_error": "", "last_preview": [],
    }])
    monkeypatch.setattr(discovery_schedule, "status", lambda: {
        "next_run_at": "2026-11-08T14:00:00+00:00",
        "last_attempt_at": "2026-11-08T12:00:00+00:00",
        "last_success_at": "2026-11-08T12:00:00+00:00",
        "last_outcome": "success",
    })
    captured = {}
    monkeypatch.setattr(
        app_module.templates,
        "TemplateResponse",
        lambda _request, _template, context: captured.update(context) or context,
    )
    request = Request({
        "type": "http", "method": "GET", "path": "/sources",
        "headers": [], "query_string": b"", "scheme": "https",
        "server": ("test", 443), "client": ("127.0.0.1", 1),
    })

    app_module.sources_page(request)
    rows = {row["name"]: row for row in captured["sources"]}
    assert set(rows) == {"Built-in notices", "County feed"}
    assert rows["County feed"]["last_attempt_at"] == "2026-11-06T12:00:00+00:00"
    assert rows["County feed"]["discovered_count"] == 4
    assert captured["schedule_available"] is True
    assert captured["schedule_status"]["last_outcome"] == "success"
