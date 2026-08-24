# intelligence/proposal_writer.py
import anthropic
import json
from loguru import logger
from utils.claude_helpers import get_text
from database.airtable_client import get_winning_proposals, log_agent_action, get_table
from database.supabase_client import get_embedding, supabase
from config import (
    CLAUDE_MODEL,
    CLAUDE_MODEL_PROPOSAL,
    CLAUDE_MAX_TOKENS,
    CORTECH_PROFILE,
    get_anthropic_client,
)

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


COMPLETENESS_RULES = """
MANDATORY WRITING STANDARDS:
- This is a client-facing, submission-ready document. Write complete sections only.
- Never stop mid-sentence, mid-bullet, mid-table row, or mid-heading.
- If you open a heading, write the full body under it before moving on.
- Align every claim with the ToR EVALUATION CRITERIA. Weighted criteria get proportionally more depth.
- Be specific: geographies, sample sizes, tools, dates, named past assignments, named experts.
- Do not invent evaluation criteria. Use those extracted from the ToR.
- Professional development-consulting tone. No hollow phrases ("we are excited",
  "we believe", "our team is passionate", "this proposal aims", "we are pleased").
"""

QUALITY_SUFFIX = (
    "\n\nFinish the entire section. Never end mid-sentence, mid-list, or mid-table. "
    "Explicitly satisfy the ToR evaluation criteria from your system context with "
    "specific, evidence-based claims — not generic consulting language."
)


def _eval_criteria_block(analysis: dict) -> str:
    criteria = analysis.get("evaluation_criteria") or []
    if not criteria:
        return (
            "EVALUATION CRITERIA: None were extracted from the ToR. Infer the likely "
            "scoring dimensions from the opportunity analysis (methodology, team, "
            "relevant experience, work plan, organisational capacity) and address "
            "each explicitly."
        )
    return (
        "EVALUATION CRITERIA FROM THE TOR (these decide the bid — write to them):\n"
        + json.dumps(criteria, indent=2)
    )


def _looks_truncated(text: str) -> bool:
    """True when the last line is an unfinished sentence, heading, or table stub."""
    t = (text or "").rstrip()
    if not t:
        return True
    last_line = t.splitlines()[-1].strip()
    if last_line.startswith("#"):
        return True
    if last_line.startswith("|---") or last_line.startswith("| ---"):
        return True
    if t.endswith(("...", "…")):
        return False
    if last_line.endswith("|") and last_line.count("|") >= 2:
        return False
    if t[-1] in ".!?\"'”’":
        return False
    return True


_TERMINATORS = ".!?"


def _is_table_separator_line(line: str) -> bool:
    s = line.strip().replace(" ", "")
    return s.startswith("|") and bool(s) and set(s) <= set("|:-")


def _ends_cleanly(line: str) -> bool:
    """A finished table row, or a sentence closed with terminal punctuation."""
    s = line.strip()
    if s.endswith("|") and s.count("|") >= 2:
        return True
    s = s.rstrip("\"'”’*)")
    return bool(s) and s[-1] in _TERMINATORS


def _trim_to_clean_end(text: str) -> str:
    """
    Last-resort guarantee that a section never reaches the client ending
    mid-word, mid-sentence, on a bare heading, or on a half-built table.
    Trims back to the last complete sentence or table row.

    This is the backstop for when the model still stops short after every
    continuation attempt — a client-facing document must never ship the
    "...tracked through KoboToolb" endings that earlier drafts contained.
    Returns the input unchanged when it already ends cleanly, and never
    returns an empty string.
    """
    lines = (text or "").rstrip().split("\n")
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        # A heading with no body written under it yet.
        if last.startswith("#"):
            lines.pop()
            continue
        # A table separator, or a header row with no data rows beneath it.
        if _is_table_separator_line(last):
            lines.pop()
            if lines and lines[-1].strip().startswith("|"):
                lines.pop()
            continue
        # A table row that was cut before its closing pipe.
        if last.startswith("|") and not _ends_cleanly(last):
            lines.pop()
            continue
        if _ends_cleanly(last):
            break
        # Mid-sentence: keep the line only if a substantial finished clause
        # survives the cut, otherwise drop it and re-test the line above.
        cut = max(last.rfind(c) for c in _TERMINATORS)
        if cut > 40:
            lines[-1] = last[: cut + 1]
            break
        lines.pop()
    cleaned = "\n".join(lines).rstrip()
    if cleaned:
        return cleaned
    # The line walk consumed everything (e.g. a section that is nothing but a
    # half-built table). Fall back to the last completed sentence anywhere in
    # the text rather than handing back the untrimmed, mid-sentence original.
    original = (text or "").strip()
    cut = max(original.rfind(c) for c in _TERMINATORS)
    return original[: cut + 1] if cut > 0 else original


# The first continuation prompt asked the model to "finish every remaining
# subsection", which invited it to keep opening new headings instead of
# closing the open one — a 600-word section ballooned past 1,700 words and
# was still cut off. Continuation must push toward closure, not coverage.
_CONTINUE_INSTRUCTION = (
    "Continue from exactly where you stopped, resuming mid-sentence if the "
    "text broke mid-sentence. Do not restart, do not repeat any sentence you "
    "have already written, and do not introduce headings or subsections "
    "beyond those the original instructions asked for. Finish the open "
    "sentence, complete any unfinished table or list, close out the "
    "subsection you were in, then stop. Closing the section is the priority, "
    "not adding new material."
)


def _build_shared_context(
    analysis: dict,
    extra_context: str = "",
    submission_type: str = "FULL_PROPOSAL",
) -> str:
    """Build once per proposal — identical bytes across all section calls for cache hits."""
    style_guide = _load_style_guide(submission_type)
    base = (
        f"{COMPLETENESS_RULES}\n\n"
        f"{_eval_criteria_block(analysis)}\n\n"
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

    writable = [
        k for k, v in sections.items()
        if isinstance(v, str) and k not in ("submission_type",)
    ]
    prompt = f"""Score this draft proposal against the evaluation criteria actually stated in the ToR — not generic categories.

EVALUATION CRITERIA FROM THE TOR:
{json.dumps(eval_criteria, indent=2)}

DRAFT:
{combined[:24000]}

Return ONLY valid JSON:
{{
  "overall_score": <0-100, grounded in the criteria above, not a guess>,
  "weakest_criterion": "<which stated criterion is weakest, and why, one sentence>",
  "strongest_criterion": "<which stated criterion is strongest, and why, one sentence>",
  "one_improvement": "<the single most impactful specific fix, referencing something specific in the draft>",
  "rewrite_section": "<exactly one of: {", ".join(writable)} — the section that most needs a rewrite to lift the score. Empty string if none>"
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
    """
    Generate a section and continue if the model hits the output cap
    or otherwise stops mid-sentence. Unfinished sentences in emailed
    drafts were caused by max_tokens cutoffs with no continuation.
    """
    system = [{
        "type": "text",
        "text": shared_context,
        "cache_control": {"type": "ephemeral"},
    }]
    messages = [{"role": "user", "content": user_prompt}]
    assembled = ""
    max_attempts = 4
    # Continuations must share one output ceiling. Re-applying the full
    # max_tokens per attempt let a section run to 4x its intended length
    # while still ending mid-sentence.
    total_budget = int(max_tokens * 1.5)
    spent = 0

    for attempt in range(max_attempts):
        remaining = min(max_tokens, total_budget - spent)
        if remaining < 256:
            logger.warning(
                f"  [{section_name}] output budget exhausted "
                f"({spent}/{total_budget} tokens) — trimming to a clean close"
            )
            break
        response = client.messages.create(
            model=model,
            max_tokens=remaining,
            system=system,
            messages=messages,
        )
        label = section_name if attempt == 0 else f"{section_name}+cont{attempt}"
        _log_cache_usage(response, label)
        assembled += get_text(response)
        spent += getattr(response.usage, "output_tokens", 0) or 0
        stop = getattr(response, "stop_reason", None)
        if stop != "max_tokens" and not _looks_truncated(assembled):
            return assembled.strip()
        logger.warning(
            f"  [{section_name}] output truncated "
            f"(stop_reason={stop}, attempt={attempt + 1}/{max_attempts}) — continuing"
        )
        messages = [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": assembled},
            {"role": "user", "content": _CONTINUE_INSTRUCTION},
        ]

    cleaned = _trim_to_clean_end(assembled)
    if cleaned != assembled.strip():
        logger.warning(
            f"  [{section_name}] still unfinished after {max_attempts} attempts — "
            f"trimmed {len(assembled.strip()) - len(cleaned)} trailing chars "
            f"back to the last complete sentence"
        )
    return cleaned


def _repair_weakest_section(
    sections: dict,
    analysis: dict,
    shared_context: str,
) -> dict:
    """One targeted rewrite of the weakest section when the self-score is below 80."""
    score = sections.get("quality_score") or {}
    overall = score.get("overall_score")
    target = score.get("rewrite_section") or ""
    if not isinstance(overall, (int, float)) or overall >= 80:
        return sections
    if target in ("submission_type", "lightweight_reason", "quality_score"):
        return sections
    if target not in sections or not isinstance(sections.get(target), str):
        return sections
    if not sections[target].strip():
        return sections
    logger.info(f"  Repairing weakest section '{target}' (self-score={overall})")
    user_prompt = f"""Rewrite the following proposal section to submission-ready, top-tier quality.

{_eval_criteria_block(analysis)}

THIS SECTION WAS WEAKEST AGAINST: {score.get("weakest_criterion", "")}
REQUIRED FIX: {score.get("one_improvement", "")}

CURRENT DRAFT:
{sections[target]}

Rewrite the complete section from start to finish. Keep accurate facts (names, dates, sample sizes, past assignments, named experts). Strengthen alignment with the evaluation criteria. Finish every sentence and every subsection. Do not leave headings without body text.{QUALITY_SUFFIX}"""
    sections[target] = _generate_section(
        f"{target}_repair",
        CLAUDE_MODEL_PROPOSAL,
        CLAUDE_MAX_TOKENS,
        shared_context,
        user_prompt,
    )
    return sections


def generate_eoi(
    analysis: dict,
    matched_team_result: dict,
    opportunity_id: str = None,
) -> dict:
    """
    Full Expression of Interest aligned to ToR evaluation/shortlisting
    criteria. House style: letter of interest, firm presentation,
    relevant experience table, resources in staff — complete prose,
    not a profile dump.
    """
    opportunity = analysis.get("opportunity", {})
    title = opportunity.get("title", "Unknown Assignment")
    client_name = opportunity.get("client", "Client")
    donor = opportunity.get("donor", "")

    extra_context = (
        get_relevant_lessons(client_name, donor)
        + get_donor_intelligence(donor, client_name)
    )
    shared_context = _build_shared_context(
        analysis, extra_context, submission_type="EOI"
    )
    logger.info(f"Generating EOI for: {title[:60]}")

    team_summary = json.dumps({
        role: {
            "name": m.get("consultant_name"),
            "score": m.get("similarity_score"),
            "justification": m.get("justification", ""),
        }
        for role, m in matched_team_result.get("matched_team", {}).items()
        if m.get("consultant_name") != "EXTERNAL RECRUITMENT NEEDED"
    }, indent=2)

    cover_letter = _generate_section(
        "eoi_cover",
        CLAUDE_MODEL_PROPOSAL,
        4096,
        shared_context,
        f"""Write a complete Expression of Interest cover letter for Cortech Consulting Group.

ASSIGNMENT: {title}
CLIENT: {client_name}

This is an EOI / shortlisting submission, not a full technical proposal.
Do not write a methodology. Do write a finished letter of 4-5 paragraphs:
- Addressed to the procurement committee
- Clear statement of interest and understanding of the assignment
- Two or three specific past assignments that match this ToR
- Confirmation that Cortech can field a qualified team and will submit a
  full technical and financial proposal if shortlisted
- Sign off: Daud Hussein Ibrahim, Director, Cortech Consulting Group

{_eval_criteria_block(analysis)}{QUALITY_SUFFIX}""",
    )

    firm_profile = _generate_section(
        "eoi_firm",
        CLAUDE_MODEL_PROPOSAL,
        CLAUDE_MAX_TOKENS,
        shared_context,
        f"""Write the 'Presentation of Cortech Consulting Group' section for this EOI.

ASSIGNMENT: {title}
CLIENT: {client_name}

400-600 words covering history, registrations, geographic presence,
thematic competence, and why the firm is qualified for THIS assignment.
Map credentials to the ToR evaluation/shortlisting criteria. Use only
facts from the CORTECH PROFILE. Complete every paragraph.

{_eval_criteria_block(analysis)}{QUALITY_SUFFIX}""",
    )

    relevant_experience = _generate_section(
        "eoi_experience",
        CLAUDE_MODEL_PROPOSAL,
        CLAUDE_MAX_TOKENS,
        shared_context,
        f"""Write the 'Relevant Experience of Completed Assignments' section for this EOI.

ASSIGNMENT: {title}

Format as a markdown table: Project | Client | Value | Year | Relevance to this assignment
Use the past assignments in your system context. For each row, one sentence
connecting that assignment to THIS ToR (geography, theme, method, or client type).
Follow the table with 2-3 paragraphs of narrative that explicitly address
the experience-related evaluation criteria.

{_eval_criteria_block(analysis)}{QUALITY_SUFFIX}""",
    )

    key_experts = _generate_section(
        "eoi_experts",
        CLAUDE_MODEL_PROPOSAL,
        4096,
        shared_context,
        f"""Write the 'Resources in Staff' section for this EOI.

ASSIGNMENT: {title}
PROPOSED EXPERTS:
{team_summary}

For each named expert: role on this assignment, 4-6 sentence bio, and
why they satisfy the ToR's personnel/shortlisting criteria. If a role
is unfilled, state the recruitment profile in 3-4 complete sentences.
Close with a short availability and commitment paragraph.

{_eval_criteria_block(analysis)}{QUALITY_SUFFIX}""",
    )

    sections = {
        "submission_type": "EOI",
        "cover_letter": cover_letter,
        "firm_profile": firm_profile,
        "relevant_experience": relevant_experience,
        "key_experts": key_experts,
    }
    sections["quality_score"] = generate_quality_self_score(sections, analysis)
    sections = _repair_weakest_section(sections, analysis, shared_context)

    try:
        log_agent_action(
            action_type="Proposal",
            description=f"Generated EOI for: {title[:60]}",
            opportunity_id=opportunity_id,
            tokens_used=8000,
            status="Success",
        )
    except Exception:
        pass

    logger.success("EOI generation complete!")
    return sections


def generate_proposal(
    analysis: dict,
    matched_team_result: dict,
    budget: dict,
    compliance_matrix: str,
    opportunity_id: str = None,
) -> dict:
    """
    Generate a proposal draft routed by bid_recommendation:
      BID   → all 10 sections on the proposal model, complete and
              aligned to ToR evaluation criteria
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
        sections = _repair_weakest_section(sections, analysis, shared_context)
        try:
            log_agent_action(
                action_type="Proposal",
                description=f"Generated WATCH quick-flag for: {title[:60]}",
                opportunity_id=opportunity_id,
                tokens_used=3000,
                status="Success",
            )
        except Exception:
            pass
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
            matched_team_result, title, shared_context, analysis,
        ),
        "work_plan": generate_work_plan(analysis, shared_context),
    }
    sections["quality_score"] = generate_quality_self_score(sections, analysis)
    sections = _repair_weakest_section(sections, analysis, shared_context)

    try:
        log_agent_action(
            action_type="Proposal",
            description=f"Generated full draft proposal for: {title[:60]}",
            opportunity_id=opportunity_id,
            tokens_used=15000,
            status="Success",
        )
    except Exception:
        pass
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
- Write a complete letter — every sentence finished
- 400-500 words{QUALITY_SUFFIX}"""

    return _generate_section("cover_letter", model, 4096, shared_context, user_prompt)


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

Professional, evidence-based, specific. 450-650 words. Complete all four
paragraphs including the budget/value statement — do not stop mid-paragraph.
Reference 2-3 specific past assignments as credibility evidence.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section("executive_summary", model, 4096, shared_context, user_prompt)


def generate_methodology(analysis: dict, shared_context: str) -> str:
    """Generate the detailed methodology section — longest and most important."""
    user_prompt = f"""Write a detailed methodology section for a technical proposal.

ASSIGNMENT: {analysis.get('opportunity', {}).get('title', '')}

Use deliverables, methodology requirements, thematic areas, and
evaluation criteria from the OPPORTUNITY ANALYSIS in your system context.

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
Reference KoboToolbox, SPSS, NVivo explicitly. 900-1400 words.
Complete ALL five numbered parts — do not stop inside part 3, 4, or 5.
Map the method explicitly to the ToR evaluation criteria (especially any
methodology / technical-approach weighting).
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "methodology", CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, shared_context, user_prompt,
    )


def generate_team_section(
    matched_team_result: dict,
    title: str,
    shared_context: str,
    analysis: dict | None = None,
) -> str:
    """Generate team composition section."""
    team = matched_team_result.get("matched_team", {})
    gaps = matched_team_result.get("gaps", [])
    analysis = analysis or {}

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
{_eval_criteria_block(analysis)}

Write:
1. Opening paragraph on overall team strength against the ToR personnel criteria
2. Brief profile for each team member (4-6 sentences each — qualifications, relevant assignments, role on this job)
3. Note on any external specialist to be recruited (if gaps exist)
4. Statement on team availability and commitment

Professional, confident tone. 400-600 words. Complete every bio.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "team_section", CLAUDE_MODEL_PROPOSAL, 4096, shared_context, user_prompt,
    )


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

Format the Gantt as a complete markdown table with every phase/activity row filled.
Professional. 500-700 words. Do not stop after a heading such as "GANTT CHART".
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "work_plan", CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, shared_context, user_prompt,
    )


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

Format as a complete markdown table (every row finished) followed by 2 paragraphs
on overall risk management approach.
Professional development sector language. 400-550 words.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "risk_register", CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, shared_context, user_prompt,
    )


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

Return both sections with clear headers. Complete every table row.
Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "org_profile_and_track_record",
        CLAUDE_MODEL_PROPOSAL,
        CLAUDE_MAX_TOKENS,
        shared_context,
        user_prompt,
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

SECTION 2 — CONCEPTUAL FRAMEWORK (300-400 words):
The theoretical/analytical lens Cortech will apply (e.g. OECD DAC criteria,
theory of change, results framework) and why it fits this assignment.
Complete this section in full — do not stop mid-sentence.

Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "introduction_and_framework",
        CLAUDE_MODEL_PROPOSAL,
        CLAUDE_MAX_TOKENS,
        shared_context,
        user_prompt,
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

Use methodology requirements, deliverables, and evaluation criteria from the OPPORTUNITY ANALYSIS in your system context.

{sampling_instruction}

SECTION 2 — DATA ANALYSIS PLAN (250-350 words):
How quantitative data will be analyzed (SPSS, descriptive/inferential
approach) and qualitative data (NVivo, thematic analysis), and how the
two will be triangulated.

Professional development consulting tone. Complete both sections — do not stop
inside the triangulation paragraph. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "analysis_plan", CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, shared_context, user_prompt,
    )


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
location, and do-no-harm principles. Complete both sections in full.

Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "qa_and_ethics", CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, shared_context, user_prompt,
    )
