# intelligence/analyzer.py
import anthropic
import json
from loguru import logger
from config import CLAUDE_MODEL, CLAUDE_MAX_TOKENS, CORTECH_PROFILE
from database.airtable_client import log_agent_action

client = anthropic.Anthropic()


ANALYSIS_SCHEMA = """
{
  "opportunity": {
    "title": "string",
    "client": "string",
    "donor": "string or null",
    "reference_number": "string or null",
    "submission_deadline": "YYYY-MM-DD or null",
    "project_location": ["list of countries/cities"],
    "project_duration": "string e.g. '3 months'",
    "estimated_budget_usd": "number or null",
    "currency": "string"
  },
  "requirements": {
    "technical": ["list of technical requirements"],
    "thematic_areas": ["list of thematic areas"],
    "geographic_experience": ["list of required locations"],
    "language_requirements": ["list of languages"],
    "certifications": ["list of required certifications or policies"],
    "methodology_requirements": ["list of required methodologies"]
  },
  "team_requirements": [
    {
      "role": "string - job title",
      "level": "Senior/Mid/Junior",
      "years_experience_minimum": "number",
      "required_skills": ["list of skills"],
      "required_geographic_experience": ["list of locations"],
      "required_education": "string",
      "estimated_days_of_effort": "number or null",
      "must_be_local": "boolean",
      "local_country": "string or null"
    }
  ],
  "deliverables": [
    {
      "name": "string",
      "description": "string",
      "timeline": "string"
    }
  ],
  "evaluation_criteria": [
    {
      "criterion": "string",
      "weight_percent": "number or null",
      "description": "string"
    }
  ],
  "submission_requirements": {
    "technical_proposal_page_limit": "number or null",
    "cvs_required": "boolean",
    "past_work_samples_required": "number",
    "references_required": "number",
    "financial_proposal_required": "boolean"
  },
  "bid_analysis": {
    "cortech_fit_score": "number 0-100",
    "win_probability": "number 0-100",
    "bid_recommendation": "BID/WATCH/NO-BID",
    "effort_required": "Low/Medium/High",
    "key_strengths": ["list of Cortech advantages"],
    "key_gaps": ["list of weaknesses or missing requirements"],
    "recommended_external_partners": ["list of suggested partner types if needed"],
    "rationale": "2-3 sentence explanation",
    "priority": "HIGH/MEDIUM/LOW"
  }
}
"""


def analyze_rfp(
    tor_text: str,
    opportunity_id: str = None,
    title: str = "Unknown"
) -> dict:
    """
    Core function: Claude reads the entire ToR/RFP and extracts
    structured intelligence. This is the main intelligence call.
    """
    logger.info(f"Analyzing RFP: {title[:60]}...")

    # Chunk if too long (Claude can handle 200k tokens but we manage costs)
    max_chars = 100_000  # ~25k tokens
    if len(tor_text) > max_chars:
        tor_text = tor_text[:max_chars] + "\n\n[DOCUMENT TRUNCATED FOR ANALYSIS]"

    prompt = f"""You are an expert development sector business analyst for Cortech Consulting Group.

CORTECH PROFILE:
{CORTECH_PROFILE}

TASK:
Analyze the following ToR/RFP document and extract comprehensive intelligence.
Return ONLY a valid JSON object matching this exact schema (no other text):

SCHEMA:
{ANALYSIS_SCHEMA}

CRITICAL FILTER — READ FIRST:
Cortech Consulting Group is a FIRM that bids on CONSULTANCY ENGAGEMENTS
— time-bound contracts for research, evaluation, MEL, or technical
assistance delivered by a team. Cortech does NOT recruit for staff
positions, employee vacancies, or individual salaried roles (Country
Director, Head of Grants, Finance Officer, Programme Manager, etc.).

If this posting is a staff/employee vacancy rather than a firm-level
consultancy contract, set cortech_fit_score to 0-5 regardless of
thematic overlap, and set bid_recommendation to "NO-BID" with
rationale explicitly stating this is a staff position, not a
consultancy opportunity.

SCORING GUIDANCE:
- cortech_fit_score 0-100:
  * 90-100: Perfect match - all requirements met, strong track record, ideal geography
  * 70-89: Strong match - most requirements met, minor gaps
  * 50-69: Moderate match - some gaps but manageable
  * 30-49: Weak match - significant gaps
  * 0-29: Poor match - fundamental misalignment

- win_probability considers:
  * Cortech's past performance in similar work
  * Competition level in this space
  * Relationship with client/donor
  * Realistic assessment of team capability

ToR/RFP DOCUMENT:
{tor_text}

Return only the JSON object. No preamble, no explanation, no markdown formatting."""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = response.content[0].text.strip()
        tokens_used = response.usage.input_tokens + response.usage.output_tokens

        # Log to Airtable
        log_agent_action(
            action_type="Analysis",
            description=f"Analyzed RFP: {title[:60]}",
            opportunity_id=opportunity_id,
            tokens_used=tokens_used,
            status="Success"
        )

        # Parse JSON
        # Remove any markdown formatting if Claude added it
        if response_text.startswith("```"):
            response_text = response_text.split("```json")[-1].split("```")[0]

        analysis = json.loads(response_text)
        logger.success(f"Analysis complete. Fit score: {analysis['bid_analysis']['cortech_fit_score']}")
        return analysis

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error in RFP analysis: {e}")
        logger.debug(f"Raw response: {response_text[:500]}")
        log_agent_action(
            action_type="Error",
            description=f"JSON parse failed for: {title}",
            opportunity_id=opportunity_id,
            status="Error",
            error_message=str(e)
        )
        return {}

    except Exception as e:
        logger.error(f"RFP analysis failed: {e}")
        log_agent_action(
            action_type="Error",
            description=f"Analysis failed for: {title}",
            opportunity_id=opportunity_id,
            status="Error",
            error_message=str(e)
        )
        return {}


def generate_compliance_matrix(
    analysis: dict,
    matched_team: list[dict]
) -> str:
    """Generate a compliance matrix as formatted text."""

    evaluation_criteria = analysis.get("evaluation_criteria", [])
    key_strengths = analysis.get("bid_analysis", {}).get("key_strengths", [])
    key_gaps = analysis.get("bid_analysis", {}).get("key_gaps", [])

    prompt = f"""Generate a compliance matrix for Cortech Consulting Group's bid response.

EVALUATION CRITERIA FROM ToR:
{json.dumps(evaluation_criteria, indent=2)}

CORTECH STRENGTHS FOR THIS BID:
{json.dumps(key_strengths, indent=2)}

GAPS TO ADDRESS:
{json.dumps(key_gaps, indent=2)}

MATCHED TEAM:
{json.dumps([{"name": m.get("consultant_name"), "role": m.get("role_title"), "match_score": m.get("similarity")} for m in matched_team], indent=2)}

CORTECH PROFILE:
{CORTECH_PROFILE}

Generate a compliance matrix in this format:
CRITERION | WEIGHT | STATUS | CORTECH EVIDENCE | SCORE/WEIGHT
For each criterion show: ✅ STRONG, ⚠️ PARTIAL, ❌ GAP

Then add:
- ESTIMATED TOTAL SCORE: X/100
- WIN PROBABILITY: X%  
- KEY MITIGATION STRATEGIES for any gaps

Keep it concise but comprehensive. Maximum 600 words."""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text