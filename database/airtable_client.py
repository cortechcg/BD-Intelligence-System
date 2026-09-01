# database/airtable_client.py
from pyairtable import Api, retry_strategy
from config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID, TABLES
from loguru import logger
import time
import uuid
from datetime import datetime
from typing import Optional


# Fail fast. backoff_factor=5 × total=8 slept ~21 minutes PER CALL
# (5+10+20+40+80+160+320+640s) before the fail-open except ever ran.
# That hung the pipeline on availability lookup and made it look like
# proposal writing had stalled. Two short retries, then continue.
AIRTABLE_RETRY = retry_strategy(
    status_forcelist=(429, 500, 502, 503, 504),
    backoff_factor=0.5,
    total=2,
)

api = Api(AIRTABLE_API_KEY, retry_strategy=AIRTABLE_RETRY)
base = api.base(AIRTABLE_BASE_ID)

_circuit_open_until = 0.0
_CIRCUIT_SECONDS = 90


def _circuit_open() -> bool:
    return time.time() < _circuit_open_until


def _trip_circuit() -> None:
    global _circuit_open_until
    _circuit_open_until = max(_circuit_open_until, time.time() + _CIRCUIT_SECONDS)
    logger.warning(
        f"Airtable rate-limited — skipping further Airtable calls "
        f"for {_CIRCUIT_SECONDS}s so the draft can proceed"
    )


def _is_rate_limited(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg


def _note_failure(exc: Exception) -> None:
    if _is_rate_limited(exc):
        _trip_circuit()


def get_table(table_name: str):
    return base.table(TABLES[table_name])


def create_opportunity(opportunity_data: dict) -> str | None:
    """Create an opportunity record without overwriting its lifecycle state."""
    opportunity_data = dict(opportunity_data or {})
    table = get_table("opportunities")

    opportunity_data.setdefault("opportunity_id", str(uuid.uuid4()))
    opportunity_data.setdefault("discovered_at", datetime.now().strftime("%Y-%m-%d"))
    # A caller may intentionally set a lifecycle state such as Reviewing or
    # Failed. Blank remains equivalent to the normal New state.
    opportunity_data["status"] = opportunity_data.get("status") or "New"

    if _circuit_open():
        logger.warning(
            f"Airtable circuit open — not creating opportunity "
            f"'{opportunity_data.get('title')}'"
        )
        import json, os
        os.makedirs("failed_saves", exist_ok=True)
        with open(f"failed_saves/{opportunity_data['opportunity_id']}.json", "w") as f:
            json.dump(opportunity_data, f, default=str, indent=2)
        return None

    try:
        record = table.create(opportunity_data, typecast=True)
        logger.success(f"Created opportunity: {opportunity_data.get('title', 'Unknown')}")
        return record["id"]
    except Exception as e:
        _note_failure(e)
        logger.error(f"Failed to save opportunity '{opportunity_data.get('title')}' to Airtable: {e}")
        import json, os
        os.makedirs("failed_saves", exist_ok=True)
        with open(f"failed_saves/{opportunity_data['opportunity_id']}.json", "w") as f:
            json.dump(opportunity_data, f, default=str, indent=2)
        return None


def update_opportunity(record_id: str, fields: dict) -> None:
    """Update opportunity record. Never raises — a 429 must not crash the pipeline."""
    if _circuit_open():
        logger.info("Airtable circuit open — skipping opportunity update")
        return
    try:
        table = get_table("opportunities")
        table.update(record_id, fields, typecast=True)
    except Exception as e:
        _note_failure(e)
        logger.warning(f"Airtable update failed (non-fatal): {e}")


def get_all_consultants() -> list[dict]:
    """Fetch all consultant records from Airtable."""
    if _circuit_open():
        logger.info("Airtable circuit open — skipping consultant fetch")
        return []
    try:
        table = get_table("consultants")
        records = table.all()
        return [{"id": r["id"], **r["fields"]} for r in records]
    except Exception as e:
        _note_failure(e)
        logger.warning(f"Could not fetch consultants (non-fatal): {e}")
        return []


def get_consultant_by_id(airtable_id: str) -> Optional[dict]:
    """Fetch single consultant. Returns None on Airtable failure so matching continues."""
    if _circuit_open():
        return None
    try:
        table = get_table("consultants")
        record = table.get(airtable_id)
        return record["fields"] if record else None
    except Exception as e:
        _note_failure(e)
        logger.warning(f"Could not fetch consultant {airtable_id} (non-fatal): {e}")
        return None


def get_rate_card() -> list[dict]:
    """Get all rate card entries. Empty list on 429 so budget still calculates with defaults."""
    if _circuit_open():
        logger.info("Airtable circuit open — skipping rate card fetch")
        return []
    try:
        table = get_table("rate_cards")
        records = table.all()
        return [r["fields"] for r in records]
    except Exception as e:
        _note_failure(e)
        logger.warning(
            f"Could not fetch rate card from Airtable (non-fatal) — "
            f"budget will use default day rates: {e}"
        )
        return []


def get_past_proposals(limit: int = 20) -> list[dict]:
    """Get past proposals for style reference."""
    if _circuit_open():
        return []
    try:
        table = get_table("past_proposals")
        records = table.all(max_records=limit, sort=["-year"])
        return [{"id": r["id"], **r["fields"]} for r in records]
    except Exception as e:
        _note_failure(e)
        logger.warning(f"Could not fetch past proposals (non-fatal): {e}")
        return []


def get_winning_proposals(limit: int = 10) -> list[dict]:
    """Get only winning proposals."""
    if _circuit_open():
        return []
    try:
        table = get_table("past_proposals")
        records = table.all(
            formula="won = TRUE()",
            max_records=limit,
        )
        return [r["fields"] for r in records]
    except Exception as e:
        _note_failure(e)
        logger.warning(f"Could not fetch winning proposals (non-fatal): {e}")
        return []


def log_agent_action(
    action_type: str,
    description: str,
    opportunity_id: str = None,
    tokens_used: int = 0,
    status: str = "Success",
    error_message: str = None,
    model: str = None,
    estimated_cost_usd: float = None,
    error_type: str = None,
) -> None:
    """Log every agent action for audit trail. Never raises — a logging
    failure must not be allowed to crash the pipeline it's logging for.

    Cost: only written when estimated_cost_usd is provided or a known
    model + token count can ESTIMATE it. Never uses a fake blended $3/MTok.
    """
    if _circuit_open():
        return
    try:
        table = get_table("logs")
        from utils.observability import estimate_cost_usd, get_execution_id

        cost = estimated_cost_usd
        if cost is None and tokens_used and model:
            # Total tokens only — split unknown, so treat all as input (under-estimate, labeled).
            cost = estimate_cost_usd(model, int(tokens_used), 0)
        eid = get_execution_id()
        desc = description or ""
        if eid:
            desc = f"[{eid[:8]}] {desc}"
        if error_type:
            desc = f"{desc} error_type={error_type}"
        if len(desc) > 2000:
            desc = desc[:2000] + "…"

        payload = {
            "log_id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "action_type": action_type,
            "opportunity_id": opportunity_id or "",
            "description": desc,
            "tokens_used": tokens_used,
            "status": status,
            "error_message": (error_message or "")[:2000],
        }
        if cost is not None:
            payload["cost_usd"] = round(cost, 4)

        table.create(payload, typecast=True)
    except Exception as e:
        _note_failure(e)
        logger.warning(f"Agent log write failed (non-fatal, continuing): {e}")
