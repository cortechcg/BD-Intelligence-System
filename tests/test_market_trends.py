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


def test_submission_deadline_is_not_a_discovery_date():
    from database.market_store import _from_airtable_record, _from_cache_row

    cache = _from_cache_row({
        "id": "row-deadline",
        "source_url": "https://procurement.example/deadline-only",
        "title": "Evaluation",
        "submission_deadline": "2026-12-01",
        "thematic_areas": ["Evaluation"],
        "locations": ["Somalia"],
        "donor": "Client W",
    })
    assert cache is not None
    assert cache.discovered_on is None
    airtable = _from_airtable_record({
        "id": "recDEAD",
        "fields": {
            "source_url": "https://procurement.example/airtable-deadline",
            "title": "Evaluation",
            "submission_deadline": "2026-12-01",
            "thematic_areas": ["Evaluation"],
            "location": ["Somalia"],
        },
    })
    assert airtable is not None
    assert airtable.discovered_on is None


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


# The real opportunities_cache columns on the hosted project (information_schema,
# 2026-09-21). There is no created_at and never was; discovered_at is the row
# timestamp (TIMESTAMPTZ DEFAULT now(), populated 1212/1212).
REAL_CACHE_COLUMNS = frozenset({
    "id", "airtable_opportunity_id", "source_url", "title", "raw_text",
    "analysis_json", "discovered_at", "processed_at", "embedding",
    "content_hash", "thematic_areas", "locations", "donor",
})


class _PostgrestLikeTable:
    """Models what PostgREST actually does: a select that names any unknown
    column fails as a whole with 42703 — it does not return the known ones.
    Honours .order() and a server-side row cap, and reports count="exact"."""

    def __init__(self, rows, *, columns=REAL_CACHE_COLUMNS, server_max_rows=1000, calls=None):
        self._rows = rows
        self._columns = columns
        self._server_max = server_max_rows
        self._calls = calls if calls is not None else []
        self._select = ""
        self._order = None
        self._limit = None
        self._count = None

    def select(self, cols, count=None):
        self._select = cols
        self._count = count
        self._calls.append({"select": cols, "count": count})
        return self

    def order(self, column, desc=False, nullsfirst=None):
        self._order = (column, desc)
        self._calls[-1]["order"] = self._order
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        wanted = [c.strip() for c in self._select.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in self._columns]
        if unknown:
            raise RuntimeError(
                f"{{'message': 'column opportunities_cache.{unknown[0]} does not exist', "
                f"'code': '42703'}}"
            )
        rows = list(self._rows)
        if self._order:
            col, desc = self._order
            rows.sort(key=lambda r: (r.get(col) is None, r.get(col) or ""), reverse=desc)
            # keep NULLs last even when descending
            rows = [r for r in rows if r.get(col) is not None] + [r for r in rows if r.get(col) is None]
        cap = min(self._limit or len(rows), self._server_max)
        page = [{k: r.get(k) for k in wanted} for r in rows[:cap]]

        class _Res:
            pass

        res = _Res()
        res.data = page
        res.count = len(rows) if self._count == "exact" else None
        return res


class _RealSchemaClient:
    def __init__(self, rows, **kw):
        self._rows = rows
        self._kw = kw
        self.calls = []

    def table(self, name):
        assert name == "opportunities_cache"
        return _PostgrestLikeTable(self._rows, calls=self.calls, **self._kw)


def _cache_rows(n, *, start=date(2026, 9, 1), themed_every=1):
    """n rows dated start, start+1, ... ; every `themed_every`-th row carries labels."""
    out = []
    for i in range(n):
        out.append({
            "id": f"row-{i}",
            "source_url": f"https://procurement.example/tender/{i}",
            "title": f"Tender {i}",
            "discovered_at": f"{(start + timedelta(days=i)).isoformat()}T09:00:00+00:00",
            "thematic_areas": ["evaluation"] if i % themed_every == 0 else None,
            "locations": ["Somalia"] if i % themed_every == 0 else None,
            "donor": None,
        })
    return out


def test_missing_fact_columns_retry_without_them(monkeypatch):
    """Project without the Phase 3 fact columns: dates must still be selected.

    The previous version of this test fed a fake row carrying `created_at` and
    so passed while production returned zero dated rows for weeks.
    """
    market_store.reset_market_store_status()
    no_facts = REAL_CACHE_COLUMNS - {"thematic_areas", "locations", "donor"}
    client = _RealSchemaClient(_cache_rows(1, start=date(2026, 9, 10)), columns=no_facts)
    monkeypatch.setattr(market_store, "_client", lambda: client)

    rows, truncated = market_store.load_supabase_opportunity_rows()

    assert truncated is False
    assert len(rows) == 1
    assert rows[0].discovered_on == date(2026, 9, 10), "fallback select must include discovered_at"
    assert rows[0].themes == ()
    selects = [c["select"] for c in client.calls]
    assert all("created_at" not in sel for sel in selects), selects
    market_store.reset_market_store_status()


def test_regression_digest_query_returns_dated_rows_against_the_real_schema(monkeypatch):
    """Would have caught the production defect: a fully populated
    discovered_at column must produce dated rows, not 'insufficient data'."""
    market_store.reset_market_store_status()
    client = _RealSchemaClient(_cache_rows(12, start=date(2026, 9, 1)))
    monkeypatch.setattr(market_store, "_client", lambda: client)

    rows, truncated = market_store.load_supabase_opportunity_rows()

    assert len(rows) == 12
    assert sum(1 for r in rows if r.discovered_on) == 12, "every row has discovered_at; none may be undated"
    assert client.calls[0]["select"].startswith("id, source_url, title, thematic_areas")
    assert len(client.calls) == 1, "the full select must succeed first time against the real schema"
    market_store.reset_market_store_status()


def test_regression_no_select_ever_names_created_at():
    for const in (market_store._CACHE_FULL, market_store._CACHE_WITH_DATE, market_store._CACHE_MIN):
        assert "created_at" not in const, const
    assert "discovered_at" in market_store._CACHE_FULL
    assert "discovered_at" in market_store._CACHE_WITH_DATE


def test_regression_end_to_end_digest_is_a_trend_when_dates_are_populated(monkeypatch):
    """Loader → build_market_digest with ≥10 dated rows inside 30 days."""
    from intelligence.market_trends import build_market_digest

    market_store.reset_market_store_status()
    as_of = date(2026, 9, 21)
    client = _RealSchemaClient(_cache_rows(12, start=date(2026, 9, 1)))
    monkeypatch.setattr(market_store, "_client", lambda: client)

    rows, truncated = market_store.load_supabase_opportunity_rows()
    digest = build_market_digest(rows, as_of=as_of, truncated=truncated)

    assert digest.dated_row_count == 12
    w = digest.themes[30]
    assert w.sample_size == 12
    assert w.is_trend is True, w.message
    assert w.labelled_rows == 12
    assert [(c.label, c.count) for c in w.counts] == [("evaluation", 12)]
    market_store.reset_market_store_status()


def test_loader_asks_for_newest_rows_first_when_dates_are_selected(monkeypatch):
    """A capped, unordered page returned the OLDEST rows on the hosted project,
    leaving the 30-day window empty even with every row dated."""
    market_store.reset_market_store_status()
    rows = _cache_rows(1500, start=date(2022, 1, 1))  # spans years; newest at the end
    client = _RealSchemaClient(rows, server_max_rows=1000)
    monkeypatch.setattr(market_store, "_client", lambda: client)

    loaded, truncated = market_store.load_supabase_opportunity_rows()

    assert client.calls[0].get("order") == ("discovered_at", True)
    newest = max(r.discovered_on for r in loaded)
    assert newest == date(2022, 1, 1) + timedelta(days=1499), "the newest stored row must be in the page"
    market_store.reset_market_store_status()


def test_loader_reports_truncation_from_the_exact_count_not_the_page_length(monkeypatch):
    """PostgREST caps the page at db-max-rows, so a LIMIT cap+1 probe never fires."""
    market_store.reset_market_store_status()
    client = _RealSchemaClient(_cache_rows(1212, start=date(2023, 1, 1)), server_max_rows=1000)
    monkeypatch.setattr(market_store, "_client", lambda: client)

    loaded, truncated = market_store.load_supabase_opportunity_rows()

    assert len(loaded) == 1000
    assert truncated is True
    assert client.calls[0]["count"] == "exact"
    market_store.reset_market_store_status()


def test_loader_does_not_claim_truncation_when_the_store_fits(monkeypatch):
    market_store.reset_market_store_status()
    client = _RealSchemaClient(_cache_rows(40))
    monkeypatch.setattr(market_store, "_client", lambda: client)
    loaded, truncated = market_store.load_supabase_opportunity_rows()
    assert len(loaded) == 40 and truncated is False
    market_store.reset_market_store_status()


def test_truncation_note_states_rows_counted_not_the_configured_cap():
    from intelligence.market_trends import build_market_digest

    rows = [ObservedOpportunity(source_id=str(i), source="supabase", source_url=f"https://x/{i}",
                                title="t", discovered_on=date(2026, 9, 1),
                                themes=(), locations=(), donor="") for i in range(3)]
    digest = build_market_digest(rows, as_of=date(2026, 9, 21), truncated=True)
    note = next(n for n in digest.notes if "truncated" in n)
    assert "3 stored rows were counted" in note
    assert "2000" not in note


def test_window_discloses_label_coverage_so_shares_cannot_mislead():
    """45 of 47 in-window rows on the hosted project carry no labels. A share
    of 'n=47' must travel with how many rows were labelled at all."""
    from intelligence.market_trends import build_market_digest
    from reporting.email_report import _frequency_section_html

    rows = []
    for i in range(12):
        rows.append(ObservedOpportunity(
            source_id=str(i), source="supabase", source_url=f"https://x/{i}", title="t",
            discovered_on=date(2026, 9, 1) + timedelta(days=i),
            themes=("evaluation",) if i < 2 else (),
            locations=(), donor="",
        ))
    digest = build_market_digest(rows, as_of=date(2026, 9, 21))
    w = digest.themes[30]
    assert w.is_trend is True
    assert w.sample_size == 12 and w.labelled_rows == 2
    assert "2 carrying a label" in w.message
    html = _frequency_section_html("Thematic areas", w)
    assert "Label coverage: 2 of 12 dated rows carry a label" in html
    assert "UNKNOWN, not zero" in html
    assert "Share of all dated rows in window" in html


def test_airtable_load_fail_opens():
    assert market_store.load_airtable_opportunity_rows() == []


def test_airtable_malformed_record_skipped():
    good = market_store._from_airtable_record({
        "id": "rec1",
        "fields": {
            "title": "Good",
            "source_url": "https://procurement.example/a",
            "thematic_areas": ["Evaluation"],
            "location": ["Somalia"],
            "donor": "FCDO",
            "discovered_at": "2026-09-01",
        },
    })
    bad = market_store._from_airtable_record({
        "id": "rec2",
        "fields": {
            "title": "Bad theme",
            "source_url": "https://procurement.example/b",
            "thematic_areas": [{"area": "WASH"}],
            "location": ["Kenya"],
            "discovered_at": "2026-09-02",
        },
    })
    assert good.themes == ("Evaluation",)
    assert bad.themes == ()
    assert bad.malformed_theme_skips == 1
    assert market_store.load_airtable_opportunity_rows() == []


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
