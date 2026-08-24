# intelligence/analyzer.py
"""
RFP/ToR analysis via Claude.
Two public functions:
  analyze_rfp()              → structured JSON from full document text
  generate_compliance_matrix() → scored matrix against evaluation criteria
"""

import os
import json
from loguru import logger

import anthropic

from config import (
    ANTHROPIC_MAX_RETRIES,
    CLAUDE_MODEL,
    CLAUDE_MAX_TOKENS,
    CORTECH_PROFILE,
    get_anthropic_client,
)
from database.airtable_client import log_agent_action


# ── EXTRACTION SCHEMA ─────────────────────────────────────────────────────────
# Claude must return a JSON object matching this structure exactly.
# is_consultancy_contract is the most critical field — it gates the
# entire downstream pipeline before any expensive work begins.

ANALYSIS_SCHEMA = """
{
  "opportunity": {
    "title": "string",
    "client": "string",
    "donor": "string or null",
    "reference_number": "string or null",
    "submission_deadline": "YYYY-MM-DD or null",
    "project_location": ["list of countries or cities"],
    "project_duration": "string e.g. 3 months",
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
      "role": "string — job title",
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
    "is_consultancy_contract": "boolean — TRUE if a company/firm is being hired to deliver a product, study, evaluation, assessment, or service with defined deliverables and a scope of work. FALSE only if this is a pure individual staff vacancy with no deliverables (salaried employment). DEFAULT TO TRUE when in doubt.",
    "submission_type": "EOI or FULL_PROPOSAL — EOI if the document explicitly requests an Expression of Interest, REOI, pre-qualification, or shortlisting submission. FULL_PROPOSAL if it requests a Technical Proposal, RFP response, or doesn't specify a lighter stage. DEFAULT TO FULL_PROPOSAL WHEN GENUINELY AMBIGUOUS.",
    "cortech_fit_score": "number 0-100",
    "win_probability": "number 0-100",
    "bid_recommendation": "BID/WATCH/NO-BID",
    "effort_required": "Low/Medium/High",
    "key_strengths": ["list of Cortech advantages for this opportunity"],
    "key_gaps": ["list of gaps or weaknesses"],
    "recommended_external_partners": ["list of partner types if needed"],
    "rationale": "2-3 sentence explanation of score and recommendation",
    "priority": "HIGH/MEDIUM/LOW"
  }
}
"""


# ── MAIN ANALYSIS FUNCTION ────────────────────────────────────────────────────

def analyze_rfp(
    tor_text: str,
    opportunity_id: str = None,
    title: str = "Unknown",
) -> dict:
    """
    Reads a full ToR/RFP document and extracts structured intelligence.

    Returns a dict matching ANALYSIS_SCHEMA, or empty dict on failure.
    The is_consultancy_contract field in bid_analysis is the critical
    gate read by main.py before any further pipeline work begins.
    """
    logger.info(f"  Analyzing: {title[:60]}...")

    # Truncate if too long — keep within safe token budget
    max_chars = 120000  # ~30k tokens — enough for a ToR plus 2–3 annexes
    if len(tor_text) > max_chars:
        # Keep beginning and end — both contain critical information
        half = max_chars // 2
        tor_text = (
            tor_text[:half]
            + "\n\n[... MIDDLE SECTION TRUNCATED FOR TOKEN MANAGEMENT ...]\n\n"
            + tor_text[-half:]
        )

    prompt = f"""You are an expert development-sector business analyst for
Cortech Consulting Group. Analyze the document below and return a
single valid JSON object. No preamble, no markdown, no explanation —
only the JSON object.

CORTECH PROFILE (use this to score fit):
{CORTECH_PROFILE}

CRITICAL FILTER — READ THIS BEFORE ANYTHING ELSE:
Determine whether this document is a FIRM-LEVEL CONSULTANCY CONTRACT
or a STAFF VACANCY.

Set is_consultancy_contract = TRUE for:
- RFPs, ToRs, EOIs, Call for Proposals, procurement notices
- Documents where a COMPANY or TEAM is being hired to deliver
  a product, study, evaluation, assessment, or service
- Any document with deliverables, timelines, and payment milestones
  for an organization rather than an individual employee
- Even if the document says "individual consultant" in places,
  if it has a scope of work and deliverables it is TRUE

Set is_consultancy_contract = FALSE ONLY for:
- Pure job postings where ONE PERSON is being recruited as an employee
- Salaried positions with HR language (benefits, leave entitlement)
- Vacancy announcements with no scope of work or deliverables

IF IN DOUBT: set is_consultancy_contract = TRUE.
It is better to draft a proposal for a borderline case than to miss
a real opportunity. The human reviewer makes the final submission call.

If is_consultancy_contract is FALSE:
  - Set cortech_fit_score to 0
  - Set bid_recommendation to "NO-BID"
  - State clearly in rationale: "STAFF VACANCY — not a consultancy contract"

SUBMISSION TYPE — classify what the document asks firms to submit:
Set submission_type = "EOI" if the document explicitly requests an
Expression of Interest, REOI, pre-qualification, or shortlisting submission.
Set submission_type = "FULL_PROPOSAL" if it requests a Technical Proposal,
RFP response, or doesn't specify a lighter stage.
DEFAULT TO FULL_PROPOSAL WHEN GENUINELY AMBIGUOUS — a full proposal can
be trimmed by a human reviewer in minutes; an EOI-only draft cannot be
expanded into a full proposal under deadline pressure if a full one
turns out to be needed.

SCORING GUIDANCE for cortech_fit_score (0-100):
- 85-100: Perfect match — all requirements met, strong track record,
          ideal geography, high win probability
- 70-84:  Strong match — most requirements met, minor gaps fillable
- 50-69:  Moderate match — some gaps but manageable with the right team
- 30-49:  Weak match — significant gaps, high effort for uncertain win
- 0-29:   Poor match — fundamental misalignment with Cortech's profile

SCHEMA — return a JSON object matching this exactly:
{ANALYSIS_SCHEMA}

DOCUMENT TO ANALYZE:
The text may contain multiple source files concatenated (e.g. Annex I, II, III
from a Google Drive folder), each marked with ===== SOURCE FILE: <name> =====.
Read ALL of them. Evaluation criteria, scoring metrics, required proposal
sections, and submission instructions often live in an annex rather than the
cover ToR — extract evaluation_criteria from wherever they actually appear
and treat the combined pack as one assignment.
Copy every scored criterion and its weight_percent exactly as stated
(technical approach, methodology, team, experience, financial, etc.).
If a scoring matrix or marking scheme exists in any annex, extract every
row. Do not summarise away the weights or collapse distinct criteria.

{tor_text}"""

    tokens_used = 0

    try:
        response = get_anthropic_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}]
        )

        tokens_used = (
            response.usage.input_tokens + response.usage.output_tokens
        )
        response_text = response.content[0].text.strip()

        # Strip markdown code fences if Claude added them
        if "```" in response_text:
            parts = response_text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    response_text = part
                    break

        analysis = json.loads(response_text)

        score = (
            analysis
            .get("bid_analysis", {})
            .get("cortech_fit_score", 0)
        )
        is_contract = (
            analysis
            .get("bid_analysis", {})
            .get("is_consultancy_contract", True)
        )
        submission_type = (
            analysis
            .get("bid_analysis", {})
            .get("submission_type", "FULL_PROPOSAL")
        )

        logger.success(
            f"  Analysis complete — score: {score}/100 | "
            f"consultancy: {is_contract} | submission: {submission_type}"
        )

        try:
            log_agent_action(
                action_type="Analysis",
                description=f"Analyzed: {title[:60]}",
                opportunity_id=opportunity_id,
                tokens_used=tokens_used,
                status="Success",
            )
        except Exception:
            pass  # Logging must never crash the pipeline

        return analysis

    except json.JSONDecodeError as e:
        logger.error(f"  JSON parse failed for '{title[:60]}': {e}")
        try:
            log_agent_action(
                action_type="Error",
                description=f"JSON parse failed: {title[:60]}",
                opportunity_id=opportunity_id,
                tokens_used=tokens_used,
                status="Error",
                error_message=str(e),
            )
        except Exception:
            pass
        return {}

    except anthropic.APITimeoutError:
        from config import ANTHROPIC_TIMEOUT_SECONDS
        logger.error(
            f"  Analysis timed out after {ANTHROPIC_TIMEOUT_SECONDS:.0f}s "
            f"(x{ANTHROPIC_MAX_RETRIES + 1} attempts) for '{title[:60]}' — "
            "skipping. Raise ANTHROPIC_TIMEOUT_SECONDS in .env if this "
            "recurs on large documents."
        )
        return {}

    except anthropic.RateLimitError:
        logger.error("  Anthropic rate limit hit — waiting 60s")
        import time
        time.sleep(60)
        return {}

    except anthropic.AuthenticationError:
        suffix = "????"
        try:
            from config import get_anthropic_api_key as _key
            k = _key() or ""
            if len(k) >= 4:
                suffix = k[-4:]
        except Exception:
            pass
        logger.error(
            f"  Anthropic rejected API key ending ...{suffix} (401 invalid). "
            "Create a new key at https://console.anthropic.com/settings/keys "
            "(API Console, not claude.ai), paste it in .env as "
            "ANTHROPIC_API_KEY=sk-ant-api03-... with no quotes, save, and rerun."
        )
        return {}

    except Exception as e:
        logger.error(f"  Analysis failed for '{title[:60]}': {e}")
        try:
            log_agent_action(
                action_type="Error",
                description=f"Analysis exception: {title[:60]}",
                opportunity_id=opportunity_id,
                tokens_used=tokens_used,
                status="Error",
                error_message=str(e),
            )
        except Exception:
            pass
        return {}


# ── COMPLIANCE MATRIX ─────────────────────────────────────────────────────────

def generate_compliance_matrix(
    analysis: dict,
    matched_team: list[dict],
) -> str:
    """
    Generates a compliance matrix mapping evaluation criteria from the
    ToR against Cortech's matched capabilities and team.

    Returns formatted text suitable for inclusion in proposal emails
    and Airtable records.
    """
    evaluation_criteria = analysis.get("evaluation_criteria", [])
    bid_analysis        = analysis.get("bid_analysis", {})
    key_strengths       = bid_analysis.get("key_strengths", [])
    key_gaps            = bid_analysis.get("key_gaps", [])
    opportunity         = analysis.get("opportunity", {})

    team_summary = [
        {
            "required_role": role,
            "assigned":      match.get("consultant_name", "TBD"),
            "match_score":   match.get("similarity_score", 0),
        }
        for role, match in
        {r: m for r, m in
         [(r, m) for m in matched_team
          for r in [m.get("role", "Unknown")]
          if m.get("consultant_name") != "EXTERNAL RECRUITMENT NEEDED"]
        }.items()
    ] if matched_team else []

    prompt = f"""Generate a compliance matrix for a Cortech Consulting Group
proposal bid response. Format as a structured text table followed by
a brief summary.

OPPORTUNITY: {opportunity.get("title", "Unknown")}
CLIENT: {opportunity.get("client", "Unknown")}

EVALUATION CRITERIA FROM ToR:
{json.dumps(evaluation_criteria, indent=2)}

CORTECH KEY STRENGTHS FOR THIS BID:
{json.dumps(key_strengths, indent=2)}

GAPS TO ADDRESS:
{json.dumps(key_gaps, indent=2)}

MATCHED TEAM:
{json.dumps(team_summary, indent=2)}

CORTECH PROFILE:
{CORTECH_PROFILE}

Format the matrix as:
CRITERION | WEIGHT | STATUS | CORTECH EVIDENCE | ESTIMATED SCORE

Use: STRONG | PARTIAL | GAP for status column.

End with:
- ESTIMATED TOTAL SCORE: X/100
- WIN PROBABILITY: X%
- TOP 3 MITIGATION STRATEGIES for any gaps

Maximum 600 words. Be specific — reference actual Cortech experience."""

    try:
        response = get_anthropic_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text

    except Exception as e:
        logger.error(f"Compliance matrix generation failed: {e}")
        return "Compliance matrix generation failed — see analysis JSON for manual assessment."