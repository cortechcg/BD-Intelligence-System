"""Consultant, rate, log, and opportunity-status access.

These functions used to talk to Airtable. They now read and write Supabase.
The names are unchanged so the pipeline call sites stay put. Nothing here
opens a connection to api.airtable.com.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from loguru import logger

from database.supabase_client import get_supabase
from utils.urls import canonicalize_url

CRM_STATUSES = ("New", "Reviewing", "Bidding", "Won", "Lost", "No-bid")
OPEN_CRM_STATUSES = ("New", "Reviewing", "Bidding")
RESOLVED_CRM_STATUSES = ("Won", "Lost")


def _sb():
    return get_supabase()


def _json_safe(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {}


def _date_or_none(value) -> str | None:
    text = str(value or "").strip()[:10]
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    return None


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _source_url(opportunity_data: dict) -> str:
    raw = (opportunity_data or {}).get("source_url") or ""
    return canonicalize_url(raw) or str(raw).strip()


def _crm_patch(fields: dict, existing: dict | None = None) -> dict:
    """Columns the pipeline used to write on the Airtable opportunity row."""
    merged = dict(_as_dict((existing or {}).get("crm_fields")))
    incoming = _json_safe(fields) or {}
    if isinstance(incoming, dict):
        merged.update(incoming)
    patch = {"crm_fields": merged}
    status = fields.get("status")
    if status in CRM_STATUSES:
        patch["crm_status"] = status
    if "submission_deadline" in fields:
        patch["submission_deadline"] = _date_or_none(fields.get("submission_deadline"))
    return patch


def _crm_record(row: dict) -> dict:
    fields = dict(_as_dict(row.get("crm_fields")))
    if row.get("crm_status"):
        fields["status"] = row["crm_status"]
    if row.get("submission_deadline"):
        fields["submission_deadline"] = str(row["submission_deadline"])[:10]
    if row.get("title") and not fields.get("title"):
        fields["title"] = row["title"]
    if row.get("source_url"):
        fields["source_url"] = row["source_url"]
    return {"id": row.get("source_url") or "", "fields": fields}


def _all_rows(table: str, columns: str) -> list[dict]:
    out: list[dict] = []
    start = 0
    page = 1000
    while True:
        res = _sb().table(table).select(columns).range(start, start + page - 1).execute()
        batch = res.data or []
        out.extend(batch)
        if len(batch) < page:
            return out
        start += page


def create_opportunity(opportunity_data: dict) -> str | None:
    """Write CRM status onto the existing processing row. Never inserts a row.

    Returns the canonical source URL, which is the id later updates use.
    """
    source_url = _source_url(opportunity_data)
    if not source_url:
        logger.warning("create_opportunity skipped — no source_url")
        return None
    try:
        patch = _crm_patch(opportunity_data)
        res = (
            _sb()
            .table("opportunity_processing")
            .update(patch)
            .eq("source_url", source_url)
            .execute()
        )
        if not res.data:
            logger.warning(f"create_opportunity found no processing row for {source_url[:80]}")
            return None
        return source_url
    except Exception as e:
        logger.error(f"Failed to save opportunity status: {e}")
        return None


def update_opportunity(record_id: str, fields: dict) -> bool:
    """Merge CRM fields onto the processing row. Never raises."""
    if not record_id:
        return False
    try:
        current = (
            _sb()
            .table("opportunity_processing")
            .select("crm_fields,crm_status,submission_deadline")
            .eq("source_url", record_id)
            .limit(1)
            .execute()
        )
        existing = (current.data or [None])[0]
        if not existing:
            return False
        patch = _crm_patch(fields, existing)
        _sb().table("opportunity_processing").update(patch).eq("source_url", record_id).execute()
        return True
    except Exception as e:
        logger.warning(f"update_opportunity failed: {e}")
        return False


def _consultant_view(row: dict, record_id: str) -> dict:
    def _list(key: str):
        value = row.get(key)
        return value if isinstance(value, list) else []

    return {
        "id": record_id,
        "full_name": row.get("full_name") or "",
        "role_title": row.get("role_title") or "",
        "years_experience": row.get("years_experience"),
        "education": row.get("education") or "",
        "seniority_level": row.get("seniority_level") or "",
        "based_in": row.get("based_in") or "",
        "day_rate_usd": row.get("day_rate_usd"),
        "thematic_expertise": _list("thematic_expertise"),
        "geographic_experience": _list("geographic_experience"),
        "languages": _list("languages"),
        "tools": _list("tools"),
        "key_skills": row.get("key_skills") or "",
        "key_assignments": row.get("key_assignments") or "",
        "availability_status": row.get("availability_status"),
        "availability_percentage": row.get("availability_percentage"),
        "cv_text": row.get("cv_text") or "",
    }


def get_all_consultants() -> list[dict]:
    """One dict per stored id, including legacy embedding ids for the same person."""
    try:
        rows = _all_rows(
            "consultants",
            "id,full_name,role_title,years_experience,education,seniority_level,based_in,"
            "day_rate_usd,thematic_expertise,geographic_experience,languages,tools,key_skills,"
            "key_assignments,availability_status,availability_percentage,cv_text,legacy_ids",
        )
        out = []
        for row in rows:
            ids = [str(item) for item in (row.get("legacy_ids") or []) if item]
            if row.get("id"):
                ids.append(str(row["id"]))
            seen = set()
            for record_id in ids:
                if record_id in seen:
                    continue
                seen.add(record_id)
                out.append(_consultant_view(row, record_id))
        return out
    except Exception as e:
        logger.warning(f"get_all_consultants failed: {e}")
        return []


def get_consultant_by_id(record_id: str) -> dict | None:
    if not record_id:
        return None
    for consultant in get_all_consultants():
        if consultant.get("id") == record_id:
            return consultant
    return None


def get_rate_card(role_level: str, location: str) -> dict | None:
    """First matching card. A missing row returns None — never an invented price."""
    try:
        res = (
            _sb()
            .table("rate_cards")
            .select("role_level,location,day_rate_usd,per_diem_usd")
            .eq("role_level", role_level)
            .eq("location", location)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as e:
        logger.warning(f"get_rate_card failed: {e}")
        return None


def _proposal_row(row: dict) -> dict:
    meta = _as_dict(row.get("metadata"))
    won = row.get("won")
    if won is None:
        won = meta.get("won")
    return {
        "id": row.get("airtable_proposal_id") or row.get("id") or "",
        "project_title": row.get("project_title") or "",
        "client": meta.get("client") or "",
        "year": meta.get("year"),
        "location": meta.get("location") or "",
        "thematic_areas": meta.get("thematic_areas") or [],
        "won": bool(won),
        "proposal_text": row.get("content_chunk") or "",
    }


def get_past_proposals(limit: int = 20) -> list[dict]:
    try:
        rows = _all_rows(
            "proposal_embeddings",
            "id,airtable_proposal_id,project_title,won,content_chunk,metadata",
        )
        rows.sort(key=lambda row: str(_as_dict(row.get("metadata")).get("year") or ""), reverse=True)
        return [_proposal_row(row) for row in rows[:limit]]
    except Exception as e:
        logger.warning(f"get_past_proposals failed: {e}")
        return []


def get_winning_proposals(limit: int = 5) -> list[dict]:
    """Fallback after semantic search. Only rows already marked won."""
    try:
        rows = _all_rows(
            "proposal_embeddings",
            "id,airtable_proposal_id,project_title,won,content_chunk,metadata",
        )
        winners = []
        for row in rows:
            meta = _as_dict(row.get("metadata"))
            won = row.get("won")
            if won is None:
                won = meta.get("won")
            if won is True:
                winners.append(row)
        winners.sort(key=lambda row: str(_as_dict(row.get("metadata")).get("year") or ""), reverse=True)
        return [_proposal_row(row) for row in winners[:limit]]
    except Exception as e:
        logger.warning(f"get_winning_proposals failed: {e}")
        return []


def list_crm_opportunities(statuses: tuple[str, ...] | None = None) -> list[dict]:
    """Processing rows shaped like the old opportunity records: {id, fields}."""
    try:
        rows = _all_rows(
            "opportunity_processing",
            "source_url,title,crm_status,submission_deadline,crm_fields",
        )
        records = [_crm_record(row) for row in rows if row.get("source_url")]
        if statuses is None:
            return records
        allowed = set(statuses)
        return [row for row in records if (row.get("fields") or {}).get("status") in allowed]
    except Exception as e:
        logger.warning(f"list_crm_opportunities failed: {e}")
        return []


def log_agent_action(
    action_type: str,
    opportunity_id: str,
    description: str,
    tokens_used: int = 0,
    status: str = "Success",
    error_message: str = "",
    cost_usd: float | None = None,
) -> None:
    """Write one agent log. A failed insert must never raise."""
    try:
        payload = {
            "log_id": f"LOG-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "action_type": action_type or "",
            "opportunity_id": opportunity_id or "",
            "description": (description or "")[:10000],
            "tokens_used": int(tokens_used or 0),
            "status": status or "",
            "error_message": (error_message or "")[:5000],
        }
        if cost_usd is not None:
            payload["cost_usd"] = float(cost_usd)
        _sb().table("agent_logs").insert(payload).execute()
    except Exception as e:
        logger.warning(f"log_agent_action skipped: {e}")


def consultant_name_key(full_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (full_name or "").lower()).strip()


def upsert_consultant(fields: dict) -> str | None:
    """Insert or refresh one person. Never raises. Returns the consultant id."""
    try:
        key = consultant_name_key(fields.get("full_name") or "")
        if not key:
            return None
        payload = {
            "full_name": fields.get("full_name") or "",
            "role_title": fields.get("role_title") or "",
            "education": fields.get("education") or "",
            "seniority_level": fields.get("seniority_level") or "",
            "based_in": fields.get("based_in") or "",
            "key_skills": fields.get("key_skills") or "",
            "key_assignments": fields.get("key_assignments") or "",
            "cv_text": fields.get("cv_text") or "",
            "thematic_expertise": fields.get("thematic_expertise") or [],
            "geographic_experience": fields.get("geographic_experience") or [],
            "languages": fields.get("languages") or [],
            "tools": fields.get("tools") or [],
        }
        if fields.get("years_experience") not in (None, ""):
            payload["years_experience"] = fields.get("years_experience")
        if fields.get("day_rate_usd") not in (None, ""):
            payload["day_rate_usd"] = fields.get("day_rate_usd")
        if fields.get("availability_status"):
            payload["availability_status"] = fields.get("availability_status")
        if fields.get("availability_percentage") not in (None, ""):
            payload["availability_percentage"] = fields.get("availability_percentage")
        updated = _date_or_none(fields.get("cv_last_updated"))
        if updated:
            payload["cv_last_updated"] = updated

        existing = (
            _sb()
            .table("consultants")
            .select("id,legacy_ids")
            .eq("name_key", key)
            .limit(1)
            .execute()
        )
        row = (existing.data or [None])[0]
        if row:
            legacy = [str(item) for item in (row.get("legacy_ids") or []) if item]
            extra = fields.get("id")
            if extra and str(extra) not in legacy:
                legacy.append(str(extra))
            if str(row["id"]) not in legacy:
                legacy.append(str(row["id"]))
            payload["legacy_ids"] = legacy
            _sb().table("consultants").update(payload).eq("id", row["id"]).execute()
            return str(row["id"])

        consultant_id = str(uuid.uuid4())
        payload["id"] = consultant_id
        payload["name_key"] = key
        payload["legacy_ids"] = [consultant_id]
        _sb().table("consultants").insert(payload).execute()
        return consultant_id
    except Exception as e:
        logger.warning(f"upsert_consultant skipped: {e}")
        return None


def upsert_rate_card(role_level: str, location: str, day_rate_usd, per_diem_usd=None) -> bool:
    """Replace one rate. Never invents a price and never raises."""
    try:
        payload = {
            "role_level": role_level,
            "location": location,
            "day_rate_usd": day_rate_usd,
            "per_diem_usd": per_diem_usd,
            "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        _sb().table("rate_cards").upsert(payload, on_conflict="role_level,location").execute()
        return True
    except Exception as e:
        logger.warning(f"upsert_rate_card skipped: {e}")
        return False
