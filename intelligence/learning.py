# intelligence/learning.py
"""Poll Airtable for Won/Lost outcomes and store lessons in Supabase for retrieval."""
import json

from loguru import logger

from config import OPENAI_MODEL
from database.airtable_client import get_table
from database.supabase_client import get_embedding, supabase
from utils.llm import complete, get_text


def process_win_loss_outcomes() -> None:
    """
    Polls Airtable for opportunities marked Won/Lost that haven't been
    processed into win_loss_memory yet. Checks Supabase directly for an
    existing row (by opportunity_id) rather than needing a new Airtable
    "processed" flag to maintain.
    """
    table = get_table("opportunities")
    resolved = table.all(formula="OR({status}='Won', {status}='Lost')")

    for record in resolved:
        opp_id = record["id"]
        already = (
            supabase.table("win_loss_memory")
            .select("id")
            .eq("opportunity_id", opp_id)
            .execute()
        )
        if already.data:
            continue  # already processed

        fields = record["fields"]
        outcome = fields.get("status", "")
        client = fields.get("client", "")
        donor = fields.get("donor", "")
        score = fields.get("relevance_score", 0)

        lesson_prompt = f"""Cortech Consulting Group submitted a proposal. Outcome: {outcome}
Client: {client} | Donor: {donor} | Fit score: {score}/100

Extract 3-5 specific, actionable lessons for future proposals to this client/donor.
Be concrete — not "improve methodology" but a specific, named requirement.

Return ONLY valid JSON: {{"lessons": [...], "donor_preferences": [...]}}"""

        try:
            response = complete(
                model=OPENAI_MODEL,
                max_tokens=600,
                messages=[{"role": "user", "content": lesson_prompt}],
            )
            lessons = json.loads(get_text(response))
        except Exception as e:
            logger.warning(f"Win/loss lesson extraction failed for {opp_id}: {e}")
            continue

        lesson_text = (
            f"OUTCOME: {outcome} | CLIENT: {client} | DONOR: {donor} | "
            f"LESSONS: {' | '.join(lessons.get('lessons', []))}"
        )
        try:
            embedding = get_embedding(lesson_text)
        except Exception as e:
            logger.warning(f"Could not embed win/loss lesson (non-fatal): {e}")
            embedding = None

        try:
            supabase.table("win_loss_memory").insert({
                "opportunity_id": opp_id,
                "outcome": outcome,
                "client": client,
                "donor": donor,
                "lessons": lessons,
                "embedding": embedding,
            }).execute()
            logger.success(f"Stored win/loss lesson for {opp_id} ({outcome})")
        except Exception as e:
            logger.warning(f"Could not store win/loss lesson for {opp_id}: {e}")
