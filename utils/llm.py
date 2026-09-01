# utils/llm.py
"""OpenAI chat wrapper. Call sites must not import the OpenAI SDK."""

from __future__ import annotations

from typing import Any, Optional

from openai import OpenAI

from config import (
    OPENAI_MAX_RETRIES,
    OPENAI_TIMEOUT_SECONDS,
    get_openai_api_key,
)

_CLIENT_CACHE: dict[str, OpenAI] = {}


def get_openai_client(*, timeout: float | None = None, max_retries: int | None = None) -> OpenAI:
    """Shared OpenAI client — always uses the sanitized key from .env.

    Cached per (api_key, timeout, retries) so repeated calls reuse one HTTP
    pool; a rotated key in .env still produces a fresh client.
    """
    api_key = get_openai_api_key()
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is not set. Add it to .env and restart the terminal."
        )
    to = OPENAI_TIMEOUT_SECONDS if timeout is None else timeout
    retries = OPENAI_MAX_RETRIES if max_retries is None else max_retries
    cache_key = f"{api_key}:{to}:{retries}"
    client = _CLIENT_CACHE.get(cache_key)
    if client is None:
        client = OpenAI(api_key=api_key, timeout=to, max_retries=retries)
        _CLIENT_CACHE[cache_key] = client
    return client


def system_text(system: Any) -> Optional[str]:
    """Flatten a string or Anthropic-style text-block list into one system prompt."""
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


def complete(
    model: str,
    messages: list[dict],
    system: Any = None,
    max_tokens: int = 8192,
    timeout: float | None = None,
    max_retries: int | None = None,
):
    """One Chat Completions call. GPT-5 family uses max_completion_tokens."""
    client = get_openai_client(timeout=timeout, max_retries=max_retries)
    msgs: list[dict] = []
    sys_prompt = system_text(system)
    if sys_prompt:
        msgs.append({"role": "system", "content": sys_prompt})
    msgs.extend(messages)
    return client.chat.completions.create(
        model=model,
        messages=msgs,
        max_completion_tokens=max_tokens,
    )


def get_text(response) -> str:
    """Extract assistant text from an OpenAI Chat Completions response."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        # Leftover Anthropic-shaped objects (tests / old caches).
        content = getattr(response, "content", None) or []
        for block in content:
            if getattr(block, "type", None) == "text":
                return block.text or ""
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
    """OpenAI finish_reason, or Anthropic stop_reason if present."""
    choices = getattr(response, "choices", None) or []
    if choices:
        return getattr(choices[0], "finish_reason", None) or ""
    return getattr(response, "stop_reason", None) or ""


def output_tokens(response) -> int:
    _, out = usage_totals(response)
    return out


def cached_tokens(response) -> int:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    return int(getattr(details, "cached_tokens", 0) or 0)


def usage_totals(response) -> tuple[int, int]:
    """(input_tokens, output_tokens) from OpenAI or Anthropic usage objects."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    inp = int(
        getattr(usage, "prompt_tokens", None)
        or getattr(usage, "input_tokens", None)
        or 0
    )
    out = int(
        getattr(usage, "completion_tokens", None)
        or getattr(usage, "output_tokens", None)
        or 0
    )
    return inp, out
