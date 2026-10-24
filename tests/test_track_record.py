"""Track record page: the profile data, the merge with past proposals, the page.

The profile facts are real and ship in intelligence/company_profile_2026.json.
Rows here are in-memory fixtures shaped like proposal_embeddings; they test the
merge and the rules. The shipped page never reads fixtures.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dashboard import app as app_module
from dashboard import auth, settings, track_record

PROFILE_PATH = Path("intelligence/company_profile_2026.json")
PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
P01 = PROFILE["projects"][0]["title"]
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2705\u274C]")


def _row(i, title, client="", year=None, themes=(), places=(), won=False, **meta):
    m = {"client": client, "year": year, "thematic_areas": list(themes), "location": list(places)}
    m.update(meta)
    return {"airtable_proposal_id": f"t-{i}", "project_title": title, "won": won, "metadata": m}


EXTRA = [
    _row(1, "Alpha study", "Client One", 2024, ["WASH"], ["Kenya"], True),
    _row(2, "Beta review", "Client One", 2021, ["Research"], ["Uganda"]),
    _row(3, "No details", "", 2026),
]


def _by_title(t):
    return {p["title"]: p for p in t["projects"]}


# ── the shipped profile ─────────────────────────────────────────────────────

def test_profile_file_has_the_stated_shape():
    assert PROFILE["version"] == 1
    assert PROFILE["company"]["established"] == 2015
    assert PROFILE["company"]["tagline"] == "Where innovation meets impact."
    assert len(PROFILE["company"]["offices"]) == 3
    assert [len(PROFILE[k]) for k in ("projects", "key_partners", "sectors", "themes", "service_lines")] == [15, 9, 13, 13, 6]
    assert len({p["id"] for p in PROFILE["projects"]}) == 15


def test_profile_loader_returns_the_file_and_survives_a_broken_one(tmp_path):
    assert track_record.load_profile()["projects"][0]["id"] == "p01"
    bad = tmp_path / "x.json"
    bad.write_text("{not json", encoding="utf-8")
    assert track_record.load_profile(bad) == {}
    assert track_record.load_profile(tmp_path / "missing.json") == {}


def test_every_project_uses_a_known_theme_and_a_stated_area_group():
    slugs = {t["slug"] for t in PROFILE["themes"]}
    for p in PROFILE["projects"]:
        assert p["theme"] in slugs
        assert p["area_group"] in ("Somalia", "Kenya", "Regional", None)
        assert 2023 <= p["year"] <= 2026
    # A description that names no place gets no area, not a guess.
    assert [p["id"] for p in PROFILE["projects"] if p["area_group"] is None] == ["p12", "p14"]


def test_profile_text_has_no_dashes_emoji_or_senior_wording():
    raw = PROFILE_PATH.read_text(encoding="utf-8")
    assert "\u2014" not in raw and "\u2013" not in raw
    assert not EMOJI.search(raw)
    assert "senior" not in raw.lower()
    assert not Path("intelligence/senior_clients.json").exists()


def test_key_partners_are_the_nine_the_profile_names():
    names = [k["name"] for k in PROFILE["key_partners"]]
    assert names[0] == "African Union / IOM" and names[-1] == "GIZ Somalia"
    assert all(k["work"] for k in PROFILE["key_partners"])


# ── computed figures (profile only, the database empty) ─────────────────────

def test_headline_figures_are_computed_from_the_profile():
    t = track_record.build_track_record([])
    f = t["figures"]
    assert t["available"] and not t["empty"]
    assert (f["assignments"], f["projects"], f["clients"]) == (15, 15, 12)
    assert (f["key_partners"], f["sectors"], f["themes_total"], f["service_lines"], f["offices"]) == (9, 13, 13, 6, 3)
    assert (f["themes_covered"], f["areas"]) == (7, 3)
    assert (f["assign_first"], f["assign_last"], f["established"]) == (2023, 2026, 2015)
    assert f["years_active"] == date.today().year - 2015
    assert f["won"] == 0 and f["library_added"] == 0


def test_timeline_counts_a_range_in_the_year_it_began():
    t = track_record.build_track_record([])
    assert [(y["year"], y["count"]) for y in t["timeline"]] == [(2023, 4), (2024, 3), (2025, 6), (2026, 2)]
    assert sum(len(y["items"]) for y in t["timeline"]) == 15


def test_areas_themes_and_partner_counts_come_from_the_tags():
    t = track_record.build_track_record([])
    assert {a["name"]: a["count"] for a in t["areas"]} == {"Somalia": 6, "Kenya": 5, "Regional": 2}
    assert sum(th["count"] for th in t["themes"]) == 15
    assert len(t["themes"]) == 13 and all(th["items"] for th in t["themes"])
    assert [k["assignments"] for k in t["key_partners"]][5] == 0  # Norwegian Embassy: no selected assignment named


def test_profile_assignments_have_no_outcome_until_a_record_says_won():
    t = track_record.build_track_record([])
    assert all(p["won"] is False for p in t["projects"])


def test_clients_index_unites_named_organisations_and_key_partners():
    t = track_record.build_track_record([])
    names = {c["name"] for c in t["clients"]}
    assert {"UNICEF", "Norwegian Embassy", "DRC", "African Union", "IOM", "HIDIG"} <= names
    partner = {c["name"] for c in t["clients"] if c["key_partner"]}
    assert "GIZ" in partner and "UNICEF" not in partner


# ── the merge with past proposals ───────────────────────────────────────────

def test_an_exact_title_match_marks_the_profile_assignment_won_without_adding_a_line():
    t = track_record.build_track_record([_row(1, P01.upper(), "X", 2026, won=True)])
    assert t["figures"]["projects"] == 15 and t["figures"]["library_added"] == 0
    p = _by_title(t)[P01]
    assert p["won"] is True and p["also_in_library"] is True and t["figures"]["won"] == 1


def test_a_match_that_is_not_won_stays_not_recorded():
    t = track_record.build_track_record([_row(1, P01, "X", 2026, won=False)])
    p = _by_title(t)[P01]
    assert p["won"] is False and p["also_in_library"] is True and t["figures"]["won"] == 0


def test_match_titles_in_the_profile_join_a_differently_worded_record():
    profile = json.loads(json.dumps(PROFILE))
    profile["projects"][0]["match_titles"] = ["Community early warning training"]
    t = track_record.build_track_record([_row(1, "Community early warning training", "X", 2026, won=True)], profile=profile)
    assert t["figures"]["projects"] == 15 and _by_title(t)[P01]["won"] is True


def test_similar_but_different_titles_are_never_fuzzy_matched():
    t = track_record.build_track_record([_row(1, P01[:40], "X", 2026, won=True)])
    assert t["figures"]["projects"] == 16 and t["figures"]["won"] == 1 and t["figures"]["library_added"] == 1


def test_extra_records_add_lines_and_keep_the_profile_figures():
    t = track_record.build_track_record(EXTRA)
    f = t["figures"]
    assert f["assignments"] == 15 and f["projects"] == 18 and f["library_added"] == 3 and f["library_won"] == 1
    assert f["clients"] == 12 and f["clients_all"] == 13
    assert (f["first_year"], f["last_year"]) == (2021, 2026) and (f["assign_first"], f["assign_last"]) == (2023, 2026)
    alpha = _by_title(t)["Alpha study"]
    assert alpha["source"] == "library" and alpha["won"] is True and alpha["theme_slugs"] == ["wash"]
    assert {c["name"] for c in t["clients"] if c["library_only"]} == {"Client One"}
    assert _by_title(t)["Beta review"]["won"] is False  # unknown, not lost (ADR 006)


def test_a_year_with_no_other_evidence_is_not_trusted_and_future_years_drop():
    t = track_record.build_track_record(EXTRA + [_row(9, "Far", "C", date.today().year + 5, ["R"], ["Kenya"])])
    assert _by_title(t)["No details"]["year"] is None
    assert _by_title(t)["Far"]["year"] is None
    assert t["figures"]["no_year"] == 2


def test_library_clients_group_by_normalised_name():
    rows = [_row(1, "A", "Acme Ltd.", 2024, ["R"]), _row(2, "B", "ACME Ltd", 2023, ["R"])]
    t = track_record.build_track_record(rows)
    assert t["figures"]["clients_all"] == 13
    assert [c["projects"] for c in t["clients"] if c["name"].lower().startswith("acme")] == [2]


def test_db_layer_rules_still_hold():
    rows = [_row(1, "Fin", "C", 2024, type="financial"), _row(2, "", "C", 2024), _row(3, "Real", "C", 2024)]
    assert [p["title"] for p in track_record.db_projects(rows)] == ["Real"]
    dup = [
        {**_row(1, "Same title", "C", 2024, ["R"]), "airtable_proposal_id": "library:gone"},
        _row(2, "Same title", "C", 2024, ["R"]),
        _row(3, "Same title", "C", 2024, ["R"], won=True),
    ]
    out = track_record.db_projects(dup, excluded_ids={"library:gone"})
    assert len(out) == 1 and out[0]["won"] is True
    assert [a["name"] for a in track_record.build_track_record(
        [_row(1, "A", "C", 2024, ["R"], ["Trinidad and Tobago"])])["areas"] if "Trinidad" in a["name"]] == ["Trinidad and Tobago"]


def test_filter_projects():
    t = track_record.build_track_record(EXTRA)
    ps = t["projects"]
    f = track_record.filter_projects
    assert len(f(ps, theme="livelihoods")) == 6
    assert len(f(ps, year="2025")) == 6
    assert len(f(ps, area="Somalia")) == 6
    assert [p["title"] for p in f(ps, won=True)] == ["Alpha study"]
    assert len(f(ps, q="  UNICEF ")) >= 1
    assert f(ps, theme="no-such-theme") == []


# ── failure and read-only ───────────────────────────────────────────────────

def test_loader_fails_open_and_the_profile_still_renders(monkeypatch):
    import database.supabase_client as sc

    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(sc, "get_supabase", boom)
    t = track_record.load_track_record()
    assert t["available"] and t["library_error"] and t["figures"]["assignments"] == 15 and len(t["key_partners"]) == 9


def test_data_layer_is_read_only():
    src = open("dashboard/track_record.py").read()
    assert not re.search(r"\)\s*\.(insert|update|upsert|delete)\(", src)
    assert ".rpc(" not in src
    assert "senior_" not in src and "load_senior" not in src


# ── the page ────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "track-test-secret")
    monkeypatch.setattr(settings, "AUTH_ALLOWED_DOMAIN", "cortechconsultinggroup.com")
    return TestClient(app_module.app, follow_redirects=False)


def _signed_in(client):
    client.cookies.set(settings.SESSION_COOKIE_NAME, auth.issue_session("staff@cortechconsultinggroup.com"))
    return client


@pytest.fixture
def page(client, monkeypatch):
    monkeypatch.setattr(track_record, "load_track_record", lambda: track_record.build_track_record([]))
    return _signed_in(client)


def _visible_rows(html):
    return len(re.findall(r'<tr id="p-\d+"(?! hidden)', html))


def test_track_record_requires_sign_in(client):
    r = client.get("/track-record")
    assert r.status_code == 302 and r.headers["location"] == "/auth/login"


def test_old_design_preview_is_gone(client):
    assert client.get("/design-preview").status_code in (302, 404)
    assert _signed_in(client).get("/design-preview").status_code == 404
    assert "/design-preview" not in {getattr(r, "path", "") for r in app_module.app.routes}
    assert not Path("dashboard/templates/redesign.html").exists()
    assert not Path("dashboard/static/redesign.js").exists()


def test_page_renders_the_profile_with_an_empty_database(page):
    r = page.get("/track-record")
    assert r.status_code == 200
    html = r.text
    for text in ("Where innovation meets impact.", "Cortech Consulting Group", "ICA World", "Nairobi, Mogadishu, London",
                 "Selected assignments", "Key partners", "Thematic areas", "Service lines", "Sectors", "All clients",
                 "About this data", "Evidence Building", "African Union / IOM", "Public Financial Management",
                 "Agricultural &amp; rural development"):
        assert text in html
    assert 'aria-current="page"' in html and 'href="/track-record"' in html
    assert _visible_rows(html) == 15
    assert html.count('class="tr-roll__item"') == 9 and html.count('class="tr-line"') == 6
    assert len(re.findall(r'class="tr-theme tr-theme--', html)) == 13 and html.count('class="tr-tl__col') == 4
    assert "No further past proposals are recorded in the Knowledge Library yet" in html


def test_page_wording_is_honest_about_the_selection_and_never_says_senior(page):
    html = page.get("/track-record").text
    low = re.sub(r"<[^>]+>", " ", html).lower()
    assert "key partners" in low and "senior" not in low
    assert "selection from the company profile 2026" in low or "selection, not the full record" in low
    assert "full record is in the" in low
    assert "all projects" not in low
    assert "\u2014" not in html and not EMOJI.search(html)
    for text in ("2nd Floor, ACK Gardens, 1st Av Ngong Road", "Makkah al Mukaramma Road, Mogadishu",
                 "71-75 Shelton Street, Covent Garden, London WC2H 9JQ", "www.cortechcgl.com", "info@cortechcgl.com"):
        assert text in html


def test_profile_missing_values_read_na_and_outcome_is_not_recorded(page):
    html = page.get("/track-record").text
    assert html.count("Not recorded") >= 15 and 'class="v-missing">n/a' in html
    assert "tr-tag--won" not in html


def test_core_content_and_filters_need_no_script(page):
    html = page.get("/track-record").text
    body = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    assert "Welthungerhilfe" in body and "Livelihoods" in body
    form = re.search(r"<form class=\"tr-filters\"[^>]*>", html).group(0)
    assert "hidden" not in form and 'method="get"' in form and 'action="/track-record#projects"' in form
    assert 'class="btn btn-ghost tr-apply"' in html and 'type="submit"' in html
    # Theme tiles and year links are plain links with a server-side result.
    assert 'href="/track-record?theme=livelihoods#themes"' in html and 'href="/track-record?year=2025#projects"' in html
    # The script is an enhancement only, loaded deferred, from this origin.
    assert '<script src="/static/track.js' in html and "defer" in html


def test_filters_work_on_the_server(page):
    assert _visible_rows(page.get("/track-record?theme=livelihoods").text) == 6
    assert _visible_rows(page.get("/track-record?year=2025").text) == 6
    assert _visible_rows(page.get("/track-record?area=Somalia&year=2026").text) == 1
    assert _visible_rows(page.get("/track-record?won=1").text) == 0
    r = page.get("/track-record?theme=livelihoods")
    assert 'class="tr-tpanel is-open" id="tp-livelihoods"' in r.text
    assert "No assignment matches these filters" in page.get("/track-record?q=zzzzzz").text
    # An unknown theme is ignored, not echoed; markup in a filter is escaped.
    odd = page.get("/track-record?theme=<script>x</script>&q=<b>")
    assert odd.status_code == 200 and "<script>x</script>" not in odd.text and "tr-tpanel is-open" not in odd.text


def test_page_merges_past_proposals_and_marks_won(client, monkeypatch):
    rows = [_row(1, P01, "X", 2026, won=True), *EXTRA]
    monkeypatch.setattr(track_record, "load_track_record", lambda: track_record.build_track_record(rows))
    html = _signed_in(client).get("/track-record").text
    assert _visible_rows(html) == 18 and "Alpha study" in html and "Knowledge Library</span>" in html
    assert html.count("tr-tag tr-tag--won") >= 2
    assert "3 more past proposals from the Knowledge Library are added below" in html


def test_database_down_shows_a_note_and_the_whole_profile(client, monkeypatch):
    monkeypatch.setattr(track_record, "load_track_record", lambda: track_record.unavailable("The records could not be read just now."))
    r = _signed_in(client).get("/track-record")
    assert r.status_code == 200 and "could not be read just now" in r.text and _visible_rows(r.text) == 15


def test_missing_profile_file_is_reported_not_crashed(client, monkeypatch):
    monkeypatch.setattr(track_record, "load_track_record", lambda: track_record.build_track_record([], profile={}))
    # An empty dict means "use the file"; force a truly empty profile through the loader instead.
    monkeypatch.setattr(track_record, "load_profile", lambda path=None: {})
    monkeypatch.setattr(track_record, "load_track_record", lambda: track_record.build_track_record([]))
    r = _signed_in(client).get("/track-record")
    assert r.status_code == 200 and "could not be read" in r.text and _visible_rows(r.text) == 0


def test_nav_has_track_record_and_no_preview_link(page):
    html = page.get("/track-record").text
    assert 'href="/track-record"' in html
    assert "design-preview" not in html and "Bid desk redesign preview" not in html
