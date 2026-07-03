"""
check_schema.py
Compares the exact field names populate_airtable.py expects against
what actually exists in Airtable. Run this ONCE — fixes every field
mismatch in one pass instead of discovering them one error at a time.
"""
import os
from dotenv import load_dotenv
from pyairtable import Api

load_dotenv()

api = Api(os.getenv("AIRTABLE_API_KEY"))
base = api.base(os.getenv("AIRTABLE_BASE_ID"))

EXPECTED = {
    "CONSULTANTS": [
        "consultant_id", "full_name", "role_title", "years_experience",
        "education", "seniority_level", "based_in", "day_rate_usd",
        "thematic_expertise", "geographic_experience", "languages",
        "tools", "key_skills", "key_assignments", "availability_status",
        "cv_text", "cv_last_updated",
    ],
    "PAST_PROPOSALS": [
        "proposal_id", "project_title", "client", "donor", "year",
        "won", "contract_value_usd", "thematic_areas", "location",
        "methodology_approach", "proposal_text",
    ],
    "AGENT_LOGS": [
        "log_id", "timestamp", "action_type", "opportunity_id",
        "description", "tokens_used", "cost_usd", "status",
        "error_message",
    ],
    "OPPORTUNITIES": [
        "opportunity_id", "title", "client", "donor", "source_portal",
        "source_url", "submission_deadline", "estimated_budget_usd",
        "location", "thematic_areas", "relevance_score",
        "win_probability", "bid_recommendation", "claude_analysis",
        "key_strengths", "key_gaps", "status", "discovered_at",
        "matched_team", "compliance_matrix",
    ],
}

for table_name, expected_fields in EXPECTED.items():
    print(f"\n{'='*60}\n  {table_name}\n{'='*60}")
    table = base.table(table_name)

    try:
        schema = table.schema()
        actual_fields = {f.name for f in schema.fields}
    except Exception as e:
        print(f"  (schema() unsupported, falling back to sample record: {e})")
        records = table.all(max_records=1)
        actual_fields = set(records[0]["fields"].keys()) if records else set()
        print(f"  Note: fallback only catches fields with data in that one row")

    missing = [f for f in expected_fields if f not in actual_fields]

    if not missing:
        print(f"  ✅ All {len(expected_fields)} expected fields exist correctly")
    else:
        print(f"  ❌ MISSING or MISNAMED ({len(missing)}):")
        for f in missing:
            print(f"     - {f}")