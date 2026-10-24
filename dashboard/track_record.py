"""Track record: the company profile plus the past proposals, read-only.

Two real sources, merged:

1. ``intelligence/company_profile_2026.json`` is the base record: the verified
   facts from Cortech's Company Profile 2026 (company, 13 sectors, 6 service
   lines, 13 thematic areas, 9 key partners and 15 selected assignments). It
   ships in the repo, is versioned, and always renders, even when the database
   has nothing or cannot be read. Only two things in it are not copied from the
   profile and both are marked: the area group and the single thematic area of
   each assignment, tagged by Cortech BD from the project description.
2. ``proposal_embeddings`` adds past proposals and marks an assignment won
   where a matching record says so. Rows come from ``embed_cvs.py`` (original
   corpus: ``client``, ``year``, ``thematic_areas``, ``location``) and from the
   Knowledge Library ingest (``client``, ``donor``, ``country``, ``sectors``,
   ``date``).

Nothing is invented or inferred from a name. Rules, all stated on the page:

* A project is "Won" only when a record says ``won`` is true. ``won=False`` means
  the outcome was not recorded (ADR 006), never "lost". Profile assignments carry
  no outcome, so they read "Not recorded" until a library record says won.
* A library row joins a profile assignment only on an identical normalised title
  or an explicit ``match_titles`` entry in the profile file. Otherwise it is added
  as its own project, labelled as coming from the Knowledge Library.
* A year is shown only when the record carries one. The original ingest wrote the
  current year when extraction failed, so a row with no client, no areas and no
  themes has its year ignored.
* A range such as 2023 to 2024 counts in the year it began.
* Clients are grouped by the same name normalisation the organisation index uses.
  Acronyms are not expanded except where the profile file pairs them.
* There is no seniority flag in the data. The profile names nine key partners and
  they are shown as exactly that. "Senior" is not used.
* Financial-only library documents are left out so a project is not counted twice.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from loguru import logger

from intelligence.organizations import normalize_org_name

_PROFILE_PATH = Path(__file__).resolve().parents[1] / "intelligence" / "company_profile_2026.json"
_PLACEHOLDERS = {"", "unknown", "n/a", "na", "none", "null", "tbd", "not specified", "unspecified"}
_SPLIT = re.compile(r"\s*[,;\n]\s*")
_YEAR = re.compile(r"(19[89]\d|20\d\d)")
PAGE = 1000


def _clean(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return "" if text.casefold() in _PLACEHOLDERS else text


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _names(value: Any) -> list[str]:
    """A list or a delimited string, as clean distinct labels in first-seen order."""
    if isinstance(value, str):
        # Only split plain delimited strings; a list item is already one label.
        items = [p for p in _SPLIT.split(value)]
    elif isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        items = []
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = _clean(item)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _year(meta: dict, trust: bool) -> int | None:
    if not trust:
        return None
    raw = meta.get("year")
    if raw in (None, "", 0, "0"):
        raw = meta.get("date")
    match = _YEAR.search(str(raw or ""))
    if not match:
        return None
    year = int(match.group(1))
    return year if year <= date.today().year + 1 else None


def _key(raw: str) -> str:
    if not raw:
        return ""
    forms = normalize_org_name(raw)
    return forms.compact or forms.spaced


def load_profile(path: Path | None = None) -> dict:
    """The versioned Company Profile 2026 file. A missing or broken file gives an empty profile."""
    try:
        payload = json.loads((path or _PROFILE_PATH).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning(f"company profile unreadable: {type(exc).__name__}")
        return {}


def _project(row: dict) -> dict | None:
    meta = _as_dict(row.get("metadata"))
    if str(meta.get("type") or "").casefold() == "financial":
        return None
    title = _clean(row.get("project_title"))
    if not title:
        return None
    client = _clean(meta.get("client"))
    areas = _names(meta.get("location")) or _names(meta.get("country"))
    themes = _names(meta.get("thematic_areas")) or _names(meta.get("sectors"))
    # An extraction that found nothing at all is the fingerprint of the old
    # fallback that stamped the current year. Its year is not evidence.
    year = _year(meta, trust=bool(client or areas or themes))
    won = row.get("won") is True or meta.get("won") is True
    return {
        "title": title,
        "client": client or None,
        "donor": _clean(meta.get("donor")) or None,
        "year": year,
        "areas": areas,
        "themes": themes,
        "won": won,
    }


def _richness(p: dict) -> int:
    return sum(bool(p[k]) for k in ("client", "year", "areas", "themes"))


def db_projects(rows: list[dict], excluded_ids: set[str] | None = None) -> list[dict]:
    """Pure: proposal_embeddings rows in, de-duplicated projects out."""
    excluded = excluded_ids or set()
    by_title: dict[str, dict] = {}
    for row in rows or []:
        if str(row.get("airtable_proposal_id") or "") in excluded:
            continue
        project = _project(row)
        if project is None:
            continue
        key = _key(project["title"]) or project["title"].casefold()
        prior = by_title.get(key)
        if prior is None:
            project["key"] = key
            by_title[key] = project
        else:
            best = project if _richness(project) > _richness(prior) else prior
            best["won"] = prior["won"] or project["won"]
            best["key"] = key
            by_title[key] = best
    return list(by_title.values())


def _span(year: int | None, year_to: int | None) -> str | None:
    if not year:
        return None
    return f"{year} to {year_to}" if year_to and year_to != year else str(year)


def _year_range(years: list[int]) -> tuple[int, int] | None:
    return (min(years), max(years)) if years else None


def build_track_record(rows: list[dict], *, profile: dict | None = None,
                       excluded_ids: set[str] | None = None, library_error: str = "") -> dict:
    """Pure: the profile and proposal_embeddings rows in, one display model out."""
    profile = load_profile() if profile is None else profile
    company = profile.get("company") or {}
    themes_def = list(profile.get("themes") or [])
    theme_by_slug = {t["slug"]: t for t in themes_def}
    theme_by_name = {t["name"].casefold(): t for t in themes_def}

    projects: list[dict] = []
    for p in profile.get("projects") or []:
        theme = theme_by_slug.get(p.get("theme"))
        group = p.get("area_group")
        projects.append({
            "source": "profile", "id": p.get("id"), "title": p["title"], "client": p.get("client") or None,
            "client_key": p.get("client_key") or p.get("client"), "orgs": list(p.get("orgs") or []),
            "year": p.get("year"), "year_label": _span(p.get("year"), p.get("year_to")),
            "places": list(p.get("places") or []), "areas": [group] if group else [],
            "themes": [theme["name"]] if theme else [], "theme_slugs": [theme["slug"]] if theme else [],
            "won": False, "also_in_library": False,
            "match_keys": {_key(p["title"])} | {_key(t) for t in p.get("match_titles") or []},
        })

    # Library rows: join a profile assignment on title only, otherwise add.
    index: dict[str, dict] = {}
    for proj in projects:
        for k in proj["match_keys"]:
            index.setdefault(k, proj)
    library_added = 0
    library_won = 0
    for d in db_projects(rows, excluded_ids):
        hit = index.get(d["key"])
        if hit is not None:
            hit["also_in_library"] = True
            hit["won"] = hit["won"] or d["won"]
            library_won += 1 if d["won"] else 0
            continue
        slugs = [theme_by_name[t.casefold()]["slug"] for t in d["themes"] if t.casefold() in theme_by_name]
        projects.append({
            "source": "library", "id": None, "title": d["title"], "client": d["client"],
            "client_key": d["client"], "orgs": [d["client"]] if d["client"] else [],
            "year": d["year"], "year_label": _span(d["year"], None), "places": list(d["areas"]),
            "areas": list(d["areas"]), "themes": list(d["themes"]), "theme_slugs": slugs,
            "won": d["won"], "also_in_library": True, "match_keys": set(),
        })
        library_added += 1
        library_won += 1 if d["won"] else 0

    projects.sort(key=lambda p: (-(p["year"] or 0), (p["client"] or "~").casefold(), p["title"].casefold()))
    for i, p in enumerate(projects, 1):
        p["n"] = i
        p.pop("match_keys", None)

    # Years: a range counts in the year it began.
    year_n: dict[int, int] = defaultdict(int)
    year_won: dict[int, int] = defaultdict(int)
    year_items: dict[int, list[dict]] = defaultdict(list)
    for p in projects:
        if p["year"]:
            year_n[p["year"]] += 1
            year_won[p["year"]] += 1 if p["won"] else 0
            year_items[p["year"]].append({"n": p["n"], "client": p["client"], "won": p["won"],
                                          "year_label": p["year_label"]})
    span = _year_range(sorted(year_n))
    timeline = [{"year": y, "count": year_n.get(y, 0), "won": year_won.get(y, 0), "items": year_items.get(y, [])}
                for y in range(span[0], span[1] + 1)] if span else []

    # Areas: ranked groups, with the places named inside each.
    area_n: dict[str, int] = defaultdict(int)
    area_name: dict[str, str] = {}
    area_places: dict[str, dict[str, str]] = defaultdict(dict)
    for p in projects:
        for a in p["areas"]:
            k = a.casefold()
            area_n[k] += 1
            area_name.setdefault(k, a)
            for pl in p["places"]:
                if pl.casefold() != k:
                    area_places[k].setdefault(pl.casefold(), pl)
    areas = [{"name": area_name[k], "count": n, "places": list(area_places[k].values())}
             for k, n in area_n.items()]
    areas.sort(key=lambda r: (-r["count"], r["name"].casefold()))

    # Themes: the 13 profile themes, with the assignments tagged to each.
    themes = []
    for t in themes_def:
        tagged = [p for p in projects if t["slug"] in p["theme_slugs"]]
        themes.append({
            "n": t["n"], "slug": t["slug"], "name": t["name"], "items": list(t.get("items") or []),
            "count": len(tagged),
            "projects": [{"n": p["n"], "title": p["title"], "client": p["client"], "year_label": p["year_label"]}
                         for p in tagged],
        })

    # Clients: every organisation the profile or a library row names, one line each.
    orgs: dict[str, dict] = {}

    def org(name: str) -> dict | None:
        k = _key(name)
        if not k:
            return None
        return orgs.setdefault(k, {"name": name, "projects": 0, "years": [], "areas": {}, "profile_named": False,
                                   "key_partner": False, "library": False})

    for name in profile.get("named_organizations") or []:
        o = org(name)
        if o:
            o["profile_named"] = True
    key_partner_keys: set[str] = set()
    for kp in profile.get("key_partners") or []:
        for name in kp.get("orgs") or []:
            o = org(name)
            if o:
                o["key_partner"] = True
                o["profile_named"] = True
                key_partner_keys.add(_key(name))
    for p in projects:
        seen: set[str] = set()
        for name in p["orgs"]:
            o = org(name)
            if not o or _key(name) in seen:
                continue
            seen.add(_key(name))
            o["projects"] += 1
            o["library"] = o["library"] or p["source"] == "library"
            o["profile_named"] = o["profile_named"] or p["source"] == "profile"
            if p["year"]:
                o["years"].append(p["year"])
            for a in p["areas"]:
                o["areas"].setdefault(a.casefold(), a)
    client_rows = []
    for o in orgs.values():
        ys = sorted(o["years"])
        client_rows.append({
            "name": o["name"], "projects": o["projects"],
            "first_year": ys[0] if ys else None, "last_year": ys[-1] if ys else None,
            "areas": list(o["areas"].values()), "key_partner": o["key_partner"],
            "profile_named": o["profile_named"], "library_only": o["library"] and not o["profile_named"],
        })
    client_rows.sort(key=lambda c: (-c["projects"], not c["key_partner"], c["name"].casefold()))

    def _distinct(items: list[dict]) -> int:
        return len({_key(p["client_key"]) for p in items if p["client_key"] and _key(p["client_key"])})

    profile_projects = [p for p in projects if p["source"] == "profile"]
    p_years = [p["year"] for p in profile_projects if p["year"]]
    partner_rows = []
    for kp in profile.get("key_partners") or []:
        keys = {_key(o) for o in kp.get("orgs") or []}
        n_proj = sum(1 for p in profile_projects if keys & {_key(o) for o in p["orgs"]})
        partner_rows.append({**kp, "assignments": n_proj})
    figures = {
        "assignments": sum(1 for p in projects if p["source"] == "profile"),
        "projects": len(projects),
        "clients": _distinct(profile_projects),
        "clients_all": _distinct(projects),
        "assign_first": min(p_years) if p_years else None,
        "assign_last": max(p_years) if p_years else None,
        "areas": len(area_n),
        "themes_total": len(themes_def),
        "themes_covered": sum(1 for t in themes if t["count"]),
        "key_partners": len(profile.get("key_partners") or []),
        "sectors": len(profile.get("sectors") or []),
        "service_lines": len(profile.get("service_lines") or []),
        "offices": len(company.get("offices") or []),
        "established": company.get("established"),
        "years_active": (date.today().year - company["established"]) if company.get("established") else None,
        "won": sum(1 for p in projects if p["won"]),
        "first_year": span[0] if span else None,
        "last_year": span[1] if span else None,
        "no_client": sum(1 for p in projects if not p["client"]),
        "no_year": sum(1 for p in projects if not p["year"]),
        "no_area": sum(1 for p in projects if not p["areas"]),
        "no_theme": sum(1 for p in projects if not p["themes"]),
        "library_added": library_added,
        "library_won": library_won,
    }
    return {
        "available": True,
        "profile_loaded": bool(profile),
        "empty": not projects,
        "library_error": library_error,
        "company": company,
        "project_note": profile.get("project_note", ""),
        "source": profile.get("source", ""),
        "projects": projects,
        "clients": client_rows,
        "key_partners": partner_rows,
        "sectors": list(profile.get("sectors") or []),
        "service_lines": list(profile.get("service_lines") or []),
        "areas": areas,
        "themes": themes,
        "timeline": timeline,
        "figures": figures,
        "filters": {
            "clients": sorted({p["client"] for p in projects if p["client"]}, key=str.casefold),
            "areas": [a["name"] for a in areas],
            "themes": [{"slug": t["slug"], "name": t["name"]} for t in themes],
            "years": sorted(year_n, reverse=True),
        },
    }


def filter_projects(projects: list[dict], *, q: str = "", client: str = "", year: str = "", area: str = "",
                    theme: str = "", won: bool = False) -> list[dict]:
    """Server-side filter, so the table works without a script."""
    q = q.strip().casefold()
    out = []
    for p in projects:
        if q and q not in f"{p['title']} {p['client'] or ''}".casefold():
            continue
        if client and p["client"] != client:
            continue
        if year and str(p["year"] or "") != year:
            continue
        if area and area not in p["areas"]:
            continue
        if theme and theme not in p["theme_slugs"]:
            continue
        if won and not p["won"]:
            continue
        out.append(p)
    return out


def unavailable(reason: str) -> dict:
    """The profile still renders; only the library part is reported as unreadable."""
    return build_track_record([], library_error=reason)


def _fetch_rows(sb) -> list[dict]:
    out: list[dict] = []
    start = 0
    while True:
        batch = (
            sb.table("proposal_embeddings")
            .select("airtable_proposal_id,project_title,won,metadata")
            .range(start, start + PAGE - 1)
            .execute()
            .data
            or []
        )
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        start += PAGE


def _archived_library_ids(sb) -> set[str]:
    """Embedding rows for library documents that were archived are not track record."""
    try:
        rows = (
            sb.table("library_documents").select("id").not_.is_("archived_at", "null").execute().data or []
        )
    except Exception:
        return set()
    return {f"library:{r['id']}" for r in rows if r.get("id")}


def load_track_record() -> dict:
    """Read-only. The profile always renders; a database failure is reported, never raised."""
    try:
        from database.supabase_client import get_supabase

        sb = get_supabase()
        rows = _fetch_rows(sb)
        return build_track_record(rows, excluded_ids=_archived_library_ids(sb))
    except Exception as exc:
        logger.warning(f"track record library read failed: {type(exc).__name__}")
        return unavailable("The past-proposal records in the Knowledge Library could not be read just now.")
