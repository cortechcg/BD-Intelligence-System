"""Phase 3 observed-data market digest — aggregation, thin-n refusal, fail-open."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from intelligence.market_trends import (
    INSUFFICIENT_TREND_PHRASE,
    ObservedOpportunity,
    TREND_MIN_N,
    build_market_digest,
    extract_labels,
    parse_observed_date,
    window_bounds,
)
from intelligence.organizations import match_organization, index_with_match
from reporting import email_report
from database import market_store
from database import supabase_client


AS_OF = date(2026, 9, 15)


def _row(**kwargs) -> ObservedOpportunity:
    payload = dict(
        source_id="row-1",
        source="supabase",
        source_url="https://procurement.example/tender-1",
        title="Evaluation of programme",
        discovered_on=AS_OF,
        themes=("Evaluation",),
        locations=("Somalia",),
        donor="",
    )
    payload.update(kwargs)
    return ObservedOpportunity(**payload)


def _n_rows(n: int, **kwargs) -> list[ObservedOpportunity]:
    rows = []
    for i in range(n):
        extras = dict(kwargs)
        extras.setdefault("source_id", f"row-{i}")
        extras.setdefault("source_url", f"https://procurement.example/tender-{i}")
        rows.append(_row(**extras))
    return rows


def _send_capture(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        email_report,
        "_send_email",
        lambda subject, content, attachment_path=None: sent.update(
            subject=subject, content=content
        )
        or True,
    )
    return sent


def test_trend_minimum_is_ten():
    assert TREND_MIN_N == 10


def test_trailing_window_is_inclusive_calendar_days():
    start, end = window_bounds(AS_OF, 30)
    assert end == AS_OF
    assert start == AS_OF - timedelta(days=29)
    start90, end90 = window_bounds(AS_OF, 90)
    assert end90 == AS_OF
    assert (end90 - start90).days == 89


def test_parse_observed_date_never_invents():
    assert parse_observed_date("2026-09-15") == date(2026, 9, 15)
    assert parse_observed_date("2026-09-15T08:30:00+03:00") == date(2026, 9, 15)
    assert parse_observed_date(None) is None
    assert parse_observed_date("") is None
    assert parse_observed_date("TBD") is None
    assert parse_observed_date("soon") is None
    assert parse_observed_date("2099-12-01") == date(2099, 12, 1)
    assert parse_observed_date(1726400000) is None  # unix timestamp is not a stored date field
    assert parse_observed_date({"year": 2026}) is None
    assert parse_observed_date(["2026-09-15"]) is None


def test_extract_labels_skips_malformed_and_does_not_split_into_buckets():
    labels, bad = extract_labels(["Evaluation", {"area": "WASH"}, 12, None, "  "])
    assert labels == ("Evaluation",)
    assert bad == 3
    one, bad2 = extract_labels("WASH, Health")
    assert one == ("WASH, Health",)
    assert bad2 == 0
    empty, bad3 = extract_labels(None)
    assert empty == ()
    assert bad3 == 0
    skipped, bad4 = extract_labels("n/a")
    assert skipped == ()
    assert bad4 == 0


def test_thin_n2_digest_is_insufficient_not_a_percentage_trend(monkeypatch):
    digest = build_market_digest(
        _n_rows(2, themes=("Evaluation",), locations=("Somalia",), donor="FCDO"),
        as_of=AS_OF,
        org_index=[],
        orgs_available=False,
    )
    assert digest.themes[30].sample_size == 2
    assert digest.themes[30].is_trend is False
    assert digest.themes[30].status == "INSUFFICIENT DATA"
    assert digest.themes[30].counts == ()
    assert INSUFFICIENT_TREND_PHRASE in digest.themes[30].message
    sent = _send_capture(monkeypatch)
    email_report.send_market_digest_email(digest)
    html = sent["content"]
    assert INSUFFICIENT_TREND_PHRASE in html
    assert "n=2" in html
    assert "2026-08-17 to 2026-09-15" in html
    assert "%" not in html or "n>=10" in html or "n&gt;=10" in html
    # No confident per-theme percentage from n=2.
    assert "50%" not in html
    assert "100%" not in html
    assert "Other WASH" not in html
    assert "East Africa" not in html
    assert "FCDO" not in html
    assert "\r" not in sent["subject"]
    assert "\n" not in sent["subject"]


def test_n9_below_threshold_is_still_insufficient():
    digest = build_market_digest(
        _n_rows(9),
        as_of=AS_OF,
        orgs_available=False,
        org_index=[],
    )
    assert digest.themes[30].sample_size == 9
    assert digest.themes[30].is_trend is False
    assert digest.themes[30].counts == ()


def test_n10_shows_observed_labels_with_sample_size_inline(monkeypatch):
    rows = _n_rows(7, themes=("Evaluation",), locations=("Somalia",))
    rows += [
        _row(
            source_id=f"h-{i}",
            source_url=f"https://procurement.example/health-{i}",
            themes=("Health",),
            locations=("Kenya",),
        )
        for i in range(3)
    ]
    digest = build_market_digest(rows, as_of=AS_OF, org_index=[], orgs_available=False)
    win = digest.themes[30]
    assert win.sample_size == 10
    assert win.is_trend is True
    assert win.status == "VERIFIED"
    labels = {item.label: item.count for item in win.counts}
    assert labels == {"Evaluation": 7, "Health": 3}
    assert "Other WASH" not in labels
    geo = {item.label: item.count for item in digest.geography[30].counts}
    assert geo == {"Somalia": 7, "Kenya": 3}
    assert "East Africa" not in geo
    sent = _send_capture(monkeypatch)
    email_report.send_market_digest_email(digest)
    html = sent["content"]
    assert "Evaluation" in html
    assert "n=10" in html
    assert "2026-08-17 to 2026-09-15" in html
    assert "Other WASH" not in html
    assert "East Africa" not in html
    assert "70% of n=10" in html


def test_empty_store_is_honest_and_does_not_crash(monkeypatch):
    digest = build_market_digest([], as_of=AS_OF, org_index=[], orgs_available=False)
    assert digest.store_row_count == 0
    assert digest.themes[30].sample_size == 0
    assert digest.themes[30].is_trend is False
    sent = _send_capture(monkeypatch)
    email_report.send_market_digest_email(digest)
    html = sent["content"]
    assert INSUFFICIENT_TREND_PHRASE in html
    assert "does not invent market categories" in html
    assert "Other WASH" not in html
    assert "East Africa" not in html


def test_malformed_theme_is_not_counted():
    rows = [
        _row(
            source_id="good",
            source_url="https://procurement.example/good",
            themes=("Evaluation",),
            malformed_theme_skips=0,
        ),
        _row(
            source_id="bad",
            source_url="https://procurement.example/bad",
            themes=(),
            malformed_theme_skips=1,
        ),
    ]
    # Pad to trend threshold with evaluation-only dated rows.
    rows += [
        _row(
            source_id=f"pad-{i}",
            source_url=f"https://procurement.example/pad-{i}",
            themes=("Evaluation",),
        )
        for i in range(8)
    ]
    digest = build_market_digest(rows, as_of=AS_OF)
    labels = {item.label: item.count for item in digest.themes[30].counts}
    assert labels == {"Evaluation": 9}
    assert "WASH" not in labels
    assert digest.themes[30].malformed_skipped >= 1


def test_undated_rows_are_excluded_not_given_invented_dates():
    rows = [
        _row(source_id="dated", discovered_on=AS_OF),
        _row(
            source_id="undated",
            source_url="https://procurement.example/undated",
            discovered_on=None,
            themes=("Health",),
            locations=("Kenya",),
        ),
    ]
    digest = build_market_digest(rows, as_of=AS_OF)
    assert digest.themes[30].sample_size == 1
    assert digest.themes[30].undated_skipped == 1
    assert digest.themes[30].is_trend is False


def test_rows_outside_window_are_not_counted_in_30d():
    old = AS_OF - timedelta(days=40)
    rows = [_row(source_id="old", discovered_on=old, themes=("Health",))]
    rows += _n_rows(2, themes=("Evaluation",))
    digest = build_market_digest(rows, as_of=AS_OF)
    assert digest.themes[30].sample_size == 2
    assert digest.themes[90].sample_size == 3


def test_empty_organizations_table_does_not_invent_donors(monkeypatch):
    rows = _n_rows(12, donor="FCDO")
    digest = build_market_digest(
        rows, as_of=AS_OF, org_index=[], orgs_available=False
    )
    assert digest.donor_windows[30].donors == ()
    assert "invent" in digest.donor_windows[30].message.lower() or "empty" in (
        digest.donor_windows[30].message.lower()
    )
    sent = _send_capture(monkeypatch)
    email_report.send_market_digest_email(digest)
    assert "FCDO" not in sent["content"]
    assert "dummy" not in sent["content"].lower()


def test_unmatched_donor_is_not_listed_even_when_orgs_exist():
    first = match_organization("Nordic Development Fund")
    index = index_with_match([], first)
    rows = _n_rows(12, donor="FCDO")
    digest = build_market_digest(
        rows, as_of=AS_OF, org_index=index, orgs_available=True
    )
    names = [d.canonical_name for d in digest.donor_windows[30].donors]
    assert "FCDO" not in names
    assert names == []
    assert digest.donor_windows[30].status == "INSUFFICIENT DATA"


def test_matched_org_donor_counts_with_sample_size():
    first = match_organization("FCDO")
    index = index_with_match([], first)
    rows = _n_rows(12, donor="FCDO")
    digest = build_market_digest(
        rows, as_of=AS_OF, org_index=index, orgs_available=True
    )
    donors = digest.donor_windows[30].donors
    assert len(donors) == 1
    assert donors[0].canonical_name == first.canonical_name
    assert donors[0].count == 12
    assert digest.donor_windows[30].status == "VERIFIED"
    assert "n=12" in digest.donor_windows[30].message or "12 dated" in digest.donor_windows[30].message


def test_geo_shift_requires_both_windows_at_minimum():
    digest = build_market_digest(_n_rows(2, locations=("Somalia",)), as_of=AS_OF)
    assert digest.geo_shift.status == "INSUFFICIENT DATA"
    assert INSUFFICIENT_TREND_PHRASE in digest.geo_shift.message
    assert digest.geo_shift.rows == ()


def test_geo_shift_when_both_windows_meet_minimum():
    recent = _n_rows(10, locations=("Somalia",))
    older = [
        _row(
            source_id=f"old-{i}",
            source_url=f"https://procurement.example/old-{i}",
            discovered_on=AS_OF - timedelta(days=60),
            themes=("Evaluation",),
            locations=("Kenya",),
        )
        for i in range(10)
    ]
    digest = build_market_digest(recent + older, as_of=AS_OF)
    assert digest.geography[30].is_trend
    assert digest.geography[90].is_trend
    assert digest.geo_shift.status == "VERIFIED"
    labels = {row.label: row for row in digest.geo_shift.rows}
    assert "Somalia" in labels
    assert "Kenya" in labels
    assert "East Africa" not in labels


def test_missing_cache_table_fail_opens(monkeypatch):
    market_store.reset_market_store_status()

    class _Boom:
        def table(self, name):
            raise RuntimeError(
                "PGRST205: Could not find the table 'public.opportunities_cache' "
                "in the schema cache"
            )

    monkeypatch.setattr(market_store, "_client", lambda: _Boom())
    rows, truncated = market_store.load_supabase_opportunity_rows()
    assert rows == []
    assert truncated is False
    market_store.reset_market_store_status()


def test_missing_fact_columns_retry_without_them(monkeypatch):
    market_store.reset_market_store_status()
    calls = []

    class _Result:
        def __init__(self, data):
            self.data = data

    class _Query:
        def __init__(self, cols):
            self.cols = cols

        def select(self, cols):
            calls.append(cols)
            self.cols = cols
            return self

        def limit(self, _n):
            return self

        def execute(self):
            if "thematic_areas" in self.cols:
                raise RuntimeError(
                    "PGRST204: Could not find the 'thematic_areas' column of "
                    "'opportunities_cache' in the schema cache"
                )
            return _Result(
                [
                    {
                        "id": "abc",
                        "source_url": "https://procurement.example/tender",
                        "title": "Endline",
                        "created_at": "2026-09-10T12:00:00+00:00",
                    }
                ]
            )

    class _Client:
        def table(self, name):
            assert name == "opportunities_cache"
            return _Query("")

    monkeypatch.setattr(market_store, "_client", lambda: _Client())
    rows, truncated = market_store.load_supabase_opportunity_rows()
    assert truncated is False
    assert len(rows) == 1
    assert rows[0].discovered_on == date(2026, 9, 10)
    assert rows[0].themes == ()
    market_store.reset_market_store_status()


def test_airtable_load_fail_opens(monkeypatch):
    monkeypatch.setattr(
        "database.airtable_client.get_table",
        lambda name: (_ for _ in ()).throw(RuntimeError("429 rate limit")),
    )
    assert market_store.load_airtable_opportunity_rows() == []


def test_airtable_malformed_record_skipped(monkeypatch):
    class _Table:
        def all(self):
            return [
                {"id": "rec1", "fields": {
                    "title": "Good",
                    "source_url": "https://procurement.example/a",
                    "thematic_areas": ["Evaluation"],
                    "location": ["Somalia"],
                    "donor": "FCDO",
                    "discovered_at": "2026-09-01",
                }},
                {"id": "rec2", "fields": {
                    "title": "Bad theme",
                    "source_url": "https://procurement.example/b",
                    "thematic_areas": [{"area": "WASH"}],
                    "location": ["Kenya"],
                    "discovered_at": "2026-09-02",
                }},
            ]

    monkeypatch.setattr("database.airtable_client.get_table", lambda name: _Table())
    rows = market_store.load_airtable_opportunity_rows()
    by_url = {r.source_url: r for r in rows}
    assert by_url["https://procurement.example/a"].themes == ("Evaluation",)
    assert by_url["https://procurement.example/b"].themes == ()
    assert by_url["https://procurement.example/b"].malformed_theme_skips == 1


def test_store_opportunity_facts_fail_open_when_columns_missing(monkeypatch):
    from utils.hashing import content_hash

    backend = {"upserts": [], "updates": []}

    class _Call:
        def execute(_self):
            return SimpleNamespace(data=[{"id": "row-1"}])

    class _Update:
        def __init__(self, payload):
            backend["updates"].append(payload)

        def eq(self, *_args):
            return self

        def execute(self):
            raise RuntimeError(
                "PGRST204: Could not find the 'thematic_areas' column of "
                "'opportunities_cache' in the schema cache"
            )

    class _Table:
        def upsert(self, row, on_conflict=None):
            backend["upserts"].append(row)
            return _Call()

        def update(self, payload):
            return _Update(payload)

    class _Client:
        def table(self, name):
            return _Table()

    supabase_client.reset_opportunity_facts_status()
    monkeypatch.setattr(supabase_client, "supabase", _Client())
    monkeypatch.setattr(supabase_client, "get_embedding", lambda _t: [0.1])
    body = "Terms of Reference for an evaluation in Somalia."
    row_id = supabase_client.store_opportunity(
        "https://procurement.example/tender",
        "Endline",
        body,
        facts={
            "thematic_areas": ["Evaluation"],
            "locations": ["Somalia"],
            "donor": "FCDO",
            "discovered_at": "2026-09-15",
        },
    )
    assert row_id == "row-1"
    assert backend["upserts"][0]["content_hash"] == content_hash(body)
    assert "thematic_areas" not in backend["upserts"][0]
    assert backend["updates"]
    supabase_client.reset_opportunity_facts_status()


def test_run_market_digest_empty_store_does_not_crash_or_send_live_smtp(monkeypatch):
    import main

    sent = {}
    monkeypatch.setattr(
        "database.market_store.load_observed_opportunities",
        lambda **kwargs: ([], False),
    )
    monkeypatch.setattr(
        "database.market_store.load_org_index_for_digest",
        lambda: ([], False, 0),
    )
    monkeypatch.setattr(
        email_report,
        "_send_email",
        lambda subject, content, attachment_path=None: sent.update(
            subject=subject, content=content
        )
        or True,
    )
    monkeypatch.setattr(main, "send_market_digest_email", email_report.send_market_digest_email)
    monkeypatch.setattr(main, "log_agent_action", lambda **kwargs: None)
    main._run_market_digest()
def test_facts_migration_is_additive_and_not_a_competitor_table():
    from pathlib import Path

    sql = Path("supabase_migration_opportunity_facts.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS thematic_areas" in sql
    assert "ADD COLUMN IF NOT EXISTS locations" in sql
    assert "ADD COLUMN IF NOT EXISTS donor" in sql
    assert "ADD COLUMN IF NOT EXISTS discovered_at DATE" in sql
    assert "competitor" not in sql.lower()
    assert "CREATE TABLE" not in sql
