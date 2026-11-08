"""Deterministic tests for configurable procurement discovery."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import main
from dashboard import app as dashboard_app, auth, settings
from database import procurement_sources as registry
from database import supabase_client
from monitors import configurable_sources as configured
from utils.dns_pinned_http import PinnedResponse
from utils.urls import UnsafeURLError, safe_url_log_label

FEED_URL = "https://procurement.example.org/public/notices.xml"
SOURCE_ID = str(uuid.UUID("8c5c345b-ccab-4f5a-a0ac-353d2a15d217"))
FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Public notices</title>
<item><title>Request for proposals: Endline evaluation Somalia Lot 1 Addendum 2</title>
<guid>IFTB-2026-17</guid><link>https://procurement.example.org/notices/IFTB-2026-17?notice_id=17</link>
<description>Consultancy opportunity for an evaluation in Somalia.</description>
<pubDate>Fri, 06 Nov 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


def _response(body: bytes, *, status: int = 200, headers: dict | None = None, url: str = FEED_URL):
    return PinnedResponse(
        status_code=status,
        headers={"content-type": "application/rss+xml", **(headers or {})},
        body=body,
        pinned_ip="93.184.216.34",
        url=url,
    )


def _row(**patch):
    row = {
        "id": SOURCE_ID,
        "name": "Public notices",
        "url": FEED_URL,
        "source_type": "custom",
        "capability": "rss_atom",
        "enabled": True,
        "last_attempt_at": None,
        "last_success_at": None,
    }
    row.update(patch)
    return row


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/feed.xml",
        "http://[::1]/feed.xml",
        "http://[fe80::1]/feed.xml",
        "http://[::ffff:127.0.0.1]/feed.xml",
        "http://[64:ff9b::a9fe:a9fe]/feed.xml",
        "https://[2001:db8::1]/feed.xml",
        "https://168.63.129.16/metadata",
        "https://user:secret@example.org/feed.xml",
        "https://example.org:8443/feed.xml",
        "file:///etc/passwd",
    ],
)
def test_add_source_normalization_blocks_unsafe_destinations(url):
    with pytest.raises(UnsafeURLError):
        registry.normalize_source_url(url)


def test_normalization_adds_https_and_keeps_procurement_identifiers():
    normalized = registry.normalize_source_url("WWW.Example.org/notices?id=42&utm_source=mail")
    assert normalized == "https://example.org/notices?id=42"
    assert registry.normalize_source_url(
        "https://example.org/feed?ref=PROC-17&utm_source=mail"
    ) == "https://example.org/feed?ref=PROC-17"


def test_url_log_label_omits_path_and_query_secrets():
    label = safe_url_log_label("https://user:secret@example.org/private/path?token=secret")
    assert label == "https://example.org"
    assert "secret" not in label


def test_public_ipv6_source_normalizes_to_a_valid_authority():
    normalized = registry.normalize_source_url("https://[2606:4700:4700::1111]/notices")
    assert normalized == "https://[2606:4700:4700::1111]/notices"


def test_rss_probe_extracts_a_bounded_preview_and_source_identifiers(monkeypatch):
    monkeypatch.setattr(configured, "exchange", lambda *args, **kwargs: _response(FEED))
    result = configured.probe_source(_row())
    assert result["ok"] is True
    assert result["capability"] == "rss_atom"
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["source_url"].endswith("?notice_id=17")
    assert item["procurement_identifier"] == "IFTB-2026-17"
    assert item["lot_id"] == "1"
    assert item["addendum_id"] == "2"
    assert len(result["preview"]) == 1


def test_same_notice_url_is_distinguished_by_lot_and_addendum():
    body = b"""<rss version="2.0"><channel><title>Notice feed</title>
      <item><title>Somalia evaluation Lot 1</title><guid>ref-1</guid>
        <link>https://procurement.example.org/notices/common</link></item>
      <item><title>Somalia evaluation Lot 2</title><guid>ref-2</guid>
        <link>https://procurement.example.org/notices/common</link></item>
    </channel></rss>"""
    items = configured._feed_items(body, FEED_URL, _row())
    assert len(items) == 2
    assert items[0]["source_url"] == items[1]["source_url"]
    assert items[0]["dedup_url"] != items[1]["dedup_url"]
    assert {item["lot_id"] for item in items} == {"1", "2"}


def test_procurement_reference_query_is_preserved_and_part_of_identity():
    body = b"""<rss version="2.0"><channel><title>Notice feed</title>
      <item><title>Evaluation in Somalia</title><link>https://procurement.example.org/notices/current?ref=IFTB-17</link></item>
      <item><title>Evaluation in Kenya</title><link>https://procurement.example.org/notices/current?ref=IFTB-18</link></item>
    </channel></rss>"""
    items = configured._feed_items(body, FEED_URL, _row())
    assert len(items) == 2
    assert items[0]["source_url"].endswith("?ref=IFTB-17")
    assert items[0]["procurement_identifier"] == "IFTB-17"
    assert items[0]["dedup_url"] != items[1]["dedup_url"]


def test_unsafe_feed_entry_and_attachment_links_are_skipped():
    body = b"""<rss version="2.0"><channel><title>Notice feed</title>
      <item><title>Endline evaluation Somalia</title><link>http://127.0.0.1/tor.pdf</link></item>
      <item><title>Endline evaluation Kenya</title><link>ftp://files.example.org/tor.pdf</link></item>
      <item><title>Endline evaluation Ethiopia</title><link>https://procurement.example.org/notices/17</link></item>
    </channel></rss>"""
    items = configured._feed_items(body, FEED_URL, _row())
    assert [item["source_url"] for item in items] == ["https://procurement.example.org/notices/17"]


def test_public_listing_requires_procurement_context_and_structured_items(monkeypatch):
    html = b"""<html><body><h1>Procurement notices</h1>
      <article class="tender-notice"><h2><a href="/notice/notice-22">Endline evaluation in Somalia</a></h2>
      <p>Invitation to tender, Lot 2.</p></article></body></html>"""
    monkeypatch.setattr(
        configured,
        "exchange",
        lambda *args, **kwargs: _response(html, headers={"content-type": "text/html"}),
    )
    result = configured.probe_source(_row(capability="adapter_required"))
    assert result["capability"] == "public_listing"
    assert result["items"][0]["lot_id"] == "2"


@pytest.mark.parametrize(
    ("status", "body", "content_type", "expected"),
    [
        (401, b"", "text/html", "authentication_required"),
        (403, b"", "text/html", "access_restricted"),
        (403, b"Sign in to continue", "text/html", "authentication_required"),
        (403, b"CAPTCHA verify you are human challenge-platform", "text/html", "captcha_blocked"),
        (200, b"<html>Just a moment... challenge-platform CAPTCHA</html>", "text/html", "captcha_blocked"),
        (200, b"<html><body>Welcome to our company homepage.</body></html>", "text/html", "unsupported"),
        (200, b"not a well formed feed at all", "application/rss+xml", "unsupported"),
    ],
)
def test_capability_failures_are_classified_honestly(monkeypatch, status, body, content_type, expected):
    monkeypatch.setattr(
        configured,
        "exchange",
        lambda *args, **kwargs: _response(
            body, status=status, headers={"content-type": content_type}
        ),
    )
    with pytest.raises(configured.SourceProbeFailure) as caught:
        configured.probe_source(_row())
    assert caught.value.capability == expected


def test_redirect_to_private_ipv4_or_ipv6_is_blocked_before_second_fetch(monkeypatch):
    for destination in ("http://127.0.0.1/metadata", "http://[::1]/private"):
        calls = []

        def exchange(url, **kwargs):
            calls.append(url)
            return _response(b"", status=302, headers={"location": destination})

        monkeypatch.setattr(configured, "exchange", exchange)
        with pytest.raises(UnsafeURLError):
            configured.probe_source(_row())
        assert calls == [FEED_URL]


def test_oversized_fixture_is_rejected_even_if_transport_adapter_returns_it(monkeypatch):
    monkeypatch.setattr(
        configured,
        "exchange",
        lambda *args, **kwargs: _response(
            b"x" * (configured.MAX_SOURCE_RESPONSE_BYTES + 1),
            headers={"content-type": "text/html"},
        ),
    )
    with pytest.raises(ValueError, match="2 MiB"):
        configured.probe_source(_row())


def test_paused_source_is_not_checked(monkeypatch):
    calls = []
    monkeypatch.setattr(configured.registry, "list_sources", lambda **_kw: [_row(enabled=False)])
    monkeypatch.setattr(
        configured.registry,
        "claim_source_run",
        lambda *a, **k: calls.append("claim") or True,
    )
    monkeypatch.setattr(configured, "exchange", lambda *a, **k: calls.append("fetch"))
    opportunities, runs = configured.discover_configured_sources()
    assert opportunities == []
    assert runs == []
    assert calls == []


def test_one_source_failure_does_not_interrupt_another_source(monkeypatch):
    first = _row(id="source-fails", url="https://failed.example.org/feed.xml")
    second = _row(id="source-works", url="https://works.example.org/feed.xml")
    rows = {first["id"]: first, second["id"]: second}
    finished = []
    monkeypatch.setattr(configured.registry, "list_sources", lambda **_kw: [first, second])
    monkeypatch.setattr(configured.registry, "claim_source_run", lambda *a, **k: True)
    monkeypatch.setattr(configured.registry, "get_source", lambda source_id: rows[source_id])
    monkeypatch.setattr(configured.registry, "source_is_enabled", lambda _id: True)
    monkeypatch.setattr(
        configured.registry,
        "finish_source_run",
        lambda source_id, _token, **kw: finished.append((source_id, kw)) or True,
    )
    monkeypatch.setattr(configured, "check_opportunity_exists", lambda _url: False)
    monkeypatch.setattr(configured, "quick_relevance_check", lambda *_a: True)

    def exchange(url, **_kwargs):
        if "failed.example.org" in url:
            raise TimeoutError("provider response body must not escape")
        return _response(FEED, url=url)

    monkeypatch.setattr(configured, "exchange", exchange)
    opportunities, runs = configured.discover_configured_sources()
    assert len(opportunities) == 1
    assert [run["success"] for run in runs] == [False, True]
    configured.finalize_discovery_runs(runs, {"source-works": 1})
    assert [entry[0] for entry in finished] == ["source-fails", "source-works"]
    assert finished[0][1]["success"] is False
    assert finished[1][1]["success"] is True


def test_scheduled_source_results_are_deduplicated_and_health_is_finalized(monkeypatch):
    stored = set()
    finished = []
    row = _row()
    monkeypatch.setattr(configured.registry, "list_sources", lambda **_kw: [row])
    monkeypatch.setattr(configured.registry, "claim_source_run", lambda *a, **k: True)
    monkeypatch.setattr(configured.registry, "get_source", lambda _id: row)
    monkeypatch.setattr(configured.registry, "source_is_enabled", lambda _id: True)
    monkeypatch.setattr(configured.registry, "finish_source_run", lambda *a, **kw: finished.append((a, kw)) or True)
    monkeypatch.setattr(configured, "check_opportunity_exists", lambda url: url in stored)
    monkeypatch.setattr(configured, "quick_relevance_check", lambda *_a: True)
    monkeypatch.setattr(configured, "exchange", lambda *args, **kwargs: _response(FEED))

    opportunities, runs = configured.discover_configured_sources()
    assert len(opportunities) == 1
    assert opportunities[0]["_source_id"] == SOURCE_ID
    stored.add(opportunities[0].get("dedup_url") or opportunities[0]["source_url"])
    configured.finalize_discovery_runs(runs, {SOURCE_ID: 1})
    assert finished[-1][1]["success"] is True
    assert finished[-1][1]["discovered_delta"] == 1
    assert finished[-1][1]["stored_delta"] == 1

    opportunities, runs = configured.discover_configured_sources()
    assert opportunities == []
    configured.finalize_discovery_runs(runs, {})
    assert finished[-1][1]["success"] is True
    assert finished[-1][1]["discovered_delta"] == 1
    assert finished[-1][1]["duplicate_delta"] == 1
    assert finished[-1][1]["stored_delta"] == 0


def test_scheduled_feed_runs_through_main_discovery_and_persists_metadata(monkeypatch):
    """Exercise add-capability output -> scheduled monitor -> shared ledger insert."""
    row = _row()
    stored_urls: set[str] = set()
    inserted: list[dict] = []
    completed: list[dict] = []

    monkeypatch.setattr(configured.registry, "list_sources", lambda **_kw: [row])
    monkeypatch.setattr(configured.registry, "claim_source_run", lambda *a, **k: True)
    monkeypatch.setattr(configured.registry, "get_source", lambda _id: row)
    monkeypatch.setattr(configured.registry, "source_is_enabled", lambda _id: True)
    monkeypatch.setattr(
        configured.registry,
        "finish_source_run",
        lambda _id, _token, **kw: completed.append(kw) or True,
    )
    monkeypatch.setattr(
        configured,
        "check_opportunity_exists",
        lambda url: url in stored_urls,
    )
    monkeypatch.setattr(configured, "quick_relevance_check", lambda *_a: True)
    monkeypatch.setattr(configured, "exchange", lambda *args, **kwargs: _response(FEED))

    class _Query:
        def __init__(self, mode="select", payload=None):
            self.mode, self.payload = mode, payload

        def select(self, *_a):
            self.mode = "select"
            return self

        def eq(self, *_a):
            return self

        def limit(self, *_a):
            return self

        def insert(self, payload):
            self.mode, self.payload = "insert", payload
            return self

        def execute(self):
            if self.mode == "select":
                return SimpleNamespace(data=[])
            inserted.append(self.payload)
            stored_urls.add(self.payload["source_url"])
            return SimpleNamespace(data=[self.payload])

    class _Client:
        def table(self, _name):
            return _Query()

    monkeypatch.setattr(supabase_client, "supabase", _Client())
    monkeypatch.setattr(main, "RSS_FEEDS", [])
    monkeypatch.setattr(main, "scrape_non_rss_sources", lambda: [])
    monkeypatch.setattr(main, "check_assortis_newsletter", lambda: [])
    monkeypatch.setattr(main, "send_report", lambda **_kw: None)
    monkeypatch.setattr(main, "log_agent_action", lambda **_kw: True)
    main._run_pipeline()

    assert len(inserted) == 1
    saved = inserted[0]
    assert "__cortech_variant" in saved["source_url"]
    assert saved["source_metadata"]["procurement_identifier"] == "IFTB-2026-17"
    assert saved["source_metadata"]["lot_id"] == "1"
    assert saved["source_metadata"]["addendum_id"] == "2"
    assert saved["source_metadata"]["original_source_url"] == "https://procurement.example.org/notices/IFTB-2026-17?notice_id=17"
    assert completed[-1]["success"] is True
    assert completed[-1]["discovered_delta"] == 1

    # The second scheduled run sees the same exact source URL in the real
    # discovery dedup callback and does not insert a second opportunity.
    main._run_pipeline()
    assert len(inserted) == 1
    assert completed[-1]["success"] is True
    assert completed[-1]["discovered_delta"] == 1
    assert completed[-1]["duplicate_delta"] == 1
    assert completed[-1]["stored_delta"] == 0


def test_dashboard_add_enable_then_scheduled_discovery_ingests_once(monkeypatch):
    """Verify the complete dashboard-to-source-registry-to-shared-queue path."""

    class _RPCResult:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self

    class _SourceStore:
        def __init__(self):
            self.rows = []

        def table(self, _name):
            return _SourceQuery(self)

        def rpc(self, name, args):
            row = next((item for item in self.rows if item["id"] == args.get("p_source_id")), None)
            if not row:
                return _RPCResult(False)
            if name == "set_procurement_source_enabled":
                if args["p_enabled"] and (
                    row["capability"] not in registry.SUPPORTED_CAPABILITIES
                    or not row.get("last_success_at")
                ):
                    return _RPCResult(False)
                row["enabled"] = args["p_enabled"]
                row["status"] = "ready" if row["enabled"] else "paused"
                return _RPCResult(True)
            if name == "claim_procurement_source_run":
                if not args.get("p_allow_disabled") and not row["enabled"]:
                    return _RPCResult(False)
                if row.get("run_token"):
                    return _RPCResult(False)
                row["run_token"] = args["p_token"]
                row["last_attempt_at"] = "fixture-attempt"
                row["status"] = "checking"
                return _RPCResult(True)
            if name == "finish_procurement_source_run_with_counts":
                if row.get("run_token") != args["p_token"]:
                    return _RPCResult(False)
                if args["p_outcome"] == "success":
                    row["last_success_at"] = "fixture-success"
                    row["last_discovered_count"] = args["p_discovered_delta"]
                    row["last_duplicate_count"] = args["p_duplicate_delta"]
                    row["last_filtered_count"] = args["p_filtered_delta"]
                    row["last_stored_count"] = args["p_stored_delta"]
                    row["discovered_count"] += args["p_stored_delta"]
                    row["candidate_count_total"] += args["p_discovered_delta"]
                    row["duplicate_count_total"] += args["p_duplicate_delta"]
                    row["filtered_count_total"] += args["p_filtered_delta"]
                    row["stored_count_total"] += args["p_stored_delta"]
                    row["last_preview"] = args["p_preview"]
                    row["last_error"] = ""
                    if args.get("p_capability"):
                        row["capability"] = args["p_capability"]
                    row["status"] = "ready" if row["enabled"] else "paused"
                elif args["p_outcome"] == "failure":
                    row["last_error"] = args["p_error"]
                    row["status"] = "needs_attention"
                row["run_token"] = None
                return _RPCResult(True)
            raise AssertionError(f"unexpected RPC {name}")

    class _SourceQuery:
        def __init__(self, backend):
            self.backend = backend
            self.mode = "select"
            self.payload = None
            self.filters = []
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
            self.mode, self.payload = "insert", dict(payload)
            return self

        def execute(self):
            if self.mode == "insert":
                if any(row["url"] == self.payload["url"] for row in self.backend.rows):
                    raise RuntimeError("duplicate source URL")
                row = {
                    "id": f"source-{len(self.backend.rows) + 1}",
                    "last_attempt_at": None,
                    "last_success_at": None,
                    "discovered_count": 0,
                    "candidate_count_total": 0,
                    "duplicate_count_total": 0,
                    "filtered_count_total": 0,
                    "stored_count_total": 0,
                    "last_error": "",
                    "last_preview": [],
                    "run_token": None,
                    **self.payload,
                }
                self.backend.rows.append(row)
                return SimpleNamespace(data=[dict(row)])
            rows = self.backend.rows
            for column, value in self.filters:
                rows = [row for row in rows if row.get(column) == value]
            if self.limit_n:
                rows = rows[:self.limit_n]
            if self.columns != "*":
                fields = [field.strip() for field in self.columns.split(",")]
                rows = [{field: row.get(field) for field in fields} for row in rows]
            return SimpleNamespace(data=[dict(row) for row in rows])

    class _OpportunityCache:
        def __init__(self):
            self.rows = []

        def table(self, _name):
            return _CacheQuery(self)

    class _CacheQuery:
        def __init__(self, backend):
            self.backend = backend
            self.mode = "select"
            self.payload = None
            self.columns = "*"
            self.filters = []
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
            self.mode, self.payload = "insert", dict(payload)
            return self

        def execute(self):
            if self.mode == "insert":
                self.backend.rows.append(dict(self.payload))
                return SimpleNamespace(data=[dict(self.payload)])
            rows = self.backend.rows
            for column, value in self.filters:
                rows = [row for row in rows if row.get(column) == value]
            if self.limit_n:
                rows = rows[:self.limit_n]
            if self.columns != "*":
                fields = [field.strip() for field in self.columns.split(",")]
                rows = [{field: row.get(field) for field in fields} for row in rows]
            return SimpleNamespace(data=[dict(row) for row in rows])

    source_store = _SourceStore()
    opportunity_cache = _OpportunityCache()
    network_checks = []
    monkeypatch.setattr(registry, "get_supabase", lambda: source_store)
    monkeypatch.setattr(supabase_client, "supabase", opportunity_cache)
    monkeypatch.setattr(settings, "SESSION_SECRET", "e2e-source-test-secret")
    monkeypatch.setattr(
        configured,
        "exchange",
        lambda url, **_kw: network_checks.append(url) or _response(FEED, url=url),
    )
    monkeypatch.setattr(configured, "quick_relevance_check", lambda *_a: True)
    monkeypatch.setattr(
        configured,
        "check_opportunity_exists",
        lambda url: any(row["source_url"] == url for row in opportunity_cache.rows),
    )
    monkeypatch.setattr(main, "RSS_FEEDS", [])
    monkeypatch.setattr(main, "scrape_non_rss_sources", lambda: [])
    monkeypatch.setattr(main, "check_assortis_newsletter", lambda: [])
    monkeypatch.setattr(main, "send_report", lambda **_kw: None)
    monkeypatch.setattr(main, "log_agent_action", lambda **_kw: True)

    session = {"email": "staff@cortechconsultinggroup.com", "iat": 1}
    csrf = auth.csrf_token(session)
    added = dashboard_app.sources_add(None, "Public notices", FEED_URL, csrf, session)
    assert added.status_code == 303
    assert len(source_store.rows) == 1
    source_id = source_store.rows[0]["id"]
    assert source_store.rows[0]["enabled"] is False
    assert len(network_checks) == 1

    duplicate = dashboard_app.sources_add(None, "Duplicate", FEED_URL, csrf, session)
    assert duplicate.status_code == 303
    assert len(source_store.rows) == 1
    assert len(network_checks) == 1

    enabled = dashboard_app.sources_enable(source_id, csrf, session)
    assert enabled.status_code == 303
    assert source_store.rows[0]["enabled"] is True

    main._run_pipeline()
    assert len(opportunity_cache.rows) == 1, (
        [(r["enabled"], r["status"], r["last_attempt_at"], r["last_success_at"], r["last_error"])
         for r in source_store.rows],
        opportunity_cache.rows,
    )
    saved = opportunity_cache.rows[0]
    assert saved["source_metadata"]["procurement_identifier"] == "IFTB-2026-17"
    assert saved["source_metadata"]["original_source_url"].endswith("notice_id=17")
    assert saved["source_metadata"]["configured_source_type"] == "custom"
    assert source_store.rows[0]["discovered_count"] == 1

    main._run_pipeline()
    assert len(opportunity_cache.rows) == 1
    assert source_store.rows[0]["discovered_count"] == 1

    paused = dashboard_app.sources_pause(source_id, csrf, session)
    assert paused.status_code == 303
    main._run_pipeline()
    assert len(network_checks) == 3  # add check plus two enabled scheduled checks

    reenabled = dashboard_app.sources_enable(source_id, csrf, session)
    assert reenabled.status_code == 303
    main._run_pipeline()
    assert len(network_checks) == 4
    assert len(opportunity_cache.rows) == 1
    assert source_store.rows[0]["discovered_count"] == 1


def test_custom_source_pipeline_uses_static_non_executing_fetcher(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "load_processing_snapshot", lambda _url: {})
    monkeypatch.setattr(main, "fetch_and_extract", lambda url, **kwargs: captured.update({
        "url": url, **kwargs,
    }) or "fixture text")

    def run_pipeline(_raw, *, force, snapshot, claim_token, deps):
        captured["fetcher"] = deps.fetch_and_extract
        return SimpleNamespace(
            disposition="retryable", error="", increment_draft_fail=False, result=None,
        )

    monkeypatch.setattr(main, "run_opportunity_pipeline", run_pipeline)
    main._process_opportunity_pipeline({
        "source_url": "https://procurement.example.org/notice/17",
        "source_metadata": {"configured_source_type": "custom"},
    })
    assert captured["fetcher"](
        "https://procurement.example.org/notice/17", opportunity_id="opp-17"
    ) == "fixture text"
    assert captured["allow_browser_fallback"] is False


def test_processing_reload_uses_durable_metadata_for_non_variant_sources(monkeypatch):
    captured = {}
    metadata = {
        "configured_source_type": "custom",
        "source_portal": "Public notices",
    }
    monkeypatch.setattr(main, "load_discovered_source_metadata", lambda _url: metadata)
    monkeypatch.setattr(main, "claim_opportunity_processing", lambda *_a, **_kw: "claim-token")
    monkeypatch.setattr(main, "_reserve_processing_slot", lambda: True)
    monkeypatch.setattr(
        main,
        "_process_opportunity_pipeline",
        lambda raw, **_kw: captured.update(raw=raw) or None,
    )
    monkeypatch.setattr(main, "fail_opportunity_processing", lambda *_a, **_kw: True)
    main.process_opportunity({
        "source_url": "https://procurement.example.org/notices/17",
        "title": "Endline evaluation",
    })
    assert captured["raw"]["source_metadata"] == metadata
