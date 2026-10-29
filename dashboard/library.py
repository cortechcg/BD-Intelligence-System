"""Knowledge Library uploads, permissions, and the worker ingest step.

The web functions store a file and queue `library_ingest`. They do not
extract or embed. `run_library_ingest` runs on the worker only.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

from config import MAX_DOCUMENT_BYTES
from dashboard import settings, triggers
from intelligence.library_ingest import content_hash, detect_type, name_key, safe_filename

BUCKET = "Cortech-documents"
MAX_FILES = 10
PREFIX = {"proposal": "proposals", "cv": "cvs"}
_CONSULTANT_FIELDS = (
    "full_name",
    "role_title",
    "based_in",
    "languages",
    "thematic_expertise",
    "geographic_experience",
    "years_experience",
    "email",
    "created_by_email",
    "name_key",
)
_NEVER = ("day_rate_usd", "availability_status", "availability_percentage")


def library_source(document_id: str) -> str:
    return f"library://{document_id}"


def document_id_from_source(source_url: str) -> str | None:
    raw = (source_url or "").strip()
    if not raw.startswith("library://"):
        return None
    token = raw[len("library://"):].strip()
    return token or None


def actor(session: dict) -> tuple[str, str]:
    """Identity comes from the session, never from a form field."""
    email = str((session or {}).get("email") or "").strip().lower()
    name = str((session or {}).get("name") or "").strip()
    return email, name


def is_admin(session: dict) -> bool:
    email, _name = actor(session)
    return bool(email) and email in settings.library_admin_emails()


def can_archive(session: dict, row: dict) -> bool:
    email, _name = actor(session)
    if not email or not row or row.get("archived_at"):
        return False
    if is_admin(session):
        return True
    return email == str(row.get("uploaded_by_email") or "").strip().lower()


def validate_file(filename: str, data: bytes) -> dict:
    if not data:
        return {"ok": False, "reason": "The file is empty"}
    if len(data) > MAX_DOCUMENT_BYTES:
        return {"ok": False, "reason": "The file is larger than 25 MiB"}
    detected = detect_type(filename, data if (filename or "").lower().endswith(".docx") else data[:8])
    if (filename or "").lower().endswith(".docx"):
        detected = detect_type(filename, data)
    if detected is None:
        return {"ok": False, "reason": "The file extension does not match its contents"}
    return {
        "ok": True,
        "detected": detected,
        "content_hash": content_hash(data),
        "safe_name": safe_filename(filename),
        "size_bytes": len(data),
    }


_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d/."), "-")
_MONTHS = {
    name: number
    for number, names in enumerate(
        (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")),
        start=1,
    )
    for name in names
}


def _clean_date_text(value: str) -> str:
    """What the person meant, without what the clipboard or keyboard added.

    A pasted date can carry invisible direction marks (U+200E/U+200F) or a
    zero-width space, and an autocorrecting keyboard swaps '-' for a
    non-breaking hyphen or an en dash. All of them display as 2026-10-29.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = text.translate(_DASHES)
    return " ".join(text.split()).strip(" ,")


def _date_or_none(value: str) -> str | None:
    """ISO date for submission_date, or None when left empty.

    Accepts YYYY-MM-DD (what the date picker sends), YYYY-M-D, YYYYMMDD,
    DD-MM-YYYY or DD/MM/YYYY (day first, the convention in Kenya and Somalia;
    month first only when the day-first reading is impossible), and
    '29 October 2026' / 'Oct 29, 2026'.
    """
    raw = _clean_date_text(value)
    if not raw:
        return None
    candidates: list[tuple[int, int, int]] = []
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw):
        candidates.append((int(m[1]), int(m[2]), int(m[3])))
    elif m := re.fullmatch(r"(\d{4})(\d{2})(\d{2})", raw):
        candidates.append((int(m[1]), int(m[2]), int(m[3])))
    elif m := re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", raw):
        first, second, year = int(m[1]), int(m[2]), int(m[3])
        candidates.append((year, second, first))
        candidates.append((year, first, second))
    else:
        words = re.findall(r"[a-z]+|\d+", raw.lower())
        months = [_MONTHS[w] for w in words if w in _MONTHS]
        numbers = [int(w) for w in words if w.isdigit()]
        years = [n for n in numbers if n >= 1900]
        days = [n for n in numbers if n < 1900]
        if len(months) == 1 and len(years) == 1 and len(days) == 1:
            candidates.append((years[0], months[0], days[0]))
    for year, month, day in candidates:
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    raise ValueError(
        f"Submission date '{' '.join(str(value).split())[:40]}' is not a date. Use the date picker, or type it as "
        "YYYY-MM-DD (for example 2026-10-29), or leave it empty"
    )


def _tags(value) -> list[str]:
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value or "").replace(";", ",").split(",")
    out = []
    for part in parts:
        text = str(part).strip()
        if text and text not in out:
            out.append(text[:80])
    return out[:24]


def consultant_insert_payload(fields: dict, created_by_email: str) -> dict:
    """Only the fields a person typed. Rates and availability stay absent."""
    full_name = str(fields.get("full_name") or "").strip()
    if not full_name:
        raise ValueError("A full name is required")
    payload: dict[str, Any] = {
        "full_name": full_name,
        "name_key": name_key(full_name),
        "role_title": str(fields.get("role_title") or "").strip(),
        "based_in": str(fields.get("based_in") or "").strip(),
        "languages": _tags(fields.get("languages")),
        "thematic_expertise": _tags(fields.get("thematic_expertise") or fields.get("sectors")),
        "geographic_experience": _tags(fields.get("geographic_experience")),
        "email": str(fields.get("email") or "").strip().lower(),
        "created_by_email": created_by_email,
    }
    years = fields.get("years_experience")
    if years not in (None, "", "None"):
        payload["years_experience"] = float(years)
    for banned in _NEVER:
        payload.pop(banned, None)
    unknown = set(payload) - set(_CONSULTANT_FIELDS)
    if unknown:
        raise ValueError(f"unexpected consultant fields: {sorted(unknown)}")
    return payload


def _rows(response) -> list[dict]:
    data = getattr(response, "data", response)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def find_consultant_by_name_key(supabase, key: str) -> dict | None:
    if not key:
        return None
    found = supabase.table("consultants").select("*").eq("name_key", key).limit(1).execute()
    rows = _rows(found)
    return rows[0] if rows else None


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def check_consultant_id(supabase, consultant_id: str) -> tuple[str | None, str | None]:
    """(id, None) when the consultant exists; (None, reason) otherwise.

    The 'Someone on the team' box takes a value from the list. A name typed
    into it is not an id, and used to fail later as an opaque insert error.
    """
    value = (consultant_id or "").strip()
    if not _UUID.match(value):
        return None, (
            f"'{value[:60]}' is not on the team list. Pick the consultant from the "
            "suggestions, or choose New team member"
        )
    found = supabase.table("consultants").select("id").eq("id", value).limit(1).execute()
    if not _rows(found):
        return None, "That consultant is not in the team list any more. Pick another, or choose New team member"
    return value, None


def find_consultant_by_email(supabase, email: str) -> dict | None:
    addr = (email or "").strip().lower()
    if not addr:
        return None
    found = supabase.table("consultants").select("*").eq("email", addr).limit(1).execute()
    rows = _rows(found)
    return rows[0] if rows else None


def near_consultant_names(supabase, full_name: str, limit: int = 3) -> list[str]:
    key = name_key(full_name)
    if len(key) < 3:
        return []
    found = supabase.table("consultants").select("full_name,name_key").limit(200).execute()
    hits = []
    for row in _rows(found):
        other = str(row.get("name_key") or "")
        if other and other != key and (key in other or other in key):
            hits.append(str(row.get("full_name") or other))
    return hits[:limit]


def create_consultant(supabase, fields: dict, created_by_email: str, *, confirm: bool = False) -> dict:
    payload = consultant_insert_payload(fields, created_by_email)
    existing = find_consultant_by_name_key(supabase, payload["name_key"])
    if existing:
        return {"consultant": existing, "created": False, "did_you_mean": []}
    suggestions = near_consultant_names(supabase, payload["full_name"])
    if suggestions and not confirm:
        return {"consultant": None, "created": False, "did_you_mean": suggestions}
    for banned in _NEVER:
        if banned in payload:
            raise RuntimeError(banned)
    inserted = supabase.table("consultants").insert(payload).execute()
    rows = _rows(inserted)
    if not rows:
        raise RuntimeError("consultant was not stored")
    return {"consultant": rows[0], "created": True, "did_you_mean": []}


def resolve_me(supabase, session: dict) -> dict:
    email, name = actor(session)
    found = find_consultant_by_email(supabase, email)
    if found:
        return found
    if name:
        found = find_consultant_by_name_key(supabase, name_key(name))
        if found:
            return found
    created = create_consultant(
        supabase,
        {"full_name": name or email, "email": email},
        email,
        confirm=True,
    )
    return created["consultant"]


def _event(supabase, document_id: str | None, email: str, action: str, detail: dict) -> None:
    try:
        supabase.table("library_events").insert(
            {
                "document_id": document_id,
                "actor_email": email,
                "action": action,
                "detail": detail or {},
            }
        ).execute()
    except Exception:
        return


def _log(action: str, ref: str) -> None:
    try:
        from database.airtable_client import log_agent_action

        log_agent_action(action, ref)
    except Exception:
        return


def find_live_duplicate(supabase, kind: str, digest: str) -> dict | None:
    found = (
        supabase.table("library_documents")
        .select("id,title,status")
        .eq("kind", kind)
        .eq("content_hash", digest)
        .is_("archived_at", "null")
        .limit(1)
        .execute()
    )
    rows = _rows(found)
    return rows[0] if rows else None


def _mime(detected: str) -> str:
    if detected == "pdf":
        return "application/pdf"
    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def store_upload(
    supabase,
    session: dict,
    *,
    kind: str,
    filename: str,
    data: bytes,
    meta: dict,
    consultant_id: str | None = None,
) -> dict:
    """Validate, store, and queue one file. Does not embed."""
    email, uploader_name = actor(session)
    if not email:
        return {"ok": False, "reason": "Sign in required"}
    if kind not in ("proposal", "cv"):
        return {"ok": False, "reason": "Choose a past proposal or a CV"}
    if kind == "cv" and not consultant_id:
        return {"ok": False, "reason": "Choose whose CV this is"}
    checked = validate_file(filename, data)
    if not checked.get("ok"):
        return {"ok": False, "filename": filename, "reason": checked.get("reason")}
    existing = find_live_duplicate(supabase, kind, checked["content_hash"])
    if existing:
        return {
            "ok": True,
            "duplicate": True,
            "filename": filename,
            "existing_id": existing.get("id"),
            "reason": "Already in the library",
        }
    if not settings.TRIGGERS_ENABLED:
        return {"ok": False, "filename": filename, "reason": "Read-only: new runs are paused"}

    doc_id = _new_id()
    storage_path = f"{PREFIX[kind]}/{doc_id}-{checked['safe_name']}"
    title = str(meta.get("title") or "").strip() or _title_from_filename(filename)
    try:
        submission_date = _date_or_none(str(meta.get("submission_date") or ""))
    except ValueError as exc:
        return {"ok": False, "filename": filename, "reason": str(exc)}
    proposal_type = str(meta.get("proposal_type") or "").strip().lower() or None
    if proposal_type and proposal_type not in ("technical", "eoi", "financial", "other"):
        return {"ok": False, "filename": filename, "reason": "Unknown proposal type"}
    outcome = str(meta.get("outcome") or "unknown").strip().lower() or "unknown"
    if outcome not in ("won", "lost", "pending", "unknown"):
        outcome = "unknown"
    row = {
        "id": doc_id,
        "kind": kind,
        "title": title,
        "original_filename": safe_filename(filename),
        "storage_path": storage_path,
        "mime_type": _mime(checked["detected"]),
        "size_bytes": checked["size_bytes"],
        "content_hash": checked["content_hash"],
        "uploaded_by_email": email,
        "uploaded_by_name": uploader_name,
        "consultant_id": consultant_id if kind == "cv" else None,
        "client": str(meta.get("client") or "").strip() or None,
        "donor": str(meta.get("donor") or "").strip() or None,
        "country": str(meta.get("country") or "").strip() or None,
        "sectors": _tags(meta.get("sectors")),
        "submission_date": submission_date,
        "proposal_type": proposal_type,
        "outcome": outcome if kind == "proposal" else "unknown",
        "status": "queued",
    }
    try:
        supabase.storage.from_(BUCKET).upload(
            storage_path,
            data,
            {"content-type": row["mime_type"], "upsert": "false"},
        )
    except Exception as exc:
        detail = str(getattr(exc, "message", "") or exc)[:160]
        return {"ok": False, "filename": filename, "reason": f"The file was not stored ({type(exc).__name__}: {detail})"}
    try:
        supabase.table("library_documents").insert(row).execute()
    except Exception as exc:
        text = str(exc).lower()
        if "23505" in text or "duplicate" in text:
            again = find_live_duplicate(supabase, kind, checked["content_hash"])
            return {
                "ok": True,
                "duplicate": True,
                "filename": filename,
                "existing_id": (again or {}).get("id"),
                "reason": "Already in the library",
            }
        code = getattr(exc, "code", "") or ""
        detail = str(getattr(exc, "message", "") or "")[:160]
        return {
            "ok": False,
            "filename": filename,
            "reason": "The library row was not stored"
            + (f" (database {code}: {detail})" if code or detail else f" ({type(exc).__name__})"),
        }
    queued = triggers.enqueue(library_source(doc_id), triggers.LIBRARY_INGEST, email)
    _event(supabase, doc_id, email, "uploaded", {"filename": row["original_filename"]})
    _log("library_uploaded", doc_id)
    if not queued:
        _set_status(supabase, doc_id, "failed", "A library ingest is already in flight for this document")
        return {
            "ok": False,
            "filename": filename,
            "document_id": doc_id,
            "reason": "This document is already queued",
        }
    return {"ok": True, "filename": filename, "document_id": doc_id, "status": "queued"}


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _title_from_filename(filename: str) -> str:
    stem = safe_filename(filename).rsplit(".", 1)[0]
    return stem.replace("-", " ").replace("_", " ").strip() or "Untitled"


def _set_status(supabase, document_id: str, status: str, message: str = "", *, extra: dict | None = None) -> None:
    payload = {"status": status, "updated_at": _now()}
    if message:
        payload["error_message"] = message[:500]
    if extra:
        payload.update(extra)
    supabase.table("library_documents").update(payload).eq("id", document_id).execute()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


CENSUS_LIMIT = 5000


def list_documents(
    supabase,
    *,
    tab: str = "proposals",
    filters: dict | None = None,
    limit: int = 200,
) -> list[dict]:
    filters = filters or {}
    query = supabase.table("library_documents").select("*").is_("archived_at", "null")
    if tab == "cvs":
        query = query.eq("kind", "cv")
    elif tab == "proposals":
        query = query.eq("kind", "proposal")
    elif tab == "mine" and filters.get("uploader"):
        query = query.eq("uploaded_by_email", filters["uploader"])
    elif tab == "processing":
        query = query.in_("status", ["uploaded", "queued", "extracting", "embedding"])
    # tab == "all" keeps every kind; that list is the KPI census, not a tab.
    for key, column in (
        ("donor", "donor"),
        ("country", "country"),
        ("outcome", "outcome"),
        ("uploader", "uploaded_by_email"),
        ("consultant", "consultant_id"),
        ("status", "status"),
    ):
        if tab == "mine" and key == "uploader":
            continue
        if filters.get(key):
            query = query.eq(column, filters[key])
    found = query.order("created_at", desc=True).limit(limit).execute()
    rows = _rows(found)
    needle = str(filters.get("q") or "").strip().lower()
    sector = str(filters.get("sector") or "").strip().lower()
    if sector:
        rows = [
            row for row in rows
            if sector in " ".join(str(item) for item in (row.get("sectors") or [])).lower()
        ]
    if needle:
        rows = [
            row for row in rows
            if needle in " ".join(
                str(row.get(field) or "")
                for field in ("title", "client", "donor", "country", "original_filename")
            ).lower()
        ]
    return rows


def counts(rows: list[dict]) -> dict:
    proposals = [row for row in rows if row.get("kind") == "proposal"]
    cvs = [row for row in rows if row.get("kind") == "cv"]
    processing = {"uploaded", "queued", "extracting", "embedding"}
    ready_cvs = [row for row in cvs if row.get("status") == "ready"]
    consultants = {str(row.get("consultant_id")) for row in ready_cvs if row.get("consultant_id")}
    return {
        "proposals_ready": sum(1 for row in proposals if row.get("status") == "ready"),
        "proposals_processing": sum(1 for row in proposals if row.get("status") in processing),
        "cv_consultants": len(consultants),
        "cvs_ready": len(ready_cvs),
    }


def census_rows(supabase) -> list[dict]:
    """Slim rows for the KPI strip. Does not load file text or tab filters."""
    found = (
        supabase.table("library_documents")
        .select("kind,status,consultant_id")
        .is_("archived_at", "null")
        .limit(CENSUS_LIMIT)
        .execute()
    )
    return _rows(found)


def census_counts(supabase) -> dict:
    """KPI totals across kinds. The open tab is filtered; these figures are not."""
    return counts(census_rows(supabase))


def run_library_ingest(job: dict) -> dict:
    """Worker body for one library:// trigger. Heartbeat wraps the caller."""
    from database.supabase_client import get_supabase
    from intelligence.library_ingest import extract_text, ingest_cv, ingest_proposal
    from utils.errors import ErrorType, RunHaltRequested, classify_exception
    from utils.observability import SpendCapError, reset_run_spend_cap, start_run_spend_cap

    source = str(job.get("source_url") or "")
    document_id = document_id_from_source(source)
    if not document_id:
        return _fail(ErrorType.VALIDATION_ERROR, "This job is not a library document")
    supabase = get_supabase()
    found = supabase.table("library_documents").select("*").eq("id", document_id).limit(1).execute()
    rows = _rows(found)
    if not rows:
        return _fail(ErrorType.VALIDATION_ERROR, "The library document is not in the table")
    doc = rows[0]
    if doc.get("archived_at"):
        return _fail(ErrorType.VALIDATION_ERROR, "This document is archived")

    blocked, why = triggers.aggregate_cap_reached()
    if blocked:
        _set_status(
            supabase, document_id, "failed",
            "Embedding quota reached; retry later",
            extra={"error_kind": ErrorType.SPEND_CAP_ERROR},
        )
        return _fail(ErrorType.SPEND_CAP_ERROR, "Embedding quota reached; retry later")

    start_run_spend_cap()
    try:
        blob = supabase.storage.from_(BUCKET).download(doc["storage_path"])
        data = blob if isinstance(blob, (bytes, bytearray)) else bytes(blob or b"")
        _set_status(supabase, document_id, "extracting")
        text, meta = extract_text(doc.get("kind") or "", doc.get("original_filename") or "", data)
        if not meta.get("ok"):
            reason = str(meta.get("reason") or "The file could not be read")
            _set_status(supabase, document_id, "failed", reason, extra={"error_kind": ErrorType.DOCUMENT_ERROR})
            return _fail(ErrorType.DOCUMENT_ERROR, reason)
        _set_status(
            supabase, document_id, "embedding",
            extra={"extracted_chars": int(meta.get("chars") or 0)},
        )
        if doc.get("kind") == "cv":
            consultant_rows = _rows(
                supabase.table("consultants").select("*").eq("id", doc.get("consultant_id")).limit(1).execute()
            )
            if not consultant_rows:
                _set_status(supabase, document_id, "failed", "The consultant record is missing")
                return _fail(ErrorType.VALIDATION_ERROR, "The consultant record is missing")
            ref = ingest_cv(supabase, doc, consultant_rows[0], text)
        else:
            ref = ingest_proposal(supabase, doc, text)
        if ref == "skipped":
            _set_status(supabase, document_id, "failed", "The file has no usable text")
            return _fail(ErrorType.DOCUMENT_ERROR, "The file has no usable text")
        chunk_count = 1
        _set_status(
            supabase, document_id, "ready",
            extra={
                "embedding_ref": ref,
                "chunk_count": chunk_count,
                "extracted_chars": int(meta.get("chars") or len(text)),
                "error_kind": None,
                "error_message": None,
            },
        )
        _event(supabase, document_id, str(doc.get("uploaded_by_email") or ""), "ready", {"embedding_ref": ref})
        _log("library_ready", document_id)
        return {
            "status": "succeeded",
            "error_kind": None,
            "error_message": None,
            "execution_id": "",
            "result": {"library_document_id": document_id, "embedding_ref": ref},
        }
    except RunHaltRequested:
        _set_status(supabase, document_id, "failed", "Cancelled before the embedding was stored")
        return {
            "status": "cancelled",
            "error_kind": ErrorType.CANCELLED,
            "error_message": "Cancelled before the embedding was stored",
            "execution_id": "",
            "result": {"library_document_id": document_id},
        }
    except SpendCapError:
        _set_status(supabase, document_id, "failed", "Embedding quota reached; retry later")
        return _fail(ErrorType.SPEND_CAP_ERROR, "Embedding quota reached; retry later")
    except Exception as exc:
        message = _plain_failure(exc)
        _set_status(
            supabase, document_id, "failed", message,
            extra={"error_kind": classify_exception(exc)},
        )
        return _fail(classify_exception(exc), message)
    finally:
        reset_run_spend_cap()


def _plain_failure(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "password" in text or "encrypted" in text:
        return "File is password-protected"
    if "quota" in text or "spend" in text or "openai_api_key" in text:
        return "Embedding quota reached; retry later"
    return "The file could not be added to the library"


def _fail(kind: str, message: str) -> dict:
    return {
        "status": "failed",
        "error_kind": kind,
        "error_message": message,
        "execution_id": "",
        "result": {},
    }


def archive_document(supabase, session: dict, document_id: str) -> tuple[bool, str]:
    found = supabase.table("library_documents").select("*").eq("id", document_id).limit(1).execute()
    rows = _rows(found)
    if not rows:
        return False, "That document is not in the library"
    row = rows[0]
    if not can_archive(session, row):
        return False, "You can archive a document you uploaded"
    email, _name = actor(session)
    ref = str(row.get("embedding_ref") or "")
    if ref and row.get("kind") == "proposal":
        supabase.table("proposal_embeddings").delete().eq("airtable_proposal_id", f"library:{document_id}").execute()
    if ref and row.get("kind") == "cv":
        supabase.table("cv_embeddings").delete().eq("airtable_consultant_id", str(row.get("consultant_id") or "")).execute()
    supabase.table("library_documents").update(
        {"archived_at": _now(), "archived_by": email, "updated_at": _now()}
    ).eq("id", document_id).execute()
    _event(supabase, document_id, email, "archived", {})
    _log("library_archived", document_id)
    return True, "Archived. Matching will stop using it."


def restore_document(supabase, session: dict, document_id: str) -> tuple[bool, str]:
    if not is_admin(session):
        return False, "Only a library admin can restore a document"
    email, _name = actor(session)
    supabase.table("library_documents").update(
        {"archived_at": None, "archived_by": None, "updated_at": _now()}
    ).eq("id", document_id).execute()
    _event(supabase, document_id, email, "restored", {})
    return True, "Restored. Reprocess it to embed it again."
