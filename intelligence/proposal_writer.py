# intelligence/proposal_writer.py
import anthropic
import json
from loguru import logger
from utils.claude_helpers import get_text
from database.airtable_client import get_winning_proposals, log_agent_action, get_table
from database.supabase_client import get_embedding, supabase
from config import CLAUDE_MODEL, CLAUDE_MODEL_PROPOSAL, CORTECH_PROFILE, get_anthropic_client

client = get_anthropic_client()


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


def _load_style_guide(submission_type: str) -> str:
    label = "eoi" if submission_type == "EOI" else "full_proposal"
    path = f"intelligence/style_guides/{label}_style.md"
    try:
        with open(path) as f:
            content = f.read().strip()
            if not content:
                return ""
            return f"CORTECH HOUSE STYLE ({label.replace('_', ' ').upper()}):\n{content}"
    except FileNotFoundError:
        logger.warning(
            f"No style guide at {path} — run extract_style_guide.py first. Proceeding without it."
        )
        return ""


def _build_shared_context(
    analysis: dict,
    extra_context: str = "",
    submission_type: str = "FULL_PROPOSAL",
) -> str:
    """Build once per proposal — identical bytes across all section calls for cache hits."""
    style_guide = _load_style_guide(submission_type)
    base = (
        f"CORTECH PROFILE:\n{CORTECH_PROFILE}\n\n"
        f"OPPORTUNITY ANALYSIS:\n{json.dumps(analysis, indent=2, sort_keys=True)}\n\n"
        f"{_build_past_work_context()}"
    )
    if style_guide:
        base = f"{base}\n\n{style_guide}"
    if extra_context.strip():
        return f"{base}\n\n{extra_context.strip()}"
    return base


def get_relevant_lessons(client_name: str, donor: str) -> str:
    """Non-fatal on any failure — empty string means no past-lesson context."""
    if not client_name and not donor:
        return ""
    try:
        embedding = get_embedding(f"proposals for {client_name} {donor}")
        results = supabase.rpc("match_win_loss_memory", {
            "query_embedding": embedding,
            "match_threshold": 0.70,
            "match_count": 3,
        }).execute()
    except Exception as e:
        logger.warning(f"Could not fetch win/loss lessons (non-fatal): {e}")
        return ""
    if not results.data:
        return ""
    lines = [f"Past {r['outcome']}: {r['lessons']}" for r in results.data]
    return "\nRELEVANT PAST PERFORMANCE LESSONS:\n" + "\n".join(lines) + "\n"


def get_donor_intelligence(donor: str, client_name: str) -> str:
    """Pull donor/client preferences from Airtable DONOR_INTELLIGENCE table."""
    if not donor and not client_name:
        return ""
    try:
        safe_donor = (donor or "").replace("'", "\\'")
        safe_client = (client_name or "").replace("'", "\\'")
        table = get_table("donor_intelligence")
        records = table.all(
            formula=(
                f"OR(FIND('{safe_donor}', {{donor_name}}), "
                f"FIND('{safe_client}', {{donor_name}}))"
            )
        )
    except Exception as e:
        logger.warning(f"Could not fetch donor intelligence (non-fatal): {e}")
        return ""
    if not records:
        return ""
    intel = records[0]["fields"]
    return f"""
DONOR INTELLIGENCE FOR {donor or client_name}:
Preferred frameworks: {intel.get("preferred_frameworks", "None on file")}
Required sections: {intel.get("required_sections", "Standard")}
Evaluation priorities: {intel.get("evaluation_priorities", "Unknown")}
Red lines to avoid: {intel.get("red_lines", "None known")}
"""


def generate_quality_self_score(sections: dict, analysis: dict) -> dict:
    """
    One Claude call, evaluating the finished draft against the ToR's own
    stated evaluation criteria — not invented generic categories.
    """
    eval_criteria = analysis.get("evaluation_criteria", [])
    combined = "\n\n".join(
        f"[{k}]\n{v}" for k, v in sections.items() if isinstance(v, str)
    )

    prompt = f"""Score this draft proposal against the evaluation criteria actually stated in the ToR — not generic categories.

EVALUATION CRITERIA FROM THE TOR:
{json.dumps(eval_criteria, indent=2)}

DRAFT:
{combined[:8000]}

Return ONLY valid JSON:
{{
  "overall_score": <0-100, grounded in the criteria above, not a guess>,
  "weakest_criterion": "<which stated criterion is weakest, and why, one sentence>",
  "strongest_criterion": "<which stated criterion is strongest, and why, one sentence>",
  "one_improvement": "<the single most impactful specific fix, referencing something specific in the draft>"
}}"""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(get_text(response))
    except Exception as e:
        logger.warning(f"Quality self-score failed (non-fatal): {e}")
        return {}


def _log_cache_usage(response, section_name: str) -> None:
    """Log prompt-cache stats — verify cache_read > 0 on calls 2+ during testing."""
    usage = response.usage
    creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    logger.info(
        f"  [{section_name}] cache_creation={creation} cache_read={read} "
        f"input={usage.input_tokens} output={usage.output_tokens}"
    )


def _generate_section(
    section_name: str,
    model: str,
    max_tokens: int,
    shared_context: str,
    user_prompt: str,
) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{
            "type": "text",
            "text": shared_context,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_prompt}],
    )
    _log_cache_usage(response, section_name)
    return get_text(response)


def generate_eoi(
    analysis: dict,
    matched_team_result: dict,
    opportunity_id: str = None,
) -> dict:
    """
    Lightweight path for opportunities classified as EOI/REOI. Four
    pieces, two of which reuse existing content with zero new API
    calls. Total: ~2 Claude calls, vs 10 for a full proposal.
    """
    opportunity = analysis.get("opportunity", {})
    title = opportunity.get("title", "Unknown Assignment")
    client_name = opportunity.get("client", "Client")

    style_prefix = _load_style_guide("EOI")
    if style_prefix:
        style_prefix = f"{style_prefix}\n\n"

    cover_letter_prompt = f"""{style_prefix}Write a brief Expression of Interest cover letter for Cortech Consulting Group.

ASSIGNMENT: {title}
CLIENT: {client_name}

This is an EXPRESSION OF INTEREST, not a full technical proposal —
keep it to 2-3 short paragraphs: state clear interest in the
opportunity, briefly indicate relevant capability, note that a full
technical and financial proposal will follow if shortlisted. Do not
describe methodology — that belongs in the full proposal stage, not
here."""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": cover_letter_prompt}],
    )
    cover_letter = get_text(response)

    firm_profile = CORTECH_PROFILE
    relevant_experience = _build_past_work_context()

    team_summary = json.dumps({
        role: {
            "name": m.get("consultant_name"),
            "score": m.get("similarity_score"),
        }
        for role, m in matched_team_result.get("matched_team", {}).items()
        if m.get("consultant_name") != "EXTERNAL RECRUITMENT NEEDED"
    }, indent=2)
    experts_prompt = f"""{style_prefix}Write brief 2-3 sentence professional bios for each proposed key expert below, suitable for an Expression of Interest submission (not a full CV, not a full team narrative).

PROPOSED EXPERTS:
{team_summary}

Return plain text, one short bio per named expert."""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": experts_prompt}],
    )
    key_experts = get_text(response)

    try:
        log_agent_action(
            action_type="Proposal",
            description=f"Generated EOI for: {title[:60]}",
            opportunity_id=opportunity_id,
            tokens_used=2000,
            status="Success",
        )
    except Exception:
        pass

    return {
        "submission_type": "EOI",
        "cover_letter": cover_letter,
        "firm_profile": firm_profile,
        "relevant_experience": relevant_experience,
        "key_experts": key_experts,
    }


def generate_proposal(
    analysis: dict,
    matched_team_result: dict,
    budget: dict,
    compliance_matrix: str,
    opportunity_id: str = None,
) -> dict:
    """
    Generate a proposal draft routed by bid_recommendation:
      BID   → all 10 sections (4 on proposal model, 6 on analysis model)
      WATCH → cover letter + executive summary only (analysis model)
    NO-BID callers should not invoke this function.
    """
    recommendation = analysis.get("bid_analysis", {}).get("bid_recommendation", "WATCH")
    opportunity = analysis.get("opportunity", {})
    title = opportunity.get("title", "Unknown Assignment")
    client_name = opportunity.get("client", "Client")
    donor = opportunity.get("donor", "")
    deadline = opportunity.get("submission_deadline", "TBD")

    extra_context = (
        get_relevant_lessons(client_name, donor)
        + get_donor_intelligence(donor, client_name)
    )
    shared_context = _build_shared_context(
        analysis, extra_context, submission_type="FULL_PROPOSAL"
    )
    logger.info(f"Generating proposal ({recommendation}) for: {title[:60]}")

    if recommendation == "WATCH":
        sections = {
            "cover_letter": generate_cover_letter(
                title, client_name, deadline, analysis, shared_context,
                model=CLAUDE_MODEL,
            ),
            "executive_summary": generate_executive_summary(
                analysis, matched_team_result, budget, shared_context,
                model=CLAUDE_MODEL,
            ),
            "lightweight": True,
            "lightweight_reason": "WATCH recommendation — quick flag, not a full draft",
        }
        sections["quality_score"] = generate_quality_self_score(sections, analysis)
        log_agent_action(
            action_type="Proposal",
            description=f"Generated WATCH quick-flag for: {title[:60]}",
            opportunity_id=opportunity_id,
            tokens_used=3000,
            status="Success",
        )
        logger.success("WATCH quick-flag generation complete!")
        return sections

    # BID — full 10-section draft
    sections = {
        "cover_letter": generate_cover_letter(
            title, client_name, deadline, analysis, shared_context,
        ),
        "executive_summary": generate_executive_summary(
            analysis, matched_team_result, budget, shared_context,
        ),
        "org_profile_and_track_record": generate_org_profile_and_track_record(
            analysis, shared_context,
        ),
        "introduction_and_framework": generate_introduction_and_framework(
            analysis, shared_context,
        ),
        "methodology": generate_methodology(analysis, shared_context),
        "analysis_plan": generate_analysis_plan(analysis, shared_context),
        "qa_and_ethics": generate_qa_and_ethics(analysis, shared_context),
        "risk_register": generate_risk_register(analysis, shared_context),
        "team_section": generate_team_section(
            matched_team_result, title, shared_context,
        ),
        "work_plan": generate_work_plan(analysis, shared_context),
    }
    sections["quality_score"] = generate_quality_self_score(sections, analysis)

    log_agent_action(
        action_type="Proposal",
        description=f"Generated full draft proposal for: {title[:60]}",
        opportunity_id=opportunity_id,
        tokens_used=15000,
        status="Success",
    )
    logger.success("Proposal generation complete!")
    return sections


def generate_cover_letter(
    title: str,
    client_name: str,
    deadline: str,
    analysis: dict,
    shared_context: str,
    model: str = CLAUDE_MODEL_PROPOSAL,
) -> str:
    """Generate professional cover letter."""
    strengths = analysis.get("bid_analysis", {}).get("key_strengths", [])

    user_prompt = f"""Write a professional cover letter for Cortech Consulting Group's technical proposal.

ASSIGNMENT: {title}
CLIENT: {client_name}
SUBMISSION DATE: {deadline}

KEY STRENGTHS FOR THIS BID:
{json.dumps(strengths, indent=2)}

Use the CORTECH PROFILE and OPPORTUNITY ANALYSIS provided in your system context.

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

    return _generate_section("cover_letter", model, 800, shared_context, user_prompt)


def generate_executive_summary(
    analysis: dict,
    matched_team_result: dict,
    budget: dict,
    shared_context: str,
    model: str = CLAUDE_MODEL_PROPOSAL,
) -> str:
    """Generate executive summary."""
    opportunity = analysis.get("opportunity", {})
    budget_total = budget.get("summary", {}).get("grand_total_usd", 0)

    user_prompt = f"""Write a comprehensive executive summary for a technical proposal.

ASSIGNMENT: {opportunity.get('title', '')}
CLIENT: {opportunity.get('client', '')}
DURATION: {opportunity.get('project_duration', '')}
BUDGET: ${budget_total:,} USD
LOCATION: {', '.join(opportunity.get('project_location', []))}

TEAM COVERAGE: {matched_team_result.get('coverage_percent', 0)}% internal match

Use the CORTECH PROFILE, past assignments, and full OPPORTUNITY ANALYSIS in your system context.

Write a 4-paragraph executive summary:
1. Context and the challenge the assignment addresses
2. Cortech's proposed approach and what makes it distinctive
3. Team composition highlights and coverage
4. Budget compliance and value for money statement

Professional, evidence-based, specific. 350-450 words.
Reference 2-3 specific past assignments as credibility evidence.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."""

    return _generate_section("executive_summary", model, 1000, shared_context, user_prompt)


def generate_methodology(analysis: dict, shared_context: str) -> str:
    """Generate the detailed methodology section — longest and most important."""
    user_prompt = f"""Write a detailed methodology section for a technical proposal.

ASSIGNMENT: {analysis.get('opportunity', {}).get('title', '')}

Use deliverables, methodology requirements, and thematic areas from the OPPORTUNITY ANALYSIS in your system context.

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

    return _generate_section(
        "methodology", CLAUDE_MODEL_PROPOSAL, 1500, shared_context, user_prompt,
    )


def generate_team_section(
    matched_team_result: dict,
    title: str,
    shared_context: str,
) -> str:
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

    user_prompt = f"""Write a team composition section for a technical proposal.

ASSIGNMENT: {title}
MATCHED TEAM: {json.dumps(team_list, indent=2)}
GAPS REQUIRING EXTERNAL RECRUITMENT: {json.dumps(gaps, indent=2)}

Use the CORTECH PROFILE in your system context for team credentials.

Write:
1. Opening paragraph on overall team strength
2. Brief profile for each team member (2-3 sentences each)
3. Note on any external specialist to be recruited (if gaps exist)
4. Statement on team availability and commitment

Professional, confident tone. 300-400 words.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."""

    return _generate_section("team_section", CLAUDE_MODEL, 800, shared_context, user_prompt)


def generate_work_plan(analysis: dict, shared_context: str) -> str:
    """Generate workplan/Gantt description."""
    duration = analysis.get("opportunity", {}).get("project_duration", "3 months")

    user_prompt = f"""Create a work plan narrative and Gantt chart description for a proposal.

PROJECT DURATION: {duration}

Use deliverables from the OPPORTUNITY ANALYSIS in your system context.

Create:
1. Phase breakdown with timing (e.g., Phase 1: Inception - Week 1-2)
2. For each phase: key activities and outputs
3. Milestone dates
4. Note on parallel vs sequential activities
5. A text-based Gantt table showing Month/Week vs Activities

Format the Gantt as a simple text table. Professional. 400-500 words.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."""

    return _generate_section("work_plan", CLAUDE_MODEL, 1000, shared_context, user_prompt)


def generate_risk_register(analysis: dict, shared_context: str) -> str:
    """Generate risk register."""
    location = analysis.get("opportunity", {}).get("project_location", ["East Africa"])
    thematic_areas = analysis.get("requirements", {}).get("thematic_areas", [])

    user_prompt = f"""Create a risk management section for a technical proposal.

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

    return _generate_section("risk_register", CLAUDE_MODEL, 800, shared_context, user_prompt)


def generate_org_profile_and_track_record(analysis: dict, shared_context: str) -> str:
    """Generate 'Organisational Profile' + 'Related Previous Assignments'."""
    user_prompt = f"""Write two sections for a Cortech Consulting Group technical proposal.

Use the CORTECH PROFILE and past assignments from your system context.

SECTION 1 — ORGANISATIONAL PROFILE (300-400 words):
Cortech's history, registrations, certifications, geographic presence,
core thematic areas, and what distinguishes it from competitors.
Evidence-based, no generic claims.

SECTION 2 — RELATED PREVIOUS ASSIGNMENTS (table):
Format as a table: Project | Client | Value | Year | Relevance to this assignment
Use the past assignments listed in context. For each, add one sentence
connecting it directly to THIS opportunity's requirements.

ASSIGNMENT CONTEXT (for relevance-mapping):
{analysis.get('opportunity', {}).get('title', '')}
{json.dumps(analysis.get('requirements', {}).get('thematic_areas', []), indent=2)}

Return both sections with clear headers. Professional development
consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims." """

    return _generate_section(
        "org_profile_and_track_record", CLAUDE_MODEL, 1200, shared_context, user_prompt,
    )


def generate_introduction_and_framework(analysis: dict, shared_context: str) -> str:
    """
    Generate 'Introduction and Background' (6 sub-sections per
    PROPOSAL_STRUCTURE) plus 'Conceptual Framework'.
    """
    opportunity = analysis.get("opportunity", {})

    user_prompt = f"""Write two sections for a Cortech Consulting Group technical proposal.

ASSIGNMENT: {opportunity.get('title', '')}
CLIENT: {opportunity.get('client', '')}

Use deliverables and evaluation criteria from the OPPORTUNITY ANALYSIS in your system context.

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

    return _generate_section(
        "introduction_and_framework", CLAUDE_MODEL_PROPOSAL, 1500, shared_context, user_prompt,
    )


def generate_analysis_plan(analysis: dict, shared_context: str) -> str:
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

    user_prompt = f"""Write sections for a Cortech Consulting Group technical proposal.

Use methodology requirements and deliverables from the OPPORTUNITY ANALYSIS in your system context.

{sampling_instruction}

SECTION 2 — DATA ANALYSIS PLAN (250-350 words):
How quantitative data will be analyzed (SPSS, descriptive/inferential
approach) and qualitative data (NVivo, thematic analysis), and how the
two will be triangulated.

Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims." """

    return _generate_section("analysis_plan", CLAUDE_MODEL, 1000, shared_context, user_prompt)


def generate_qa_and_ethics(analysis: dict, shared_context: str) -> str:
    """Generate 'Quality Assurance Framework' + 'Ethical Considerations and Safeguarding'."""
    location = analysis.get("opportunity", {}).get("project_location", ["East Africa"])

    user_prompt = f"""Write two sections for a Cortech Consulting Group technical proposal.

PROJECT LOCATION: {location}

Use the CORTECH PROFILE in your system context for certifications and policies.

SECTION 1 — QUALITY ASSURANCE FRAMEWORK (250-350 words):
Data quality checks, peer review process, deliverable review stages,
client feedback loops.

SECTION 2 — ETHICAL CONSIDERATIONS AND SAFEGUARDING (250-350 words):
Reference Cortech's actual certifications: ISO certification, child
safeguarding policy, PSEA policy compliance. Cover informed consent,
data protection, protection of vulnerable groups given the project
location, and do-no-harm principles.

Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims." """

    return _generate_section("qa_and_ethics", CLAUDE_MODEL, 1000, shared_context, user_prompt)
