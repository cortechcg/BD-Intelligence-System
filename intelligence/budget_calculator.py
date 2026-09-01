# intelligence/budget_calculator.py
import anthropic
import json
from loguru import logger
from database.airtable_client import get_rate_card
from config import CLAUDE_MODEL, get_anthropic_client
from utils.claude_helpers import get_text

client = get_anthropic_client()


def calculate_budget(
    analysis: dict,
    matched_team: dict,
    primary_location: str = "Nairobi"
) -> dict:
    """
    Calculate project budget based on:
    - Matched team and their day rates from Airtable rate card
    - Project duration from analysis
    - Deliverables and estimated effort
    """
    rate_card = get_rate_card()

    # Build rate lookup
    rates = {}
    per_diems = {}
    for rate in rate_card:
        key = f"{rate.get('role_level', '')}_{rate.get('location', '')}"
        rates[key] = rate.get("day_rate_usd", 0)
        per_diems[rate.get("location", "")] = rate.get("per_diem_usd", 0)

    team_requirements = analysis.get("team_requirements", [])
    duration_text = analysis.get("opportunity", {}).get("project_duration", "3 months")
    deliverables = analysis.get("deliverables", [])

    # Ask Claude to estimate days of effort per role
    prompt = f"""Calculate days of effort for each team role for this project.

PROJECT DETAILS:
Title: {analysis.get('opportunity', {}).get('title', '')}
Duration: {duration_text}
Location: {primary_location}

DELIVERABLES:
{json.dumps(deliverables, indent=2)}

TEAM REQUIREMENTS:
{json.dumps(team_requirements, indent=2)}

For each role, estimate:
- Input days (desk work, analysis, writing)
- Field days (travel, workshops, community engagement)
- Total days

Return ONLY a JSON array:
[
  {{
    "role": "Team Leader",
    "level": "Senior",
    "input_days": 15,
    "field_days": 5,
    "total_days": 20
  }}
]"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    effort_text = get_text(response).strip()
    if effort_text.startswith("```"):
        effort_text = effort_text.split("```json")[-1].split("```")[0]

    try:
        effort_estimates = json.loads(effort_text)
    except:
        effort_estimates = []

    # Calculate costs
    personnel_costs = {}
    total_personnel = 0

    for effort in effort_estimates:
        role = effort.get("role", "")
        level = effort.get("level", "Mid")
        total_days = effort.get("total_days", 10)
        field_days = effort.get("field_days", 0)

        # Get day rate
        rate_key = f"{level}_{primary_location}"
        day_rate = rates.get(rate_key, rates.get(f"{level}_Nairobi", 1000))
        per_diem = per_diems.get(primary_location, 150)

        person_cost = (total_days * day_rate) + (field_days * per_diem)
        personnel_costs[role] = {
            "days": total_days,
            "field_days": field_days,
            "day_rate_usd": day_rate,
            "personnel_cost_usd": total_days * day_rate,
            "per_diem_cost_usd": field_days * per_diem,
            "total_cost_usd": person_cost,
        }
        total_personnel += person_cost

    # Standard additions
    field_logistics = total_personnel * 0.12  # 12% for travel, accommodation
    data_tools = 2500  # KoboToolbox, software, materials
    reporting = total_personnel * 0.05  # 5% for report design, printing
    management_overhead = total_personnel * 0.08  # 8% overhead
    contingency = total_personnel * 0.05  # 5% contingency

    total = (
        total_personnel
        + field_logistics
        + data_tools
        + reporting
        + management_overhead
        + contingency
    )

    # Workshop costs (separate)
    workshop_costs = estimate_workshop_costs(deliverables, primary_location)

    budget = {
        "summary": {
            "personnel_subtotal_usd": round(total_personnel),
            "field_logistics_usd": round(field_logistics),
            "data_tools_usd": round(data_tools),
            "reporting_design_usd": round(reporting),
            "management_overhead_usd": round(management_overhead),
            "contingency_5pct_usd": round(contingency),
            "core_budget_usd": round(total),
            "workshop_costs_usd": round(workshop_costs["total"]),
            "grand_total_usd": round(total + workshop_costs["total"]),
        },
        "personnel_breakdown": personnel_costs,
        "workshop_breakdown": workshop_costs,
        "currency": "USD",
        "primary_location": primary_location,
    }

    logger.success(f"Budget calculated: ${budget['summary']['grand_total_usd']:,}")
    return budget


def estimate_workshop_costs(deliverables: list, location: str) -> dict:
    """Estimate workshop and event costs."""
    # Count workshops in deliverables
    workshop_count = sum(
        1 for d in deliverables
        if any(word in d.get("name", "").lower()
               for word in ["workshop", "training", "roundtable", "session"])
    )

    workshop_count = max(workshop_count, 2)  # Minimum 2

    # Location-based rates
    venue_rates = {
        "Nairobi": 400,
        "Addis Ababa": 350,
        "Mogadishu": 600,
        "London": 800,
    }
    venue_day = venue_rates.get(location, 400)
    catering_pp = 35
    participants_avg = 25

    workshop_total = workshop_count * (
        venue_day
        + (catering_pp * participants_avg)
        + 200  # materials
    )

    return {
        "workshop_count": workshop_count,
        "avg_participants": participants_avg,
        "venue_per_day_usd": venue_day,
        "catering_per_person_usd": catering_pp,
        "total": workshop_total,
    }