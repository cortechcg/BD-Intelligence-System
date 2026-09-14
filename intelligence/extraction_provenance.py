"""Minimal extracted-field → source-chunk provenance (Phase 1, not a claim graph).

ADR 003 still owns past-work claim grounding and scoring evidence labels.
This module only records where an analyzer field value appears in the tender
text. Missing source or missing value → INSUFFICIENT DATA. A value that cannot
be located → UNKNOWN. Page numbers are recorded only when a PAGE marker is
actually present in the matched chunk — never invented.
"""

from __future__ import annotations

import re
from typing import Any, Optional

STATUS_VERIFIED = "VERIFIED"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_INSUFFICIENT = "INSUFFICIENT DATA"

SOURCE_FILE_RE = re.compile(
    r"===== SOURCE FILE:\s*(.+?) =====",
    re.IGNORECASE,
)
PAGE_MARKER_RE = re.compile(r"^----- PAGE (\d+) -----\s*$")
PAGE_ANYWHERE_RE = re.compile(r"----- PAGE (\d+) -----")

_MAX_EXCERPT = 240
_MIN_NEEDLE = 3


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict)) and not value:
        return True
    return False


def _empty_record(field: str, value: Any, status: str) -> dict:
    return {
        "field": field,
        "value": value,
        "status": status,
        "source_file": None,
        "page": None,
        "chunk_index": None,
        "excerpt": None,
    }


def _split_source_files(source_text: str) -> list[tuple[Optional[str], str]]:
    text = source_text or ""
    matches = list(SOURCE_FILE_RE.finditer(text))
    if not matches:
        return [(None, text)]
    files: list[tuple[Optional[str], str]] = []
    if matches[0].start() > 0:
        prefix = text[: matches[0].start()].strip()
        if prefix:
            files.append((None, prefix))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        name = match.group(1).strip() or None
        files.append((name, body))
    return files or [(None, text)]


def _iter_chunks(source_text: str) -> list[dict]:
    chunks: list[dict] = []
    index = 0
    for source_file, body in _split_source_files(source_text):
        current_page: Optional[int] = None
        paragraphs = re.split(r"\n{2,}", body)
        if len(paragraphs) == 1:
            paragraphs = body.split("\n")
        for raw in paragraphs:
            piece = (raw or "").strip()
            if not piece:
                continue
            page_only = PAGE_MARKER_RE.match(piece)
            if page_only:
                current_page = int(page_only.group(1))
                continue
            marker = PAGE_MARKER_RE.match(piece.split("\n", 1)[0])
            if marker:
                current_page = int(marker.group(1))
                rest = piece.split("\n", 1)[1] if "\n" in piece else ""
                piece = rest.strip()
                if not piece:
                    continue
            chunks.append({
                "source_file": source_file,
                "page": current_page,
                "chunk_index": index,
                "text": piece,
            })
            index += 1
    return chunks


def _needle_variants(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, float):
        if value.is_integer():
            number = str(int(value))
        else:
            number = f"{value:g}"
        variants = [number]
        if number.isdigit() and len(number) > 3:
            variants.append(f"{int(number):,}")
        return variants
    if isinstance(value, int):
        variants = [str(value)]
        if value >= 1000:
            variants.append(f"{value:,}")
        return variants
    text = str(value).strip()
    return [text] if text else []


def _contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    hay = haystack.lower()
    need = needle.lower()
    if len(need) >= _MIN_NEEDLE:
        return need in hay
    pattern = r"(?<![A-Za-z0-9])" + re.escape(need) + r"(?![A-Za-z0-9])"
    return re.search(pattern, hay) is not None


def _locate(value: Any, chunks: list[dict]) -> Optional[dict]:
    variants = _needle_variants(value)
    if not variants:
        return None
    for chunk in chunks:
        text = chunk.get("text") or ""
        if any(_contains(text, variant) for variant in variants):
            excerpt = text[:_MAX_EXCERPT]
            return {
                "status": STATUS_VERIFIED,
                "source_file": chunk.get("source_file"),
                "page": chunk.get("page"),
                "chunk_index": chunk.get("chunk_index"),
                "excerpt": excerpt,
            }
    return None


def sanitize_provenance_record(record: dict, source_text: Optional[str]) -> dict:
    """Drop invented page numbers and excerpts that are not in the source."""
    cleaned = _empty_record(
        str(record.get("field") or ""),
        record.get("value"),
        record.get("status") or STATUS_UNKNOWN,
    )
    status = cleaned["status"]
    if status not in {STATUS_VERIFIED, STATUS_UNKNOWN, STATUS_INSUFFICIENT}:
        status = STATUS_UNKNOWN
        cleaned["status"] = status

    source = source_text or ""
    page = record.get("page")
    if page is not None:
        try:
            page_int = int(page)
        except (TypeError, ValueError):
            page_int = None
        else:
            if page_int < 1:
                page_int = None
            elif not PAGE_ANYWHERE_RE.search(source):
                page_int = None
            elif not re.search(
                rf"----- PAGE {page_int} -----",
                source,
            ):
                page_int = None
        if page_int is None and status == STATUS_VERIFIED:
            # A VERIFIED citation that only had a fake page is not verified.
            if not record.get("excerpt") or (record.get("excerpt") or "") not in source:
                status = STATUS_UNKNOWN
                cleaned["status"] = status
        cleaned["page"] = page_int
    else:
        cleaned["page"] = None

    excerpt = record.get("excerpt")
    if isinstance(excerpt, str) and excerpt.strip() and source:
        if excerpt.strip() not in source and not _contains(source, excerpt.strip()[:40]):
            cleaned["excerpt"] = None
            if status == STATUS_VERIFIED:
                cleaned["status"] = STATUS_UNKNOWN
        else:
            cleaned["excerpt"] = excerpt.strip()[:_MAX_EXCERPT]
    else:
        cleaned["excerpt"] = None

    source_file = record.get("source_file")
    if isinstance(source_file, str) and source_file.strip() and source:
        if source_file.strip() not in source and not SOURCE_FILE_RE.search(source):
            cleaned["source_file"] = None
        else:
            cleaned["source_file"] = source_file.strip()
    else:
        cleaned["source_file"] = None

    index = record.get("chunk_index")
    if isinstance(index, int) and index >= 0:
        cleaned["chunk_index"] = index
    else:
        cleaned["chunk_index"] = None

    if not source:
        cleaned["status"] = STATUS_INSUFFICIENT
        cleaned["page"] = None
        cleaned["excerpt"] = None
        cleaned["source_file"] = None
        cleaned["chunk_index"] = None
    return cleaned


def attach_extraction_provenance(
    analysis: dict,
    source_text: Optional[str] = None,
) -> dict:
    """Mutate analysis with extraction_provenance. Never invents page numbers."""
    if not isinstance(analysis, dict):
        return analysis

    chunks = _iter_chunks(source_text or "") if source_text else []
    records: dict[str, dict] = {}

    def add(field: str, value: Any) -> None:
        if _blank(value):
            records[field] = _empty_record(field, value if value not in ("", []) else None,
                                           STATUS_INSUFFICIENT)
            return
        if not source_text or not chunks:
            records[field] = _empty_record(field, value, STATUS_INSUFFICIENT)
            return
        located = _locate(value, chunks)
        if located is None:
            records[field] = _empty_record(field, value, STATUS_UNKNOWN)
            return
        records[field] = {
            "field": field,
            "value": value,
            **located,
        }

    opportunity = analysis.get("opportunity") if isinstance(analysis.get("opportunity"), dict) else {}
    for key in (
        "title", "client", "donor", "reference_number", "submission_deadline",
        "project_duration", "estimated_budget_usd", "currency",
    ):
        add(f"opportunity.{key}", opportunity.get(key))
    for key in ("project_location", "lots_or_sites", "target_groups"):
        items = opportunity.get(key) or []
        if not isinstance(items, list) or not items:
            add(f"opportunity.{key}", None if not items else items)
            continue
        for index, item in enumerate(items):
            add(f"opportunity.{key}[{index}]", item)

    requirements = analysis.get("requirements") if isinstance(analysis.get("requirements"), dict) else {}
    for key in (
        "thematic_areas", "geographic_experience",
        "language_requirements", "certifications",
    ):
        items = requirements.get(key) or []
        if not isinstance(items, list) or not items:
            add(f"requirements.{key}", None if not items else items)
            continue
        for index, item in enumerate(items):
            add(f"requirements.{key}[{index}]", item)

    team = analysis.get("team_requirements") or []
    if isinstance(team, list):
        for index, row in enumerate(team):
            if not isinstance(row, dict):
                continue
            add(f"team_requirements[{index}].role", row.get("role"))
            add(
                f"team_requirements[{index}].estimated_days_of_effort",
                row.get("estimated_days_of_effort"),
            )

    criteria = analysis.get("evaluation_criteria") or []
    if isinstance(criteria, list):
        for index, row in enumerate(criteria):
            if not isinstance(row, dict):
                continue
            add(f"evaluation_criteria[{index}].criterion", row.get("criterion"))
            add(
                f"evaluation_criteria[{index}].weight_percent",
                row.get("weight_percent"),
            )

    analysis["extraction_provenance"] = records
    return analysis
