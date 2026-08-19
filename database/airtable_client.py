# database/airtable_client.py
from pyairtable import Api
from config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID, TABLES
from loguru import logger
import uuid
from datetime import datetime
from typing import Optional


api = Api(AIRTABLE_API_KEY)
base = api.base(AIRTABLE_BASE_ID)


def get_table(table_name: str):
    return base.table(TABLES[table_name])


def create_opportunity(opportunity_data: dict) -> str | None:
    """Create new opportunity record in Airtable."""
    table = get_table("opportunities")

    opportunity_data["opportunity_id"] = str(uuid.uuid4())
    opportunity_data["discovered_at"] = datetime.now().strftime("%Y-%m-%d")
    opportunity_data["status"] = "New"

    try:
        record = table.create(opportunity_data, typecast=True)
        logger.success(f"Created opportunity: {opportunity_data.get('title', 'Unknown')}")
        return record["id"]
    except Exception as e:
        logger.error(f"Failed to save opportunity '{opportunity_data.get('title')}' to Airtable: {e}")
        import json, os
        os.makedirs("failed_saves", exist_ok=True)
        with open(f"failed_saves/{opportunity_data['opportunity_id']}.json", "w") as f:
            json.dump(opportunity_data, f, default=str, indent=2)
        return None


def update_opportunity(record_id: str, fields: dict) -> None:
    """Update opportunity record. Never raises — a 429 must not crash the pipeline."""
    try:
        table = get_table("opportunities")
        table.update(record_id, fields)
    except Exception as e:
        logger.warning(f"Airtable update failed (non-fatal): {e}")


def get_all_consultants() -> list[dict]:
    """Fetch all consultant records from Airtable."""
    try:
        table = get_table("consultants")
        records = table.all()
        return [{"id": r["id"], **r["fields"]} for r in records]
    except Exception as e:
        logger.warning(f"Could not fetch consultants (non-fatal): {e}")
        return []


def get_consultant_by_id(airtable_id: str) -> Optional[dict]:
    """Fetch single consultant. Returns None on Airtable failure so matching continues."""
    try:
        table = get_table("consultants")
        record = table.get(airtable_id)
        return record["fields"] if record else None
    except Exception as e:
        logger.warning(f"Could not fetch consultant {airtable_id} (non-fatal): {e}")
        return None


def get_rate_card() -> list[dict]:
    """Get all rate card entries. Empty list on 429 so budget still calculates with defaults."""
    try:
        table = get_table("rate_cards")
        records = table.all()
        return [r["fields"] for r in records]
    except Exception as e:
        logger.warning(
            f"Could not fetch rate card from Airtable (non-fatal) — "
            f"budget will use default day rates: {e}"
        )
        return []


def get_past_proposals(limit: int = 20) -> list[dict]:
    """Get past proposals for style reference."""
    try:
        table = get_table("past_proposals")
        records = table.all(max_records=limit, sort=["-year"])
        return [{"id": r["id"], **r["fields"]} for r in records]
    except Exception as e:
        logger.warning(f"Could not fetch past proposals (non-fatal): {e}")
        return []


def get_winning_proposals(limit: int = 10) -> list[dict]:
    """Get only winning proposals."""
    try:
        table = get_table("past_proposals")
        records = table.all(
            formula="won = TRUE()",
            max_records=limit,
        )
        return [r["fields"] for r in records]
    except Exception as e:
        logger.warning(f"Could not fetch winning proposals (non-fatal): {e}")
        return []


def log_agent_action(
    action_type: str,
    description: str,
    opportunity_id: str = None,
    tokens_used: int = 0,
    status: str = "Success",
    error_message: str = None
) -> None:
    """Log every agent action for audit trail. Never raises — a logging
    failure must not be allowed to crash the pipeline it's logging for."""
    try:
        table = get_table("logs")
        cost_usd = (tokens_used / 1_000_000) * 3.00

        table.create({
            "log_id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "action_type": action_type,
            "opportunity_id": opportunity_id or "",
            "description": description,
            "tokens_used": tokens_used,
            "cost_usd": round(cost_usd, 4),
            "status": status,
            "error_message": error_message or "",
        }, typecast=True)
    except Exception as e:
        logger.warning(f"Agent log write failed (non-fatal, continuing): {e}")