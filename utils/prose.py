# utils/prose.py
"""Strip machine-looking dash separators from client-facing draft prose."""

from __future__ import annotations

import re

_HR_LINE = re.compile(r"^\s*[-*_=]{3,}\s*$")
_HASH_HEADING = re.compile(r"^\s*#{1,6}\s+")
_EM_DASH = re.compile(r"\s*[—–]\s*")
_TRIPLE_DASH = re.compile(r"-{3,}")
_DASH_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*\S)\s*$")
_DOUBLE_COMMA = re.compile(r",\s*,+")
_SPACE_COMMA = re.compile(r"\s+,")
_MULTI_SPACE = re.compile(r"[^\S\n]{2,}")


def _is_markdown_table_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    compact = stripped.replace(" ", "")
    return bool(compact) and set(compact) <= set("|:-")


def humanize_draft(text: str) -> str:
    """Make generated sections read as human Word prose.

    Removes markdown ``---`` rules, em/en dashes, and dash bullets.
    Hyphens inside words and dates (child-protection, 2024-2026) are kept.
    Markdown table separator rows are kept so the .docx builder can still
    draw tables; they are not shown as text in Word.
    """
    if not text:
        return text

    out: list[str] = []
    list_n = 0
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if _HR_LINE.match(stripped):
            continue
        if _HASH_HEADING.match(line):
            line = _HASH_HEADING.sub("", line)
            stripped = line.strip()
            if not stripped:
                continue
        if _is_markdown_table_separator(line):
            list_n = 0
            out.append(line)
            continue
        bullet = _DASH_BULLET.match(line)
        if bullet and not stripped.startswith("|"):
            list_n += 1
            indent, body = bullet.group(1), bullet.group(2)
            body = _EM_DASH.sub(", ", body)
            body = _TRIPLE_DASH.sub(", ", body)
            out.append(f"{indent}{list_n}. {body}")
            continue
        list_n = 0
        line = _EM_DASH.sub(", ", line)
        if "---" in line and not stripped.startswith("|"):
            line = _TRIPLE_DASH.sub(", ", line)
        out.append(line)

    result = "\n".join(out)
    result = _SPACE_COMMA.sub(",", result)
    result = _DOUBLE_COMMA.sub(",", result)
    result = _MULTI_SPACE.sub(" ", result)
    return result.strip() + ("\n" if text.endswith("\n") else "")
