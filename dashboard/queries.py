"""Read-only Supabase queries for the dashboard.

Every value this module returns is either a real column/JSONB path on a real
row, or ``None`` with a ``why`` string explaining the absence. Nothing is
defaulted to a plausible-looking number. See ``docs/DASHBOARD_DESIGN.md`` §5.

The ``source`` on each cell is the literal Postgres path the value came from,
so the UI can cite it and a reviewer can check it by hand.

This module never writes. Triggering lives in ``dashboard/triggers.py``.
"""
from __future__ import annotations

import math
import time
from collections.abc import Iterable
from typing import Any

from loguru import logger

from database.supabase_client import get_supabase
from utils.urls import canonicalize_url

# Agent-owned stages, in order. `reviewed` / `outcome` are human/Airtable
# transitions (ADR 011) and are deliberately not part of the agent rail.
AGENT_STAGES = ("discovered", "extracted", "scored", "drafted")
HUMAN_STAGES = ("reviewed", "outcome")
ALL_STAGES = AGENT_STAGES + HUMAN_STAGES

# Lease states from supabase_migration_opportunity_state.sql + ADR 011.
LEASE_STATES = ("pending", "processing", "failed", "completed", "dead_letter")

_SPEND_CAP_MARKERS = ("spend cap", "spend_cap", "MAX_RUN_COST_USD")


# ── the value primitive ─────────────────────────────────────────────────────

def cell(value: Any, *, why: str = "", source: str = "", label: str = "") -> dict:
    """One displayable value plus its provenance.

    ``why`` is only surfaced when the value is actually absent — so a caller
    cannot accidentally attach a "not yet scored" caption to a real number.
    """
    absent = value is None or (isinstance(value, str) and not value.strip())
    return {
        "value": None if absent else value,
        "why": why if absent else "",
        "source": source,
        "label": label,
    }


def _num(raw: Any) -> float | int | None:
    """PostgREST ``->>`` returns text. Convert without inventing a zero."""
    if raw is None or raw == "":
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return int(f) if f.is_integer() else f


def _canon(url: str | None) -> str:
    if not url:
        return ""
    return canonicalize_url(url) or url


def classify_failure(state: str | None, last_error: str | None) -> str:
    """Map a ledger row to the state the UI shows. Never guesses a reason.

    Returns one of the ``LEASE_STATES`` plus the synthetic ``spend_cap``, which
    is a *presentation* refinement of ``failed`` (ADR 012: a cap halt is
    retryable and does not increment the draft-fail count).
    """
    state = (state or "").strip()
    if state == "failed" and last_error:
        low = last_error.lower()
        # A dashboard Cancel reuses the spend-cap halt point (utils/errors.
        # RunHaltRequested) so the ledger row looks like a cap halt — check
        # the recorded reason first so it is shown as what it was.
        if "cancelled" in low or "halted at operator request" in low:
            return "cancelled"
        if any(m.lower() in low for m in _SPEND_CAP_MARKERS):
            return "spend_cap"
    return state if state in LEASE_STATES else "pending"


def stage_rail(current: str | None) -> list[dict]:
    """The agent rail with each stage marked done/current against real data."""
    cur = (current or "").strip()
    # A human stage implies every agent stage completed.
    idx = ALL_STAGES.index(cur) if cur in ALL_STAGES else -1
    rail = []
    for i, name in enumerate(AGENT_STAGES):
        rail.append({
            "name": name,
            "done": idx >= i,
            "current": cur == name,
        })
    return rail


# ── ledger + cache reads ────────────────────────────────────────────────────

_LEDGER_SELECT = (
    "source_url,title,state,pipeline_stage,draft_fail_count,last_error,"
    "attempt_count,discovered_at,updated_at,completed_at,lease_until,"
    "cp_title:checkpoint->analysis->opportunity->>title,"
    "cp_client:checkpoint->analysis->opportunity->>client,"
    "cp_donor:checkpoint->analysis->opportunity->>donor,"
    "cp_deadline:checkpoint->analysis->opportunity->>submission_deadline,"
    "cp_recommendation:checkpoint->>recommendation,"
    "cp_fit:checkpoint->>fit_score,"
    "cp_win:checkpoint->>win_prob,"
    "cp_submission_type:checkpoint->>submission_type,"
    "cp_airtable:checkpoint->>airtable_record_id"
)


def _ledger_row_to_item(row: dict) -> dict:
    url = row.get("source_url") or ""
    state = row.get("state")
    last_error = row.get("last_error")
    ui_state = classify_failure(state, last_error)
    stage = row.get("pipeline_stage") or None

    # The ledger `title` is often the discovery-time placeholder ("Unknown").
    # The extracted title in the checkpoint is the real one when it exists.
    cp_title = (row.get("cp_title") or "").strip()
    led_title = (row.get("title") or "").strip()
    title = cp_title or (led_title if led_title.lower() != "unknown" else "")

    fit = _num(row.get("cp_fit"))
    win = _num(row.get("cp_win"))
    rec = (row.get("cp_recommendation") or "").strip() or None

    not_scored = "not yet scored"
    if stage in ("discovered", "extracted") or stage is None:
        not_scored = f"not yet scored (stage: {stage or 'unknown'})"

    return {
        "source_url": url,
        "canonical_url": _canon(url),
        "has_ledger": True,
        "title": cell(
            title,
            why="title not extracted",
            source="opportunity_processing.checkpoint->analysis->opportunity->>title",
        ),
        "client": cell(
            (row.get("cp_client") or "").strip() or None,
            why="not extracted",
            source="opportunity_processing.checkpoint->analysis->opportunity->>client",
        ),
        "donor": cell(
            (row.get("cp_donor") or "").strip() or None,
            why="not extracted",
            source="opportunity_processing.checkpoint->analysis->opportunity->>donor",
        ),
        "deadline": cell(
            (row.get("cp_deadline") or "").strip() or None,
            why="no deadline in the ToR",
            source=(
                "opportunity_processing.checkpoint->analysis->opportunity"
                "->>submission_deadline"
            ),
        ),
        "stage": cell(
            stage,
            why="no stage recorded",
            source="opportunity_processing.pipeline_stage",
        ),
        "lease_state": cell(
            state, why="", source="opportunity_processing.state"
        ),
        "ui_state": ui_state,
        "rail": stage_rail(stage),
        "recommendation": cell(
            rec, why=not_scored, source="opportunity_processing.checkpoint->>recommendation"
        ),
        "fit": cell(fit, why=not_scored, source="opportunity_processing.checkpoint->>fit_score"),
        "win_probability": cell(
            win, why=not_scored, source="opportunity_processing.checkpoint->>win_prob"
        ),
        "submission_type": cell(
            (row.get("cp_submission_type") or "").strip() or None,
            why="not yet determined",
            source="opportunity_processing.checkpoint->>submission_type",
        ),
        "airtable_record_id": cell(
            (row.get("cp_airtable") or "").strip() or None,
            why="no Airtable record stored",
            source="opportunity_processing.checkpoint->>airtable_record_id",
        ),
        "draft_fail_count": int(row.get("draft_fail_count") or 0),
        "attempt_count": int(row.get("attempt_count") or 0),
        "last_error": cell(
            (last_error or "").strip() or None,
            why="no error recorded",
            source="opportunity_processing.last_error",
        ),
        "discovered_at": row.get("discovered_at"),
        "updated_at": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
    }


def _cache_row_to_item(row: dict) -> dict:
    url = row.get("source_url") or ""
    title = (row.get("title") or "").strip()
    # Discovery writes the literal placeholder "Unknown" when it has no title.
    # That is an absence, not a title, and must not render as one.
    if title.lower() == "unknown":
        title = ""
    no_ledger = "no ledger row — never claimed for processing"
    return {
        "source_url": url,
        "canonical_url": _canon(url),
        "has_ledger": False,
        "title": cell(
            title or None, why="title not stored", source="opportunities_cache.title"
        ),
        "client": cell(None, why=no_ledger, source="opportunity_processing.checkpoint"),
        "donor": cell(
            (row.get("donor") or "").strip() or None,
            why="donor fact column empty (0/1210 occupancy)",
            source="opportunities_cache.donor",
        ),
        "deadline": cell(None, why=no_ledger, source="opportunity_processing.checkpoint"),
        "stage": cell(None, why=no_ledger, source="opportunity_processing.pipeline_stage"),
        "lease_state": cell(None, why=no_ledger, source="opportunity_processing.state"),
        "ui_state": "no_ledger",
        "rail": stage_rail(None),
        "recommendation": cell(None, why=no_ledger, source="opportunity_processing.checkpoint"),
        "fit": cell(None, why=no_ledger, source="opportunity_processing.checkpoint"),
        "win_probability": cell(None, why=no_ledger, source="opportunity_processing.checkpoint"),
        "submission_type": cell(None, why=no_ledger, source="opportunity_processing.checkpoint"),
        "airtable_record_id": cell(
            (row.get("airtable_opportunity_id") or "").strip() or None,
            why="no Airtable record stored",
            source="opportunities_cache.airtable_opportunity_id",
        ),
        "draft_fail_count": 0,
        "attempt_count": 0,
        "last_error": cell(None, why="no error recorded", source="opportunity_processing.last_error"),
        "discovered_at": row.get("discovered_at") or row.get("created_at"),
        "updated_at": row.get("processed_at"),
        "completed_at": None,
    }


def list_ledger_items(limit: int = 200) -> list[dict]:
    """Every opportunity the agent has actually claimed, newest activity first."""
    sb = get_supabase()
    res = (
        sb.table("opportunity_processing")
        .select(_LEDGER_SELECT)
        .order("updated_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [_ledger_row_to_item(r) for r in (res.data or []) if isinstance(r, dict)]


def _ledger_urls(cap: int = 5000) -> set[str]:
    sb = get_supabase()
    res = sb.table("opportunity_processing").select("source_url").limit(cap).execute()
    return {_canon(r.get("source_url")) for r in (res.data or []) if isinstance(r, dict)}


def list_cache_without_ledger(limit: int = 50, scan: int = 400) -> tuple[list[dict], int]:
    """Discovered-and-cached opportunities the agent has never claimed.

    Returns ``(items, total_cache_rows)``. ``scan`` bounds how far back we look;
    the caller shows the real total so the list is never mistaken for the whole
    store.
    """
    sb = get_supabase()
    known = _ledger_urls()
    res = (
        sb.table("opportunities_cache")
        .select(
            "source_url,title,donor,discovered_at,processed_at,airtable_opportunity_id",
            count="exact",
        )
        .order("discovered_at", desc=True)
        .limit(scan)
        .execute()
    )
    total = res.count if res.count is not None else len(res.data or [])
    out: list[dict] = []
    for row in res.data or []:
        if not isinstance(row, dict):
            continue
        if _canon(row.get("source_url")) in known:
            continue
        out.append(_cache_row_to_item(row))
        if len(out) >= limit:
            break
    return out, int(total)


HALTED_STATES = ("failed", "dead_letter", "spend_cap", "cancelled")


def queue_view(limit: int = 50) -> dict:
    """Backing data for the Trigger / Queue screen.

    The buckets are drawn from real ledger states rather than a single
    "pending" pile, because those states mean genuinely different things:
    a halted run needs a human, an unclaimed cache row needs a first run, and
    a ``completed`` row that never reached ``drafted`` is a deliberate terminal
    skip (consultancy gate FALSE or deterministic NO-BID, ADR 011 §5) — not
    work that was dropped.
    """
    ledger = list_ledger_items()

    halted, in_flight, terminal_skip, drafted = [], [], [], []
    for item in ledger:
        stage = item["stage"]["value"]
        state = item["ui_state"]
        if state in HALTED_STATES:
            halted.append(item)
        elif stage == "drafted" or stage in HUMAN_STAGES:
            drafted.append(item)
        elif state == "completed":
            terminal_skip.append(item)
        else:
            in_flight.append(item)

    undiscovered, cache_total = list_cache_without_ledger(limit=limit)
    return {
        "halted": halted,
        "in_flight": in_flight,
        "terminal_skip": terminal_skip,
        "drafted": drafted[:limit],
        "cache_only": undiscovered,
        "counts": {
            "ledger_rows": len(ledger),
            "cache_rows": cache_total,
            "cache_without_ledger_shown": len(undiscovered),
            "halted": len(halted),
            "in_flight": len(in_flight),
            "terminal_skip": len(terminal_skip),
            "drafted": len(drafted),
        },
    }


# ── detail ──────────────────────────────────────────────────────────────────

def _checkpoint(source_url: str) -> dict:
    sb = get_supabase()
    canonical = _canon(source_url)
    res = (
        sb.table("opportunity_processing")
        .select("checkpoint")
        .eq("source_url", canonical)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return {}
    cp = rows[0].get("checkpoint")
    return cp if isinstance(cp, dict) else {}


def _ledger_one(source_url: str) -> dict | None:
    sb = get_supabase()
    canonical = _canon(source_url)
    res = (
        sb.table("opportunity_processing")
        .select(_LEDGER_SELECT)
        .eq("source_url", canonical)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return _ledger_row_to_item(rows[0]) if rows else None


def _cache_one(source_url: str) -> dict | None:
    sb = get_supabase()
    canonical = _canon(source_url)
    res = (
        sb.table("opportunities_cache")
        .select(
            "source_url,title,donor,thematic_areas,locations,discovered_at,"
            "processed_at,content_hash,airtable_opportunity_id"
        )
        .eq("source_url", canonical)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def _factor_rows(bid: dict) -> list[dict]:
    """The deterministic bid_scorer factor breakdown, exactly as stored."""
    values = bid.get("factor_values") if isinstance(bid.get("factor_values"), dict) else {}
    evidence = bid.get("factor_evidence") if isinstance(bid.get("factor_evidence"), list) else []
    by_name = {
        e.get("name"): e for e in evidence if isinstance(e, dict) and e.get("name")
    }
    names = list(dict.fromkeys(list(by_name.keys()) + list(values.keys())))
    rows = []
    for name in names:
        ev = by_name.get(name) or {}
        raw = ev.get("value", values.get(name))
        status = (ev.get("status") or ("UNKNOWN" if raw is None else "")).upper() or "UNKNOWN"
        rows.append({
            "name": name,
            "value": cell(
                _num(raw),
                why="UNKNOWN — not extracted, not guessed",
                source="checkpoint->analysis->bid_intelligence->factor_values",
            ),
            "status": status,
            "evidence": cell(
                (ev.get("evidence") or "").strip() or None,
                why="no evidence recorded",
                source="checkpoint->analysis->bid_intelligence->factor_evidence",
            ),
            "hits": ev.get("hits") if isinstance(ev.get("hits"), list) else [],
        })
    return rows


#: Each scorer dimension stores its number under a different key. Commercial
#: value in particular is a USD amount under ``contract_value_usd``, not a
#: 0-100 ``score`` — reading it as ``score`` renders a real, extracted budget
#: as "not available", which is the inverse of the mistake this codebase
#: guards against but a mistake all the same.
_DIMENSION_VALUE_KEYS = ("score", "contract_value_usd", "value")


def _dimension(bid: dict, key: str, source_key: str) -> dict:
    raw = bid.get(key)
    if not isinstance(raw, dict):
        return {
            "score": cell(
                _num(raw),
                why="UNKNOWN",
                source=f"checkpoint->analysis->bid_intelligence->{source_key}",
            ),
            "status": "UNKNOWN",
            "unit": "",
            "coverage": None,
            "evidence": cell(None, why="no evidence recorded"),
        }

    value = None
    value_key = "score"
    for candidate in _DIMENSION_VALUE_KEYS:
        if candidate in raw:
            value = _num(raw.get(candidate))
            value_key = candidate
            if value is not None:
                break

    status = str(raw.get("status") or "UNKNOWN").upper()
    unit = raw.get("unit") or ("USD" if value_key == "contract_value_usd" else "")
    return {
        "score": cell(
            value,
            # Do not echo the status as the missing-reason; the template already
            # renders the status label next to it.
            why="not extracted" if status == "UNKNOWN" else f"{status} but no number stored",
            source=f"checkpoint->analysis->bid_intelligence->{source_key}->{value_key}",
        ),
        "status": status,
        "unit": unit,
        "coverage": _num(raw.get("coverage")),
        "evidence": cell(
            (raw.get("evidence") or "").strip() or None,
            why="no evidence recorded",
            source=f"checkpoint->analysis->bid_intelligence->{source_key}->evidence",
        ),
    }


def opportunity_detail(source_url: str) -> dict | None:
    """Everything the detail screen shows, all of it from stored rows."""
    item = _ledger_one(source_url)
    cache = _cache_one(source_url)
    if item is None and cache is None:
        return None

    if item is None:
        item = _cache_row_to_item(cache or {})

    cp = _checkpoint(source_url) if item["has_ledger"] else {}
    analysis = cp.get("analysis") if isinstance(cp.get("analysis"), dict) else {}
    bid = analysis.get("bid_intelligence") if isinstance(analysis.get("bid_intelligence"), dict) else {}
    opp = analysis.get("opportunity") if isinstance(analysis.get("opportunity"), dict) else {}
    reqs = analysis.get("requirements") if isinstance(analysis.get("requirements"), dict) else {}
    sections = cp.get("proposal_sections") if isinstance(cp.get("proposal_sections"), dict) else {}
    client_intel = cp.get("client_intelligence") if isinstance(cp.get("client_intelligence"), dict) else {}

    scored = bool(bid)
    why_unscored = "not yet scored" if item["has_ledger"] else "no ledger row — never claimed"

    calibrated_raw = bid.get("calibrated_win_probability")
    calibrated_value = None
    calibrated_why = "INSUFFICIENT DATA"
    if isinstance(calibrated_raw, dict):
        calibrated_value = _num(calibrated_raw.get("probability"))
        calibrated_why = str(
            calibrated_raw.get("reason")
            or calibrated_raw.get("status")
            or "INSUFFICIENT DATA"
        )
    elif calibrated_raw is not None:
        calibrated_value = _num(calibrated_raw)

    ev = bid.get("expected_value") if isinstance(bid.get("expected_value"), dict) else {}

    detail = dict(item)
    detail.update({
        "scored": scored,
        "has_draft": bool(sections),
        "score_version": cell(
            bid.get("score_version"),
            why=why_unscored,
            source="checkpoint->analysis->bid_intelligence->score_version",
        ),
        "why": cell(
            (bid.get("why") or "").strip() or None,
            why=why_unscored,
            source="checkpoint->analysis->bid_intelligence->why",
        ),
        "fit_dim": _dimension(bid, "fit", "fit"),
        "risk_dim": _dimension(bid, "risk", "risk"),
        "strategic_dim": _dimension(bid, "strategic_value", "strategic_value"),
        "commercial_dim": _dimension(bid, "commercial_value", "commercial_value"),
        "win_heuristic": cell(
            _num(bid.get("win_probability")) if not isinstance(bid.get("win_probability"), dict)
            else _num((bid.get("win_probability") or {}).get("score")),
            why=why_unscored,
            source="checkpoint->analysis->bid_intelligence->win_probability",
            label="heuristic (see docs/SCORING_MODEL.md)",
        ),
        "win_calibrated": cell(
            calibrated_value,
            why=calibrated_why,
            source="checkpoint->analysis->bid_intelligence->calibrated_win_probability",
            label="calibration harness, ADR 010",
        ),
        "expected_value": cell(
            ev.get("value") if isinstance(ev.get("value"), (int, float)) else None,
            why=str(ev.get("reason") or ev.get("value") or "INSUFFICIENT DATA"),
            source="checkpoint->analysis->bid_intelligence->expected_value",
        ),
        "confidence": bid.get("confidence") if isinstance(bid.get("confidence"), dict) else {},
        "llm_audit": bid.get("llm_audit") if isinstance(bid.get("llm_audit"), dict) else {},
        "weights": bid.get("weights") if isinstance(bid.get("weights"), dict) else {},
        "factors": _factor_rows(bid),
        "gaps": [g for g in (bid.get("gaps") or []) if isinstance(g, str)],
        "risks": [r for r in (bid.get("risks") or []) if isinstance(r, str)],
        "budget_usd": cell(
            _num(opp.get("estimated_budget_usd")),
            why="no budget extracted from the ToR",
            source="checkpoint->analysis->opportunity->estimated_budget_usd",
        ),
        "locations": [x for x in (opp.get("project_location") or []) if isinstance(x, str)],
        "thematic_areas": [x for x in (reqs.get("thematic_areas") or []) if isinstance(x, str)],
        "reference_number": cell(
            (opp.get("reference_number") or "").strip() or None,
            why="not extracted",
            source="checkpoint->analysis->opportunity->reference_number",
        ),
        "client_intelligence": client_intel,
        "content_hash": cell(
            (cache or {}).get("content_hash"),
            why="not back-hashed (0/1210 occupancy on existing rows)",
            source="opportunities_cache.content_hash",
        ),
        "in_cache": cache is not None,
        "section_keys": sorted(
            k for k, v in sections.items() if isinstance(v, str) and v.strip()
        ),
        "requirement_alignment": _requirement_alignment(analysis, sections),
    })
    return detail


def _requirement_alignment(analysis: dict, sections: dict) -> dict:
    """Stored trace, or a fresh one for drafts written before this check existed."""
    if not sections:
        return {}
    stored = sections.get("requirement_alignment")
    if isinstance(stored, dict) and stored.get("headline"):
        return stored
    try:
        from intelligence.requirement_alignment import (
            align_draft_to_requirements,
            fail_closed_requirement_alignment,
        )
        return align_draft_to_requirements(
            analysis,
            sections,
            outline=sections.get("submission_outline") or {},
        )
    except Exception as exc:
        try:
            from intelligence.requirement_alignment import fail_closed_requirement_alignment
            return fail_closed_requirement_alignment(
                analysis,
                outline=sections.get("submission_outline") or {},
                error=str(exc),
            )
        except Exception:
            return {
                "headline": "Requirement alignment failed closed. Nothing was treated as satisfied.",
                "rows": [],
                "error": str(exc),
            }


def draft_sections(source_url: str) -> dict:
    """Client-facing draft sections from the stored checkpoint, or {}."""
    cp = _checkpoint(source_url)
    sections = cp.get("proposal_sections")
    return sections if isinstance(sections, dict) else {}


# ── organization history (Phase 2, ADR 006) ─────────────────────────────────

def organization_history(name: str | None) -> dict:
    """Cited roll-up for a client/donor name. Fail-open, never invents counts."""
    blank = {"available": False, "note": "", "match": None, "records": []}
    if not name or not name.strip():
        return {**blank, "note": "no client name extracted for this opportunity"}
    try:
        from database.organizations import (
            collect_matching_store_records,
            load_organization_index,
            organizations_available,
        )
        from intelligence.organizations import match_organization

        if not organizations_available():
            return {**blank, "note": "organizations tables unavailable — fail-open (ADR 006)"}
        entries, label = load_organization_index()
        if label == "missing":
            return {**blank, "note": "organizations index missing — fail-open (ADR 006)"}
        match = match_organization(name, entries)
        records = collect_matching_store_records(match, entries) or []
        return {
            "available": True,
            "note": "" if records else (
                "No stored observations for this organization yet "
                f"(organizations index: {len(entries)} entries). Not zero wins — no records."
            ),
            "match": {
                "canonical_name": getattr(match, "canonical_name", None),
                "status": getattr(match, "status", "UNKNOWN"),
                "method": getattr(match, "method", ""),
                "confidence": getattr(match, "confidence", None),
            },
            "records": [
                {
                    "title": getattr(r, "title", ""),
                    "role": getattr(r, "role", ""),
                    "outcome": getattr(r, "outcome", ""),
                    "outcome_status": getattr(r, "outcome_status", "UNKNOWN"),
                    "source_kind": getattr(r, "source_kind", ""),
                    "source_id": getattr(r, "source_id", ""),
                }
                for r in records
            ],
        }
    except Exception as exc:  # fail-open: optional intel never blocks the view
        logger.warning(f"organization history unavailable (fail-open): {exc}")
        return {**blank, "note": f"organization lookup failed (fail-open): {type(exc).__name__}"}


# ── portfolio ───────────────────────────────────────────────────────────────

def portfolio_view() -> dict:
    sb = get_supabase()
    ledger = list_ledger_items(limit=1000)

    by_stage = {s: 0 for s in ALL_STAGES}
    no_stage = 0
    by_state = {s: 0 for s in LEASE_STATES}
    by_state["spend_cap"] = 0
    by_rec: dict[str, int] = {"BID": 0, "WATCH": 0, "NO-BID": 0}
    unscored = 0

    for item in ledger:
        stage = item["stage"]["value"]
        if stage in by_stage:
            by_stage[stage] += 1
        else:
            no_stage += 1
        by_state[item["ui_state"]] = by_state.get(item["ui_state"], 0) + 1
        rec = item["recommendation"]["value"]
        if rec in by_rec:
            by_rec[rec] += 1
        elif rec:
            by_rec[rec] = by_rec.get(rec, 0) + 1
        else:
            unscored += 1

    cache_count = (
        sb.table("opportunities_cache").select("id", count="exact").limit(1).execute().count
    )

    return {
        "by_stage": by_stage,
        "no_stage": no_stage,
        "by_state": by_state,
        "by_recommendation": by_rec,
        "unscored_ledger_rows": unscored,
        "ledger_rows": len(ledger),
        "cache_rows": int(cache_count or 0),
        "outcomes": outcome_census(),
    }


def outcome_census() -> dict:
    """Won/Lost census under ADR 006/010 rules. ``won=False`` is UNKNOWN."""
    sb = get_supabase()
    won = 0
    unknown = 0
    lost = 0
    note_parts: list[str] = []

    try:
        res = sb.table("proposal_embeddings").select("won", count="exact").limit(5000).execute()
        for row in res.data or []:
            if row.get("won") is True:
                won += 1
            else:
                # ADR 006: an unchecked box is indistinguishable from "not filled".
                unknown += 1
    except Exception as exc:
        note_parts.append(f"proposal_embeddings unreadable ({type(exc).__name__})")

    try:
        sb.table("win_loss_memory").select("outcome").limit(1).execute()
    except Exception:
        note_parts.append(
            "win_loss_memory table does not exist (PGRST205) — no VERIFIED LOST source"
        )

    return {
        "won": won,
        "lost": lost,
        "unknown": unknown,
        "labelled_total": won + lost,
        "threshold_note": (
            "Calibration needs n_labelled ≥ 30 AND ≥ 10 per class (ADR 010). "
            f"Current: {won} WON / {lost} LOST → calibrated_win_probability stays null."
        ),
        "notes": note_parts,
    }


# ── market digest (Phase 3 — surfaced, not rebuilt) ─────────────────────────

_digest_cache: dict[str, Any] = {"at": 0.0, "value": None}
_DIGEST_TTL_SECONDS = 600


def market_digest(force: bool = False):
    """The Phase 3 observed-data digest, Supabase rows only.

    Uses ``intelligence.market_trends.build_market_digest`` — the same function
    ``main.py --run-market-digest`` uses. ``include_airtable=False`` keeps this
    inside an HTTP request; the weekly email also merges Airtable CRM rows, so
    its counts can be higher. The UI states that.
    """
    now = time.time()
    if not force and _digest_cache["value"] is not None:
        if now - float(_digest_cache["at"]) < _DIGEST_TTL_SECONDS:
            return _digest_cache["value"]
    try:
        from datetime import datetime

        from database.market_store import (
            load_observed_opportunities,
            load_org_index_for_digest,
        )
        from intelligence.market_trends import build_market_digest

        records, truncated = load_observed_opportunities(include_airtable=False)
        org_index, orgs_available, _ = load_org_index_for_digest()
        cited_awards: list[dict] = []
        try:
            from database.intelligence_facts import load_award_observations

            cited_awards = load_award_observations(limit=50)
        except Exception:
            cited_awards = []
        digest = build_market_digest(
            records,
            as_of=datetime.now().date(),
            org_index=org_index,
            orgs_available=orgs_available,
            truncated=truncated,
            cited_awards=cited_awards,
        )
    except Exception as exc:
        logger.warning(f"market digest unavailable (fail-open): {exc}")
        digest = None
    _digest_cache["at"] = now
    _digest_cache["value"] = digest
    return digest


def bar_rows(counts: Iterable[tuple[str, int]], ramp: list[str]) -> list[dict]:
    """Direct-labelled bar rows. Required relief for the sub-3:1 brass ramp."""
    items = [(str(k), int(v)) for k, v in counts]
    top = max((v for _, v in items), default=0)
    out = []
    for i, (label, value) in enumerate(items):
        out.append({
            "label": label,
            "value": value,
            "pct": (value / top * 100) if top else 0.0,
            "color": ramp[min(i, len(ramp) - 1)],
        })
    return out
