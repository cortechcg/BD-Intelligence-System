# utils/llm.py
"""Chat wrapper. Call sites must not import the provider SDK."""

from __future__ import annotations

import json
import re
from typing import Any

from config import get_qwen_client


def system_text(system: Any) -> str | None:
    """Flatten a string or Anthropic-style text-block list into one string.

    complete() passes system through to the API unchanged. This helper is
    for tests and for any caller that needs a concatenated view.
    """
    if system is None:
        return None
    if isinstance(system, str):
        return system or None
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, str):
                if block:
                    parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or ""
                if text:
                    parts.append(text)
            else:
                text = getattr(block, "text", "") or ""
                if text:
                    parts.append(text)
        joined = "\n\n".join(parts)
        return joined or None
    return str(system) or None


def _openai_content(content: Any) -> str:
    """Flatten Anthropic text blocks into one chat string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                if block:
                    parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or ""
                if text:
                    parts.append(text)
            else:
                text = getattr(block, "text", "") or ""
                if text:
                    parts.append(text)
        return "\n\n".join(parts)
    return str(content)


def _openai_messages(messages: list[dict], system: Any) -> list[dict]:
    out: list[dict] = []
    system_s = system_text(system)
    if system_s:
        out.append({"role": "system", "content": system_s})
    for msg in messages or []:
        out.append({
            "role": msg.get("role") or "user",
            "content": _openai_content(msg.get("content")),
        })
    return out


def complete(
    model: str,
    messages: list[dict],
    system: Any = None,
    max_tokens: int = 8192,
    timeout: float | None = None,
    max_retries: int | None = None,
    stage: str = "",
):
    """One Qwen chat call with provider-bound usage recording.

    Recording here is the lowest reliable common boundary: every chat
    request, including retries/continuations, is counted exactly once from the
    provider response rather than from guessed section-level token totals.
    """
    # Local import avoids a module-import cycle: observability reads helpers
    # from this module when it processes the provider response.
    from utils.observability import assert_under_spend_cap, record_usage

    assert_under_spend_cap()
    client = get_qwen_client(timeout=timeout, max_retries=max_retries)
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=_openai_messages(messages, system),
    )
    record_usage(response, model, stage=stage)
    return response


def get_text(response) -> str:
    """Extract assistant text from an Anthropic (or leftover OpenAI) response."""
    content = getattr(response, "content", None)
    if content:
        parts: list[str] = []
        for block in content:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", None) or "")
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        if parts:
            return "".join(parts)

    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = choices[0].message
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("text"):
                parts.append(part["text"])
            else:
                text = getattr(part, "text", None)
                if text:
                    parts.append(text)
        return "".join(parts)
    return content or ""


def finish_reason(response) -> str:
    """Anthropic stop_reason, or OpenAI finish_reason if present.

    Proposal continuation treats both ``max_tokens`` and ``length`` as a cap hit.
    """
    stop = getattr(response, "stop_reason", None)
    if stop:
        return stop
    choices = getattr(response, "choices", None) or []
    if choices:
        return getattr(choices[0], "finish_reason", None) or ""
    return ""


def output_tokens(response) -> int:
    _, out = usage_totals(response)
    return out


def cached_tokens(response) -> int:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    if cached:
        return cached
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    return int(getattr(details, "cached_tokens", 0) or 0)


def usage_totals(response) -> tuple[int, int]:
    """(input_tokens, output_tokens) from Anthropic or OpenAI usage objects."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    inp = int(
        getattr(usage, "input_tokens", None)
        or getattr(usage, "prompt_tokens", None)
        or 0
    )
    out = int(
        getattr(usage, "output_tokens", None)
        or getattr(usage, "completion_tokens", None)
        or 0
    )
    return inp, out


def loads_json_object(text: str) -> dict:
    """Parse a JSON object from model output (fences, leading prose, or raw)."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty JSON")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data
