"""Stable content hash for in-run / optional cache identity."""

import hashlib
import re


_WS = re.compile(r"\s+")


def content_hash(text: str) -> str:
    normalized = _WS.sub(" ", (text or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()
