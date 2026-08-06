# intelligence/proposal_writer.py
import anthropic
import json
from loguru import logger
from utils.claude_helpers import get_text
from database.airtable_client import get_winning_proposals, log_agent_action
from config import CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, CORTECH_PROFILE
client = anthropic.Anthropic()


# This is Cortech's exact proposal structure (from the DRC template)
PROPOSAL_STRUCTURE = """
PROPOSAL SECTIONS (in order):
1. COVER LETTER (addressed to client, professional, 4-5 paragraphs)
2. EXECUTIVE SUMMARY (2-3 pages overview)
3. ORGANISATIONAL PROFILE (Cortech's credentials)
4. RELATED PREVIOUS ASSIGNMENTS (table: project, client, value, description)
5. INTRODUCTION AND BACKGROUND
   5.1 Context and Strategic Importance
   5.2 Purpose and Objectives
   5.3 Our Interpretation of the Assignment
   5.4 Key Evaluation Questions
   5.5 Deliverables
   5.6 Understanding of Success
6. CONCEPTUAL FRAMEWORK
7. DETAILED METHODOLOGY (section per deliverable/work package)
8. SAMPLING STRATEGY (if surveys involved)
9. DATA ANALYSIS PLAN
10. QUALITY ASSURANCE FRAMEWORK
11. ETHICAL CONSIDERATIONS AND SAFEGUARDING
12. RISK MANAGEMENT FRAMEWORK
13. PROPOSED CORE EXPERTS (team table)
14. WORK PLAN AND TIMELINE (Gantt)
"""


CORTECH_PAST_WORK = """
RELEVANT PAST ASSIGNMENTS (use as references in proposal):
1. End-of-Project Evaluation — IGAD Land Governance Programme
   Client: IGAD / Swedish Embassy | Value: $24,500 | Year: 2026
   Description: Comprehensive evaluation of land governance in IGAD region

2. Assessment of Federal MOH Capacity — Somalia
   Client: World Bank | Value: $149,000 | Year: 2025
   Description: Ministry of Health institutional assessment across Somalia

3. Free Movement of Persons Framework — Africa
   Client: IOM & African Union | Value: $59,500 | Year: 2024
   Description: MEL framework development for continental protocol

4. Green Skills Documentation — Baidoa, Somalia
   Client: GREDO/DANIDA/Save the Children | Value: $6,800 | Year: 2025
   Description: Best practices documentation for youth employment project

5. Endline Evaluation — Water & Livelihoods, Somalia
   Client: Arche Nova | Value: $34,740 | Year: 2025
   Description: WASH and livelihoods project evaluation

6. Financial Services Mapping — Refugees Kenya
   Client: DRC Kenya | Value: KES 3.5M | Year: 2025
   Description: FSP mapping for refugees in Garissa, Nairobi, Turkana

7. Civil Society Evaluation — Chukua Control, Kenya
   Client: Welthungerhilfe | Value: KES 4.3M | Year: 2025
   Description: Endline evaluation of civil society empowerment project
"""


def _build_past_work_context() -> str:
    """
    Pull real winning proposals from Airtable for use as proposal evidence.
    Falls back to the static CORTECH_PAST_WORK list if Airtable returns
    nothing, so proposal generation never blocks on this.
    """
    try:
        winners = get_winning_proposals(limit=10)
    except Exception as e:
        logger.warning(f"Could not fetch winning proposals from Airtable: {e}")
        winners = []

    if not winners:
        return CORTECH_PAST_WORK

    lines = ["RELEVANT PAST ASSIGNMENTS (use as references in proposal):"]
    for i, w in enumerate(winners, 1):
        lines.append(
            f"{i}. {w.get('project_title', 'Untitled')}\n"
            f"   Client: {w.get('client', 'N/A')} | "
            f"Value: ${w.get('contract_value_usd', 0):,} | "
            f"Year: {w.get('year', 'N/A')}\n"
            f"   Description: {w.get('methodology_approach', '')[:200]}"
        )
    return "\n\n".join(lines)


def generate_proposal(
    analysis: dict,
    matched_team_result: dict,
    budget: dict,
    compliance_matrix: str,
    opportunity_id: str = None,
) -> dict:
    """
    Generate a complete technical proposal using Claude.
    Returns a dict with each section as a separate key.
    """
    logger.info("Generating proposal with Claude...")

    opportunity = analysis.get("opportunity", {})
    title = opportunity.get("title", "Unknown Assignment")
    client_name = opportunity.get("client", "Client")
    deadline = opportunity.get("submission_deadline", "TBD")

    team_members = []
    for role, match in matched_team_result.get("matched_team", {}).items():
        if match.get("consultant_name") != "EXTERNAL RECRUITMENT NEEDED":
            team_members.append(
                f"- {match['consultant_name']}: {role} (Match: {match['similarity_score']}%)"
            )

    # Generate each section separately for quality + token management.
    # Ordered to match PROPOSAL_STRUCTURE above — 10 Claude calls total,
    # covering all 14 documented sections via 4 grouped calls.
    sections = {}

    sections["cover_letter"] = generate_cover_letter(
        title, client_name, deadline, analysis
    )
    sections["executive_summary"] = generate_executive_summary(
        analysis, matched_team_result, budget
    )
    sections["org_profile_and_track_record"] = generate_org_profile_and_track_record(
        analysis
    )
    sections["introduction_and_framework"] = generate_introduction_and_framework(
        analysis
    )
    sections["methodology"] = generate_methodology(analysis)
    sections["analysis_plan"] = generate_analysis_plan(analysis)
    sections["qa_and_ethics"] = generate_qa_and_ethics(analysis)
    sections["risk_register"] = generate_risk_register(analysis)
    sections["team_section"] = generate_team_section(
        matched_team_result, title
    )
    sections["work_plan"] = generate_work_plan(analysis)

    # Log
    log_agent_action(
        action_type="Proposal",
        description=f"Generated draft proposal for: {title[:60]}",
        opportunity_id=opportunity_id,
        tokens_used=15000,  # Approximate
        status="Success"
    )

    logger.success("Proposal generation complete!")
    return sections


def generate_cover_letter(
    title: str,
    client_name: str,
    deadline: str,
    analysis: dict
) -> str:
    """Generate professional cover letter."""
    strengths = analysis.get("bid_analysis", {}).get("key_strengths", [])

    prompt = f"""Write a professional cover letter for Cortech Consulting Group's technical proposal.

ASSIGNMENT: {title}
CLIENT: {client_name}
SUBMISSION DATE: {deadline}

CORTECH PROFILE:
{CORTECH_PROFILE}

KEY STRENGTHS FOR THIS BID:
{json.dumps(strengths, indent=2)}

REQUIREMENTS:
- Address to the procurement committee
- 4-5 professional paragraphs
- Opening: Express interest and understanding of the opportunity
- Middle: Highlight Cortech's most relevant experience and team
- Closing: Availability for questions, enthusiasm for partnership
- Sign off from: Daud Hussein Ibrahim, Director, Cortech Consulting Group
- Professional development consulting sector tone
- Do NOT use hollow phrases like "we are excited" or "we are pleased"
- Be specific about capabilities, not generic
- Maximum 400 words"""

    response = client.messages.create(
        model=CLAUDE_MODEL_PROPOSAL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return get_text(response)


def generate_executive_summary(
    analysis: dict,
    matched_team_result: dict,
    budget: dict
) -> str:
    """Generate executive summary."""
    opportunity = analysis.get("opportunity", {})
    deliverables = analysis.get("deliverables", [])
    eval_criteria = analysis.get("evaluation_criteria", [])
    budget_total = budget.get("summary", {}).get("grand_total_usd", 0)

    prompt = f"""Write a comprehensive executive summary for a technical proposal.

ASSIGNMENT: {opportunity.get('title', '')}
CLIENT: {opportunity.get('client', '')}
DURATION: {opportunity.get('project_duration', '')}
BUDGET: ${budget_total:,} USD
LOCATION: {', '.join(opportunity.get('project_location', []))}

DELIVERABLES:
{json.dumps([d.get('name', '') for d in deliverables], indent=2)}

EVALUATION CRITERIA:
{json.dumps(eval_criteria, indent=2)}

CORTECH PROFILE:
{CORTECH_PROFILE}

TEAM COVERAGE: {matched_team_result.get('coverage_percent', 0)}% internal match

PAST WORK:
{_build_past_work_context()}

Write a 4-paragraph executive summary:
1. Context and the challenge the assignment addresses
2. Cortech's proposed approach and what makes it distinctive
3. Team composition highlights and coverage
4. Budget compliance and value for money statement

Professional, evidence-based, specific. 350-450 words.
Reference 2-3 specific past assignments as credibility evidence.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."""

    response = client.messages.create(
        model=CLAUDE_MODEL_PROPOSAL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return get_text(response)


def generate_methodology(analysis: dict) -> str:
    """Generate the detailed methodology section — longest and most important."""
    deliverables = analysis.get("deliverables", [])
    requirements = analysis.get("requirements", {})
    methodology_reqs = requirements.get("methodology_requirements", [])
    thematic_areas = requirements.get("thematic_areas", [])

    prompt = f"""Write a detailed methodology section for a technical proposal.

ASSIGNMENT: {analysis.get('opportunity', {}).get('title', '')}

DELIVERABLES REQUIRED:
{json.dumps(deliverables, indent=2)}

METHODOLOGY REQUIREMENTS FROM ToR:
{json.dumps(methodology_reqs, indent=2)}

THEMATIC AREAS:
{json.dumps(thematic_areas, indent=2)}

CORTECH'S STANDARD TOOLS:
- KoboToolbox for digital data collection
- SPSS for quantitative analysis
- NVivo for qualitative analysis
- Python/QGIS for spatial analysis
- OECD DAC evaluation criteria (Relevance, Effectiveness, Efficiency, Impact, Sustainability)
- Mixed-methods approach combining quantitative surveys with qualitative FGDs and KIIs
- Participatory approaches ensuring community voice
- Triangulation across multiple data sources

STRUCTURE THE METHODOLOGY AS:
1. Overall Approach (2-3 paragraphs on mixed methods rationale)
2. Phase-by-phase breakdown (Inception → Field Work → Analysis → Reporting)
3. Specific method for each deliverable (surveys, FGDs, KIIs, desk review)
4. Data quality assurance
5. Ethical considerations integration

Development sector professional language. Evidence-based. Specific tool names.
Reference KoboToolbox, SPSS, NVivo explicitly. 600-800 words.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."""

    response = client.messages.create(
        model=CLAUDE_MODEL_PROPOSAL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return get_text(response)


def generate_team_section(matched_team_result: dict, title: str) -> str:
    """Generate team composition section."""
    team = matched_team_result.get("matched_team", {})
    gaps = matched_team_result.get("gaps", [])

    team_list = []
    for role, match in team.items():
        team_list.append({
            "role": role,
            "person": match.get("consultant_name", "TBD"),
            "match_score": match.get("similarity_score", 0),
        })

    prompt = f"""Write a team composition section for a technical proposal.

ASSIGNMENT: {title}
MATCHED TEAM: {json.dumps(team_list, indent=2)}
GAPS REQUIRING EXTERNAL RECRUITMENT: {json.dumps(gaps, indent=2)}

CORTECH PROFILE:
{CORTECH_PROFILE}

Write:
1. Opening paragraph on overall team strength
2. Brief profile for each team member (2-3 sentences each)
3. Note on any external specialist to be recruited (if gaps exist)
4. Statement on team availability and commitment

Professional, confident tone. 300-400 words.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."""

    response = client.messages.create(
        model=CLAUDE_MODEL_PROPOSAL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return get_text(response)


def generate_work_plan(analysis: dict) -> str:
    """Generate workplan/Gantt description."""
    deliverables = analysis.get("deliverables", [])
    duration = analysis.get("opportunity", {}).get("project_duration", "3 months")

    prompt = f"""Create a work plan narrative and Gantt chart description for a proposal.

PROJECT DURATION: {duration}
DELIVERABLES:
{json.dumps(deliverables, indent=2)}

Create:
1. Phase breakdown with timing (e.g., Phase 1: Inception - Week 1-2)
2. For each phase: key activities and outputs
3. Milestone dates
4. Note on parallel vs sequential activities
5. A text-based Gantt table showing Month/Week vs Activities

Format the Gantt as a simple text table. Professional. 400-500 words.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."""

    response = client.messages.create(
        model=CLAUDE_MODEL_PROPOSAL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return get_text(response)


def generate_risk_register(analysis: dict) -> str:
    """Generate risk register."""
    location = analysis.get("opportunity", {}).get("project_location", ["East Africa"])
    thematic_areas = analysis.get("requirements", {}).get("thematic_areas", [])

    prompt = f"""Create a risk management section for a technical proposal.

PROJECT LOCATION: {location}
THEMATIC AREAS: {thematic_areas}

Generate 6-8 risks in this format for each:
| Risk Category | Description | Likelihood | Impact | Mitigation |

Include risks related to:
- Field access and security
- Data quality and collection
- Government engagement
- Community participation
- Timeline and budget
- Team availability

Format as a table followed by 2 paragraphs on overall risk management approach.
Professional development sector language. 300-400 words.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."""

    response = client.messages.create(
        model=CLAUDE_MODEL_PROPOSAL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return get_text(response)


def generate_org_profile_and_track_record(analysis: dict) -> str:
    """Generate 'Organisational Profile' + 'Related Previous Assignments'."""
    past_work = _build_past_work_context()

    prompt = f"""Write two sections for a Cortech Consulting Group technical proposal.

CORTECH PROFILE:
{CORTECH_PROFILE}

PAST ASSIGNMENTS:
{past_work}

SECTION 1 — ORGANISATIONAL PROFILE (300-400 words):
Cortech's history, registrations, certifications, geographic presence,
core thematic areas, and what distinguishes it from competitors.
Evidence-based, no generic claims.

SECTION 2 — RELATED PREVIOUS ASSIGNMENTS (table):
Format as a table: Project | Client | Value | Year | Relevance to this assignment
Use the past assignments listed above. For each, add one sentence
connecting it directly to THIS opportunity's requirements.

ASSIGNMENT CONTEXT (for relevance-mapping):
{analysis.get('opportunity', {}).get('title', '')}
{json.dumps(analysis.get('requirements', {}).get('thematic_areas', []), indent=2)}

Return both sections with clear headers. Professional development
consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims." """

    response = client.messages.create(
        model=CLAUDE_MODEL_PROPOSAL,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    return get_text(response)


def generate_introduction_and_framework(analysis: dict) -> str:
    """
    Generate 'Introduction and Background' (6 sub-sections per
    PROPOSAL_STRUCTURE) plus 'Conceptual Framework'.
    """
    opportunity = analysis.get("opportunity", {})
    deliverables = analysis.get("deliverables", [])
    eval_criteria = analysis.get("evaluation_criteria", [])

    prompt = f"""Write two sections for a Cortech Consulting Group technical proposal.

ASSIGNMENT: {opportunity.get('title', '')}
CLIENT: {opportunity.get('client', '')}

DELIVERABLES:
{json.dumps(deliverables, indent=2)}

EVALUATION CRITERIA:
{json.dumps(eval_criteria, indent=2)}

CORTECH PROFILE:
{CORTECH_PROFILE}

SECTION 1 — INTRODUCTION AND BACKGROUND, with these exact sub-headers:
5.1 Context and Strategic Importance
5.2 Purpose and Objectives
5.3 Our Interpretation of the Assignment
5.4 Key Evaluation Questions
5.5 Deliverables
5.6 Understanding of Success
Each sub-section 2-4 sentences. Specific to this assignment, not generic.

SECTION 2 — CONCEPTUAL FRAMEWORK (250-350 words):
The theoretical/analytical lens Cortech will apply (e.g. OECD DAC criteria,
theory of change, results framework) and why it fits this assignment.

Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims." """

    response = client.messages.create(
        model=CLAUDE_MODEL_PROPOSAL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return get_text(response)


def generate_analysis_plan(analysis: dict) -> str:
    """Generate 'Sampling Strategy' (if applicable) + 'Data Analysis Plan'."""
    methodology_reqs = analysis.get("requirements", {}).get("methodology_requirements", [])
    deliverables = analysis.get("deliverables", [])
    involves_survey = any(
        "survey" in str(d).lower() or "sample" in str(d).lower()
        for d in deliverables + methodology_reqs
    )
    sampling_instruction = (
        "SECTION 1 — SAMPLING STRATEGY (200-300 words): sample size "
        "approach, sampling method (probability/purposive), stratification "
        "logic, confidence level assumptions."
        if involves_survey else
        "This assignment does not appear to involve a survey — write one "
        "short paragraph noting the data collection approach does not "
        "require a probability sample, and explain the selection logic "
        "instead (e.g. purposive selection of KII/FGD participants)."
    )

    prompt = f"""Write sections for a Cortech Consulting Group technical proposal.

METHODOLOGY REQUIREMENTS:
{json.dumps(methodology_reqs, indent=2)}

DELIVERABLES:
{json.dumps(deliverables, indent=2)}

{sampling_instruction}

SECTION 2 — DATA ANALYSIS PLAN (250-350 words):
How quantitative data will be analyzed (SPSS, descriptive/inferential
approach) and qualitative data (NVivo, thematic analysis), and how the
two will be triangulated.

Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims." """

    response = client.messages.create(
        model=CLAUDE_MODEL_PROPOSAL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return get_text(response)


def generate_qa_and_ethics(analysis: dict) -> str:
    """Generate 'Quality Assurance Framework' + 'Ethical Considerations and Safeguarding'."""
    location = analysis.get("opportunity", {}).get("project_location", ["East Africa"])

    prompt = f"""Write two sections for a Cortech Consulting Group technical proposal.

PROJECT LOCATION: {location}
CORTECH PROFILE:
{CORTECH_PROFILE}

SECTION 1 — QUALITY ASSURANCE FRAMEWORK (250-350 words):
Data quality checks, peer review process, deliverable review stages,
client feedback loops.

SECTION 2 — ETHICAL CONSIDERATIONS AND SAFEGUARDING (250-350 words):
Reference Cortech's actual certifications: ISO certification, child
safeguarding policy, PSEA policy compliance. Cover informed consent,
data protection, protection of vulnerable groups given the project
location, and do-no-harm principles.

Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims." """

    response = client.messages.create(
        model=CLAUDE_MODEL_PROPOSAL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return get_text(response)