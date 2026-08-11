# intelligence/cv_matcher.py
import anthropic
import json
from loguru import logger
from database.supabase_client import search_consultants
from database.airtable_client import get_consultant_by_id, log_agent_action
from config import CLAUDE_MODEL, CORTECH_PROFILE
from utils.claude_helpers import get_text

client = anthropic.Anthropic()

# Human prerequisite — CONSULTANTS table must include and maintain:
#   current_projects, available_from, availability_percentage, booked_until
# This feature is only as good as those fields staying current.


def filter_by_availability(matches: list[dict]) -> list[dict]:
    """Annotates matches with live availability from Airtable. Never
    drops a match for missing data — flags it and lets a human decide."""
    for match in matches:
        airtable_id = match.get("airtable_consultant_id") or match.get("airtable_id")
        consultant = get_consultant_by_id(airtable_id) if airtable_id else None
        if not consultant:
            match["availability_flag"] = "❓ Unknown"
            continue
        pct = consultant.get("availability_percentage", 100)
        match["availability_percent"] = pct
        match["current_project_count"] = consultant.get("current_projects", 0)
        match["availability_flag"] = (
            "🟢 Available" if pct >= 50 else
            "🟡 Partially" if pct >= 20 else
            "🔴 Busy"
        )
    return sorted(matches, key=lambda m: m.get("availability_percent", 100), reverse=True)


def match_team_to_requirements(
    team_requirements: list[dict],
    opportunity_id: str = None,
    opportunity_title: str = ""
) -> dict:
    """
    For each required role in the ToR, find the best matching
    Cortech consultant using semantic search.
    """
    matched_team = {}
    gaps = []

    logger.info(f"Matching team for {len(team_requirements)} roles...")

    for req in team_requirements:
        role = req.get("role", "Unknown")
        level = req.get("level", "Senior")

        # Build semantic search query from requirements
        search_query = f"""
        {level} {role}
        Skills required: {', '.join(req.get('required_skills', []))}
        Geographic experience needed: {', '.join(req.get('required_geographic_experience', []))}
        Years experience: {req.get('years_experience_minimum', 0)}+
        Education: {req.get('required_education', '')}
        """

        # Search CV database
        matches = search_consultants(
            query_text=search_query,
            match_threshold=0.60,
            match_count=3
        )

        if matches:
            best_match = matches[0]
            matched_team[role] = {
                "consultant_name": best_match["consultant_name"],
                "airtable_id": best_match["airtable_consultant_id"],
                "role_title": best_match["role_title"],
                "similarity_score": round(best_match["similarity"] * 100),
                "metadata": best_match["metadata"],
                "all_matches": matches,
                "requirement": req,
            }
            logger.success(
                f"  {role} → {best_match['consultant_name']} "
                f"(score: {round(best_match['similarity'] * 100)}%)"
            )
        else:
            gaps.append(role)
            matched_team[role] = {
                "consultant_name": "EXTERNAL RECRUITMENT NEEDED",
                "airtable_id": None,
                "similarity_score": 0,
                "requirement": req,
            }
            logger.warning(f"  {role} → NO MATCH FOUND — External recruitment needed")

    # Log
    log_agent_action(
        action_type="Analysis",
        description=f"CV matching: {len(matched_team)} roles, {len(gaps)} gaps",
        opportunity_id=opportunity_id,
        tokens_used=0,  # Semantic search, no Claude tokens
        status="Success"
    )

    return {
        "matched_team": matched_team,
        "gaps": gaps,
        "coverage_percent": round(
            (len(matched_team) - len(gaps)) / len(matched_team) * 100
            if matched_team else 0
        )
    }
