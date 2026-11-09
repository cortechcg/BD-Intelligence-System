"""Live tender feed keyset pagination; all row fixtures are synthetic."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID
from urllib.parse import parse_qs

import pytest
from postgrest import SyncPostgrestClient
from starlette.testclient import TestClient

from dashboard import app as app_module, auth, queries, settings


def _stamp(value):
    return queries._live_cursor_timestamp(value)


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, client, table):
        self.client, self.table_name = client, table
        self.calls = []
        self.orders = []
        self.max_rows = 1000
        self.cursor_pair = None
        self.source_urls = None

    def select(self, *args, **kwargs):
        self.calls.append(("select", args, kwargs)); return self

    def or_(self, value):
        self.calls.append(("or", value))
        left, right = value.split(",and(discovered_at.eq.", 1)
        ts, rest = left.removeprefix("discovered_at.gt."), right
        ts2, row_id = rest[:-1].split(",id.gt.", 1)
        assert ts == ts2
        self.cursor_pair = (_stamp(ts), str(UUID(row_id)))
        return self

    def order(self, column, desc=False, **kwargs):
        self.calls.append(("order", column, desc)); self.orders.append((column, desc)); return self

    def limit(self, value):
        self.calls.append(("limit", value)); self.max_rows = value; self.client.limits.append(value); return self

    def in_(self, column, values):
        self.calls.append(("in", column, values)); self.source_urls = set(values); return self

    def execute(self):
        rows = list(self.client.rows.get(self.table_name, []))
        if self.source_urls is not None:
            rows = [row for row in rows if row.get("source_url") in self.source_urls]
        if self.cursor_pair is not None:
            rows = [row for row in rows if (_stamp(row["discovered_at"]), str(UUID(row["id"]))) > self.cursor_pair]
        for column, desc in reversed(self.orders):
            rows.sort(key=lambda row: _stamp(row[column]) if column == "discovered_at" else str(row.get(column) or ""), reverse=desc)
        return _Result(rows[:self.max_rows])


class _Supabase:
    def __init__(self, rows):
        self.rows, self.limits, self.queries = rows, [], []

    def table(self, name):
        query = _Query(self, name); self.queries.append(query); return query


def _row(n, timestamp, url=None, title=None, metadata=None):
    return {
        "id": str(UUID(int=n)),
        "source_url": url or f"https://jobs.example.org/tender/{n:03d}",
        "title": title or f"Notice {n:03d}",
        "donor": None,
        "discovered_at": timestamp,
        "source_metadata": metadata or {},
    }


def test_live_tenders_are_bounded_deduplicated_and_tie_sorted(monkeypatch):
    cache = [
        _row(3, "2026-11-08T16:20:30+00:00", "https://jobs.example.org/tender/z",
             "<script>bad</script>", {"source_portal": "UN portal"}),
        _row(1, "2026-11-08", "https://jobs.example.org/tender/a", "Consultancy"),
        _row(2, "2026-11-08", "https://JOBS.example.org/tender/a#top", "Duplicate"),
    ]
    db = _Supabase({"opportunities_cache": cache,
                    "opportunity_processing": [{"source_url": cache[0]["source_url"], "cp_deadline": "2026-12-01"}]})
    monkeypatch.setattr(queries, "get_supabase", lambda: db)
    result = queries.list_live_tenders(limit=900)
    assert [item["url"] for item in result["items"]] == [
        "https://jobs.example.org/tender/a", "https://jobs.example.org/tender/z"
    ]
    ts, row_id = queries._decode_live_tender_cursor(result["next_cursor"])
    assert ts == _stamp("2026-11-08T16:20:30+00:00") and row_id == str(UUID(int=3))
    assert result["has_more"] is False
    assert result["items"][1]["deadline"] == "2026-12-01"
    assert result["items"][1]["source"] == "UN portal"
    assert result["items"][1]["first_seen_display"] == "2026-11-08 16:20 UTC"
    assert result["items"][1]["first_seen_precision"] == "time"
    assert result["items"][0]["first_seen_precision"] == "date"
    assert result["items"][1]["title"] == "<script>bad</script>"
    assert max(db.limits) == 200


def test_live_tender_250_equal_timestamp_rows_page_without_misses_or_duplicates(monkeypatch):
    timestamp = "2026-11-09T12:00:00+00:00"
    rows = [_row(n + 1, timestamp) for n in range(250)]
    db = _Supabase({"opportunities_cache": rows, "opportunity_processing": []})
    monkeypatch.setattr(queries, "get_supabase", lambda: db)
    cursor = queries._encode_live_tender_cursor("2026-11-09T11:59:59+00:00", str(UUID(int=0)))
    all_items = []
    page_sizes = []
    while True:
        page = queries.list_live_tenders(limit=200, since=cursor)
        page_sizes.append(len(page["items"]))
        all_items.extend(page["items"])
        if not page["has_more"]:
            break
        assert page["next_cursor"]
        cursor = page["next_cursor"]
    keys = [item["key"] for item in all_items]
    assert page_sizes == [200, 50]
    assert len(keys) == 250 and len(set(keys)) == 250
    filters = [call[1] for q in db.queries for call in q.calls if call[0] == "or"]
    assert filters and all("discovered_at.gt." in value and "id.gt." in value for value in filters)


def test_postgrest_or_filter_builds_a_valid_encoded_composite_cursor():
    timestamp = "2026-11-09T12:00:00.000000+00:00"
    row_id = str(UUID(int=42))
    expression = f"discovered_at.gt.{timestamp},and(discovered_at.eq.{timestamp},id.gt.{row_id})"
    builder = SyncPostgrestClient("http://postgrest.invalid").from_("opportunities_cache").select("id")
    builder.or_(expression)
    params = parse_qs(str(builder.request.params))
    assert params["or"] == [f"({expression})"]
    assert "%2B00%3A00" in str(builder.request.params)


def test_mixed_timestamps_and_ids_are_ordered_and_legacy_cursor_is_accepted(monkeypatch):
    rows = [
        _row(3, "2026-11-10T00:00:00+00:00"),
        _row(2, "2026-11-09"),
        _row(1, "2026-11-09"),
    ]
    db = _Supabase({"opportunities_cache": rows, "opportunity_processing": []})
    monkeypatch.setattr(queries, "get_supabase", lambda: db)
    assert queries._decode_live_tender_cursor("2026-11-09") == (
        _stamp("2026-11-09"), queries._LIVE_CURSOR_MIN_ID,
    )
    page = queries.list_live_tenders(limit=10, since="2026-11-08T00:00:00Z")
    assert [item["key"] for item in page["items"]] == [rows[2]["source_url"], rows[1]["source_url"], rows[0]["source_url"]]
    assert page["has_more"] is False


def test_late_older_date_row_is_not_visible_after_forward_cursor(monkeypatch):
    rows = [_row(1, "2026-11-08"), _row(2, "2026-11-10")]
    db = _Supabase({"opportunities_cache": rows, "opportunity_processing": []})
    monkeypatch.setattr(queries, "get_supabase", lambda: db)
    start = queries._encode_live_tender_cursor("2026-11-07", str(UUID(int=0)))
    first = queries.list_live_tenders(limit=1, since=start)
    assert first["has_more"] is True
    db.rows["opportunities_cache"].append(_row(3, "2026-11-07"))
    second = queries.list_live_tenders(limit=10, since=first["next_cursor"])
    assert "https://jobs.example.org/tender/003" not in {item["key"] for item in second["items"]}
    assert "older date-only" in queries.list_live_tenders.__doc__


def test_empty_page_has_no_next_cursor_and_invalid_cursor_is_rejected_before_query(monkeypatch):
    db = _Supabase({"opportunities_cache": [_row(1, "2026-11-09")], "opportunity_processing": []})
    monkeypatch.setattr(queries, "get_supabase", lambda: db)
    after = queries._encode_live_tender_cursor("2026-11-10", str(UUID(int=0)))
    empty = queries.list_live_tenders(since=after)
    assert empty == {"items": [], "has_more": False, "next_cursor": None}

    monkeypatch.setattr(queries, "get_supabase", lambda: (_ for _ in ()).throw(AssertionError("queried")))
    for bad in ("today;select *", "not-base64!", "eyJ0cyI6IjIwMjYtMTEtMDkifQ"):
        with pytest.raises(ValueError, match="cursor"):
            queries.list_live_tenders(since=bad)


def test_live_tender_page_and_api_keep_auth_and_incremental_empty_error_states(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "live-feed-test-secret")
    monkeypatch.setattr(settings, "AUTH_ALLOWED_DOMAIN", "cortechconsultinggroup.com")
    client = TestClient(app_module.app, follow_redirects=False)
    assert client.get("/live-tenders").status_code == 302
    assert client.get("/api/live-tenders").status_code == 401

    token = auth.issue_session("staff@cortechconsultinggroup.com")
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    empty_feed = {"items": [], "next_cursor": None, "has_more": False}
    monkeypatch.setattr(queries, "list_live_tenders", lambda **kw: empty_feed)
    assert client.get("/live-tenders").status_code == 200

    calls = []
    monkeypatch.setattr(queries, "list_live_tenders", lambda **kw: (calls.append(kw) or empty_feed.copy()))
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
    assert "MAX_PAGES_PER_TICK" in js and "data.has_more" in js
