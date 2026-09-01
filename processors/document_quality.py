"""Post-extraction quality gate. Empty/corrupt text must not look like a ToR."""

from __future__ import annotations

from utils.errors import ErrorType

MIN_USEFUL_CHARS = 200
# High replacement-character or NUL ratio ⇒ extraction garbage, not a short notice.
_MAX_REPLACEMENT_RATIO = 0.05
_MAX_CONTROL_RATIO = 0.02


def _is_google_access_wall(text: str) -> bool:
    """True when extraction is a Drive/Docs login or 'you need access' page."""
    lower = (text or "").lower()
    if "accounts.google.com" in lower:
        return True
    if "google accounts" in lower and "sign in" in lower:
        return True
    if "you need access" in lower and "google" in lower:
        return True
    if "request access" in lower and ("google drive" in lower or "google docs" in lower):
        return True
    return False


def assess_extraction(text: str, source: str = "", min_chars: int = MIN_USEFUL_CHARS) -> dict:
    """Return {ok, error_type, reason, chars}. ok=False means do not proceed."""
    raw = text or ""
    chars = len(raw.strip())
    if chars < min_chars:
        return {
            "ok": False,
            "error_type": ErrorType.DOCUMENT_ERROR,
            "reason": f"extracted text too short ({chars} chars, min {min_chars})",
            "chars": chars,
            "source": source,
        }

    sample = raw[:50000]
    if "\x00" in sample:
        return {
            "ok": False,
            "error_type": ErrorType.DOCUMENT_ERROR,
            "reason": "extracted text contains NUL bytes (corrupt binary)",
            "chars": chars,
            "source": source,
        }

    replacement = sample.count("\ufffd")
    if len(sample) and (replacement / len(sample)) > _MAX_REPLACEMENT_RATIO:
        return {
            "ok": False,
            "error_type": ErrorType.DOCUMENT_ERROR,
            "reason": "extracted text is mostly replacement characters",
            "chars": chars,
            "source": source,
        }

    control = sum(1 for c in sample if ord(c) < 9 or 11 <= ord(c) <= 31)
    if len(sample) and (control / len(sample)) > _MAX_CONTROL_RATIO:
        return {
            "ok": False,
            "error_type": ErrorType.DOCUMENT_ERROR,
            "reason": "extracted text has an abnormal control-character ratio",
            "chars": chars,
            "source": source,
        }

    letters = sum(1 for c in sample if c.isalpha())
    if letters < 40:
        return {
            "ok": False,
            "error_type": ErrorType.DOCUMENT_ERROR,
            "reason": "extracted text has almost no letters",
            "chars": chars,
            "source": source,
        }

    if _is_google_access_wall(sample):
        return {
            "ok": False,
            "error_type": ErrorType.DOCUMENT_ERROR,
            "reason": "extracted text is a Google sign-in / access-denied page, not the document",
            "chars": chars,
            "source": source,
        }

    return {
        "ok": True,
        "error_type": "",
        "reason": "ok",
        "chars": chars,
        "source": source,
    }
