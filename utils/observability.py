"""Pipeline execution_id, structured stage logs, token/cost recording.

Costs are ESTIMATED from published list prices in config. If a model has
no price row, estimated_cost_usd is None (UNKNOWN) — never fabricated.
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from typing import Optional

from loguru import logger

from utils.errors import ErrorType

execution_id_var: ContextVar[str] = ContextVar("execution_id", default="")
stage_var: ContextVar[str] = ContextVar("stage", default="")

# Per-opportunity token totals for the current process_opportunity call.
_opp_tokens: ContextVar[dict] = ContextVar("opp_tokens", default=None)


def new_execution_id() -> str:
    eid = str(uuid.uuid4())
    execution_id_var.set(eid)
    return eid


def get_execution_id() -> str:
    return execution_id_var.get("")


def set_stage(stage: str) -> None:
    stage_var.set(stage or "")


def reset_opportunity_usage() -> None:
    _opp_tokens.set({"input": 0, "output": 0, "estimated_cost_usd": 0.0, "cost_known": False})


def opportunity_usage() -> dict:
    return dict(_opp_tokens.get() or {})


def configure_logging() -> None:
    """Inject execution_id/stage into every loguru record. Idempotent enough to call at startup."""

    def _patch(record):
        extra = record["extra"]
        extra.setdefault("execution_id", execution_id_var.get("") or "-")
        extra.setdefault("stage", stage_var.get("") or "-")
        extra.setdefault("error_type", "")
        extra.setdefault("duration_s", "")
        extra.setdefault("model", "")
        extra.setdefault("estimated_cost_usd", "")

    logger.configure(
        extra={
            "execution_id": "-",
            "stage": "-",
            "error_type": "",
            "duration_s": "",
            "model": "",
            "estimated_cost_usd": "",
        },
        patcher=_patch,
    )


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    """ESTIMATED USD from config.OPENAI_PRICING_PER_MTOK. None if unknown."""
    from config import OPENAI_PRICING_PER_MTOK

    prices = OPENAI_PRICING_PER_MTOK.get(model)
    if not prices:
        return None
    inp = prices.get("input")
    out = prices.get("output")
    if inp is None or out is None:
        return None
    return (input_tokens / 1_000_000) * inp + (output_tokens / 1_000_000) * out


def record_usage(response, model: str, stage: str = "") -> dict:
    """Read OpenAI usage if present. Does not invent token counts."""
    from utils.llm import cached_tokens, usage_totals

    input_tokens, output_tokens = usage_totals(response)
    cached = cached_tokens(response)
    cost = estimate_cost_usd(model, input_tokens, output_tokens) if (input_tokens or output_tokens) else None

    bucket = _opp_tokens.get()
    if bucket is not None:
        bucket["input"] += input_tokens
        bucket["output"] += output_tokens
        if cost is not None:
            bucket["estimated_cost_usd"] = (bucket.get("estimated_cost_usd") or 0) + cost
            bucket["cost_known"] = True

    payload = {
        "stage": stage or stage_var.get(""),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached,
        "estimated_cost_usd": cost,
        "cost_basis": "ESTIMATED" if cost is not None else "UNKNOWN",
    }
    logger.bind(
        stage=payload["stage"],
        model=model,
        estimated_cost_usd=round(cost, 6) if cost is not None else "",
    ).info(
        f"llm_usage stage={payload['stage']} model={model} "
        f"in={input_tokens} out={output_tokens} cached={cached} "
        f"est_cost_usd={cost if cost is not None else 'UNKNOWN'}"
    )
    return payload


def log_stage(
    stage: str,
    status: str,
    duration_s: float | None = None,
    model: str = "",
    error_type: str = "",
    **fields,
) -> None:
    """Structured stage line. Never logs secrets or full document dumps."""
    set_stage(stage)
    extras = {
        "stage": stage,
        "model": model or "",
        "error_type": error_type or "",
        "duration_s": round(duration_s, 3) if duration_s is not None else "",
    }
    parts = [f"stage={stage}", f"status={status}"]
    if duration_s is not None:
        parts.append(f"duration_s={duration_s:.3f}")
    if model:
        parts.append(f"model={model}")
    if error_type:
        parts.append(f"error_type={error_type}")
    for key, value in fields.items():
        if value is None or value == "":
            continue
        # Hard cap so a ToR dump cannot land in the log line.
        text = str(value)
        if len(text) > 200:
            text = text[:200] + "…"
        parts.append(f"{key}={text}")
    logger.bind(**extras).info(" ".join(parts))


class StageTimer:
    def __init__(self, stage: str):
        self.stage = stage
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.monotonic()
        set_stage(self.stage)
        return self

    def __exit__(self, exc_type, exc, tb):
        duration = time.monotonic() - self._t0
        if exc is None:
            log_stage(self.stage, "ok", duration_s=duration)
        else:
            err = getattr(exc, "error_type", None) or ErrorType.INGESTION_ERROR
            if not isinstance(err, str):
                err = ErrorType.INGESTION_ERROR
            log_stage(self.stage, "error", duration_s=duration, error_type=str(err))
        return False
