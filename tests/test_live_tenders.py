"""Live tender feed reads only persisted rows; fixtures here are synthetic."""
from __future__ import annotations

from starlette.testclient import TestClient

from dashboard import app as app_module, auth, queries, settings


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, client, table):
        self.client, self.table_name = client, table
        self.calls = []

    def select(self, *args, **kwargs): self.calls.append(("select", args)); return self
    def gte(self, *args): self.calls.append(("gte", args)); return self
    def order(self, *args, **kwargs): self.calls.append(("order", args, kwargs)); return self
    def limit(self, value): self.calls.append(("limit", value)); self.client.limits.append(value); return self
    def in_(self, *args): self.calls.append(("in", args)); return self
    def execute(self): return _Result(self.client.rows.get(self.table_name, []))


class _Supabase:
    def __init__(self, rows): self.rows, self.limits = rows, []
    def table(self, name): return _Query(self, name)


def test_live_tenders_are_bounded_deduplicated_and_tie_sorted(monkeypatch):
    cache = [
        {"source_url": "https://jobs.example.org/tender/z", "title": "<script>bad</script>",
         "donor": "UN", "discovered_at": "2026-11-08T16:20:30+00:00", "source_metadata": {"source_portal": "UN portal"}},
        {"source_url": "https://jobs.example.org/tender/a", "title": "Consultancy", "donor": None,
         "discovered_at": "2026-11-08", "source_metadata": {}},
        {"source_url": "https://JOBS.example.org/tender/a#top", "title": "Duplicate", "donor": None,
         "discovered_at": "2026-11-08", "source_metadata": {}},
    ]
    db = _Supabase({"opportunities_cache": cache,
                    "opportunity_processing": [{"source_url": cache[0]["source_url"], "cp_deadline": "2026-12-01"}]})
    monkeypatch.setattr(queries, "get_supabase", lambda: db)
    result = queries.list_live_tenders(limit=900, since="2026-11-08")
    assert [item["url"] for item in result["items"]] == [
        "https://jobs.example.org/tender/a", "https://jobs.example.org/tender/z"
    ]
    assert result["cursor"] == {"at": "2026-11-08T16:20:30+00:00", "key": "https://jobs.example.org/tender/z"}
    assert result["items"][1]["deadline"] == "2026-12-01"
    assert result["items"][1]["source"] == "UN portal"
    assert result["items"][1]["first_seen_display"] == "2026-11-08 16:20 UTC"
    assert result["items"][1]["first_seen_precision"] == "time"
    assert result["items"][0]["first_seen_precision"] == "date"
    assert result["items"][1]["title"] == "<script>bad</script>"  # template/JS must escape it
    assert max(db.limits) == 200


def test_live_tenders_rejects_bad_cursor_before_query(monkeypatch):
    monkeypatch.setattr(queries, "get_supabase", lambda: (_ for _ in ()).throw(AssertionError("queried")))
    try:
        queries.list_live_tenders(since="today;select *")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid cursor accepted")


def test_live_tender_page_and_api_keep_auth_and_incremental_empty_error_states(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "live-feed-test-secret")
    monkeypatch.setattr(settings, "AUTH_ALLOWED_DOMAIN", "cortechconsultinggroup.com")
    client = TestClient(app_module.app, follow_redirects=False)
    assert client.get("/live-tenders").status_code == 302
    assert client.get("/api/live-tenders").status_code == 401

    token = auth.issue_session("staff@cortechconsultinggroup.com")
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    monkeypatch.setattr(queries, "list_live_tenders", lambda **kw: {"items": [], "cursor": None})
    assert client.get("/live-tenders").status_code == 200

    calls = []
    monkeypatch.setattr(queries, "list_live_tenders", lambda **kw: (calls.append(kw) or {"items": [], "cursor": None}))
    empty = client.get("/api/live-tenders?since=2026-11-08")
    assert empty.status_code == 200 and empty.json()["items"] == []
    assert calls[-1] == {"limit": 200, "since": "2026-11-08"}

    monkeypatch.setattr(queries, "list_live_tenders", lambda **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    assert client.get("/api/live-tenders").status_code == 503


def test_live_tender_shell_and_polling_contract():
    import inspect
    source = inspect.getsource(app_module)
    assert '@app.get("/api/live-tenders")' in source
    js = open("dashboard/static/live-tenders.js", encoding="utf-8").read()
    assert 'setTimeout(refresh, delay)' in js and 'document.visibilityState' in js
    assert 'new Set(' in js and 'credentials: "same-origin"' in js
