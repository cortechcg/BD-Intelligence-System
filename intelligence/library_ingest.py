"""One ingest path for past proposals and CVs.

The CLI (`embed_cvs.py`) and the dashboard worker both call these functions.
The web process must not import this module: it has no embedding key.
Extracted text is data. Callers store it; they do not render it as HTML.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import date
from loguru import logger

from config import MAX_DOCUMENT_BYTES, MAX_DOCUMENT_UNCOMPRESSED_BYTES, MAX_EXTRACTED_TEXT_CHARS
from processors.document_quality import assess_extraction
from processors.downloader import extract_text_from_docx, extract_text_from_pdf

_NAME_KEY = re.compile(r"[^a-zA-Z0-9]+")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

MIN_CV_TEXT_CHARS = 50
MIN_PROPOSAL_TEXT_CHARS = 100


def embedding_model() -> str:
    """The same constant `database` embeddings use. No second model string."""
    from embed_cvs import EMBEDDING_MODEL

    return EMBEDDING_MODEL


def name_key(full_name: str) -> str:
    """Same expression as supabase_migration_leave_airtable.sql, trimmed.

    The SQL is lower(regexp_replace(name, '[^a-zA-Z0-9]+', ' ', 'g')).
    Trim avoids a second row that differs only by a trailing space.
    """
    return _NAME_KEY.sub(" ", full_name or "").lower().strip()


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename(name: str) -> str:
    base = (name or "document").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = _SAFE_NAME.sub("-", base).strip(".-") or "document"
    return cleaned[:120]


def detect_type(name: str, head_bytes: bytes) -> str | None:
    """Return pdf or docx only when the extension and the magic bytes agree."""
    suffix = ""
    lower = (name or "").lower()
    if "." in lower:
        suffix = lower.rsplit(".", 1)[-1]
    head = head_bytes or b""
    if suffix == "pdf" and head.startswith(b"%PDF-"):
        return "pdf"
    if suffix == "docx" and _is_docx(head_bytes):
        return "docx"
    return None


def _is_docx(data: bytes) -> bool:
    if not data or not data.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return False
    return "word/document.xml" in names


def plain_extract_error(exc: Exception, kind: str) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "password" in text or "encrypted" in text:
        return "File is password-protected"
    if kind == "pdf":
        return "Scanned PDF with no text layer"
    return "The file could not be read"


def extract_text(kind: str, filename: str, data: bytes) -> tuple[str, dict]:
    """Return (text, meta). meta.ok is false with a plain-language reason."""
    if not data:
        return "", {"ok": False, "reason": "The file is empty", "chars": 0}
    if len(data) > MAX_DOCUMENT_BYTES:
        return "", {"ok": False, "reason": "The file is larger than 25 MiB", "chars": 0}
    detected = detect_type(filename, data[:8] if not data.startswith(b"PK") else data)
    # DOCX magic needs the whole zip, not an 8-byte head.
    if kind == "docx" or (filename or "").lower().endswith(".docx"):
        detected = detect_type(filename, data)
    if detected is None:
        return "", {
            "ok": False,
            "reason": "The file extension does not match its contents",
            "chars": 0,
        }
    if detected == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                uncompressed = sum(info.file_size for info in archive.infolist())
            if uncompressed > MAX_DOCUMENT_UNCOMPRESSED_BYTES:
                return "", {
                    "ok": False,
                    "reason": "The document expands past the safety limit",
                    "chars": 0,
                }
        except zipfile.BadZipFile:
            return "", {"ok": False, "reason": "The file extension does not match its contents", "chars": 0}
    try:
        if detected == "pdf":
            text = extract_text_from_pdf(data)
        else:
            text = extract_text_from_docx(data)
    except Exception as exc:
        return "", {"ok": False, "reason": plain_extract_error(exc, detected), "chars": 0}
    text = (text or "")[:MAX_EXTRACTED_TEXT_CHARS]
    if not text.strip():
        reason = "Scanned PDF with no text layer" if detected == "pdf" else "The file has no text layer"
        return "", {"ok": False, "reason": reason, "chars": 0}
    floor = MIN_CV_TEXT_CHARS if kind == "cv" else MIN_PROPOSAL_TEXT_CHARS
    quality = assess_extraction(text, source=filename, min_chars=floor)
    if not quality.get("ok"):
        reason = "Scanned PDF with no text layer" if detected == "pdf" and quality.get("chars", 0) < floor else quality.get("reason")
        return text, {"ok": False, "reason": reason or "The file could not be read", "chars": quality.get("chars", 0)}
    return text, {"ok": True, "reason": "", "chars": len(text.strip()), "detected": detected}


def embed_proposal_text(
    supabase,
    *,
    lookup: str,
    title: str,
    text: str,
    won: bool,
    metadata: dict,
) -> str:
    """Store one proposal_embeddings row. Returns the row id, or 'skipped'."""
    from embed_cvs import (
        MIN_PROPOSAL_TEXT_CHARS,
        get_embedding_openai,
        retry_on_transient,
        store_single_embedding_row,
    )
    from utils.observability import assert_under_spend_cap

    if len(text or "") < MIN_PROPOSAL_TEXT_CHARS:
        logger.warning(f"No usable proposal text for {title}. Skipping.")
        return "skipped"
    assert_under_spend_cap()
    model = embedding_model()

    def _embed_and_store():
        embedding = get_embedding_openai(text)
        _ = model
        return store_single_embedding_row(
            supabase=supabase,
            table_name="proposal_embeddings",
            lookup_column="airtable_proposal_id",
            lookup_value=lookup,
            payload={
                "airtable_proposal_id": lookup,
                "project_title": title,
                "won": bool(won),
                "content_chunk": text[:2000],
                "embedding": embedding,
                "metadata": metadata,
            },
        )

    embedding_id, _action, _dupes = retry_on_transient(_embed_and_store, label=title or lookup)
    return str(embedding_id)


def ingest_proposal(supabase, doc_row: dict, text: str) -> str:
    """Embed a library proposal. won is true only when outcome is won."""
    doc_id = str(doc_row.get("id") or "")
    metadata = {
        "library_id": doc_id,
        "client": doc_row.get("client") or "",
        "donor": doc_row.get("donor") or "",
        "country": doc_row.get("country") or "",
        "sectors": list(doc_row.get("sectors") or []),
        "type": doc_row.get("proposal_type") or "",
        "date": str(doc_row.get("submission_date") or ""),
        "uploaded_by_email": doc_row.get("uploaded_by_email") or "",
        "source_file": doc_row.get("original_filename") or "",
    }
    return embed_proposal_text(
        supabase,
        lookup=f"library:{doc_id}",
        title=(doc_row.get("title") or doc_row.get("original_filename") or "Proposal"),
        text=text,
        won=(doc_row.get("outcome") == "won"),
        metadata=metadata,
    )


def ingest_cv(supabase, doc_row: dict, consultant: dict, text: str) -> str:
    """Embed a library CV through embed_consultant_cv. Never writes a rate."""
    from embed_cvs import embed_consultant_cv
    from utils.observability import assert_under_spend_cap

    assert_under_spend_cap()
    consultant_id = str(consultant.get("id") or "")
    fields = {
        "full_name": consultant.get("full_name") or "",
        "role_title": consultant.get("role_title") or "",
        "based_in": consultant.get("based_in") or "",
        "languages": consultant.get("languages") or [],
        "thematic_expertise": consultant.get("thematic_expertise") or [],
        "geographic_experience": consultant.get("geographic_experience") or [],
        "years_experience": consultant.get("years_experience") or 0,
        "cv_text": text,
        "embedding_id": consultant.get("embedding_id") or "",
    }
    record = {"id": consultant_id, "fields": fields}
    result = embed_consultant_cv(supabase, None, record, use_openai=True, force=True)
    if result != "embedded":
        raise RuntimeError(result or "CV embedding failed")
    found = (
        supabase.table("cv_embeddings")
        .select("id")
        .eq("airtable_consultant_id", consultant_id)
        .limit(1)
        .execute()
    )
    rows = found.data or []
    embedding_id = str(rows[0]["id"]) if rows else ""
    today = date.today().isoformat()
    supabase.table("consultants").update(
        {
            "cv_text": text,
            "cv_last_updated": today,
            "embedding_id": embedding_id,
        }
    ).eq("id", consultant_id).execute()
    _log("library_cv_embedded", consultant_id)
    return consultant_id


def _log(action: str, ref: str) -> None:
    try:
        from database.airtable_client import log_agent_action

        log_agent_action(action, ref)
    except Exception:
        return
