# intelligence/proposal_writer.py
import anthropic
import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from loguru import logger
from utils.claude_helpers import get_text
from utils.money_scrub import (
    contains_monetary_amount,
    find_monetary_amounts,
    strip_monetary_amounts,
)
from intelligence.tender_reader import build_tor_brief, tender_documents_block
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


# Contract values are deliberately absent from this list. It feeds the
# writing context, and a technical proposal must carry no monetary figure —
# see NO_MONETARY_RULE and utils/money_scrub.py.
CORTECH_PAST_WORK = """
RELEVANT PAST ASSIGNMENTS (use as references in proposal):
1. End-of-Project Evaluation — IGAD Land Governance Programme
   Client: IGAD / Swedish Embassy | Year: 2026
   Description: Comprehensive evaluation of land governance in IGAD region

2. Assessment of Federal MOH Capacity — Somalia
   Client: World Bank | Year: 2025
   Description: Ministry of Health institutional assessment across Somalia

3. Free Movement of Persons Framework — Africa
   Client: IOM & African Union | Year: 2024
   Description: MEL framework development for continental protocol

4. Green Skills Documentation — Baidoa, Somalia
   Client: GREDO/DANIDA/Save the Children | Year: 2025
   Description: Best practices documentation for youth employment project

5. Endline Evaluation — Water & Livelihoods, Somalia
   Client: Arche Nova | Year: 2025
   Description: WASH and livelihoods project evaluation

6. Financial Services Mapping — Refugees Kenya
   Client: DRC Kenya | Year: 2025
   Description: FSP mapping for refugees in Garissa, Nairobi, Turkana

7. Civil Society Evaluation — Chukua Control, Kenya
   Client: Welthungerhilfe | Year: 2025
   Description: Endline evaluation of civil society empowerment project
"""


def _build_past_work_context() -> str:
    """
    Pull real winning proposals from Airtable for use as proposal evidence.
    Falls back to the static CORTECH_PAST_WORK list if Airtable returns
    nothing, so proposal generation never blocks on this.

    contract_value_usd is read from Airtable for other purposes but is
    never surfaced here — the drafted proposal must state no amounts.
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
            f"Year: {w.get('year', 'N/A')}\n"
            f"   Description: {w.get('methodology_approach', '')[:200]}"
        )
    return strip_monetary_amounts("\n\n".join(lines))[0]


_STYLE_GUIDE_DIR = Path(__file__).resolve().parent / "style_guides"


def _load_style_guide(submission_type: str) -> str:
    label = "eoi" if submission_type == "EOI" else "full_proposal"
    path = _STYLE_GUIDE_DIR / f"{label}_style.md"
    try:
        content = path.read_text().strip()
    except FileNotFoundError:
        logger.warning(
            f"No style guide at {path} — run extract_style_guide.py first. "
            "Proceeding without it."
        )
        return ""
    if not content:
        return ""
    return f"CORTECH HOUSE STYLE ({label.replace('_', ' ').upper()}):\n{content}"


_EXEMPLAR_MARKER_RE = re.compile(r"<!--\s*section:\s*([a-z_]+)\s*-->")
_EXEMPLARS_CACHE: dict[str, str] | None = None

# Generator section key → exemplar key in voice_exemplars.md. Several
# generators write two structural sections at once, so they map onto the
# closest single corpus section.
_EXEMPLAR_KEY_FOR = {
    "cover_letter": "cover_letter",
    "executive_summary": "executive_summary",
    "org_profile_and_track_record": "org_profile",
    "introduction_and_framework": "understanding",
    "methodology": "methodology",
    "analysis_plan": "analysis",
    "qa_and_ethics": "qa_ethics",
    "risk_register": "risk",
    "team_section": "team",
    "work_plan": "work_plan",
    # EOI generators
    "eoi_cover": "cover_letter",
    "eoi_firm": "org_profile",
    "eoi_experience": "experience",
    "eoi_experts": "team",
}


def _load_exemplars() -> dict[str, str]:
    """
    Parse intelligence/style_guides/voice_exemplars.md into
    {section_key: real prose from Cortech's submitted proposals}.

    Produced by extract_voice_exemplars.py. Cached per process — the file
    is static between runs and every section call reads it.
    """
    global _EXEMPLARS_CACHE
    if _EXEMPLARS_CACHE is not None:
        return _EXEMPLARS_CACHE

    path = _STYLE_GUIDE_DIR / "voice_exemplars.md"
    try:
        content = path.read_text()
    except FileNotFoundError:
        logger.warning(
            f"No house-voice exemplars at {path} — run "
            "`python extract_voice_exemplars.py` so drafts are grounded in "
            "the real proposals in data/proposals/. Proceeding without them."
        )
        _EXEMPLARS_CACHE = {}
        return _EXEMPLARS_CACHE

    exemplars: dict[str, str] = {}
    parts = _EXEMPLAR_MARKER_RE.split(content)
    # parts = [preamble, key, body, key, body, ...]
    for key, body in zip(parts[1::2], parts[2::2]):
        body = body.strip()
        if body:
            exemplars[key] = body
    _EXEMPLARS_CACHE = exemplars
    logger.info(f"Loaded house-voice exemplars for {len(exemplars)} sections")
    return exemplars


def _exemplar_block(section_name: str) -> str:
    """
    The matching real-proposal excerpts for this section, framed so the
    model copies the voice and level of specificity but none of the facts.
    """
    key = _EXEMPLAR_KEY_FOR.get(section_name.replace("_repair", ""))
    if not key:
        return ""
    excerpt = _load_exemplars().get(key)
    if not excerpt:
        return ""
    return (
        "\n\nHOUSE VOICE — REAL EXCERPTS FROM PROPOSALS CORTECH HAS SUBMITTED "
        "AND WON WORK WITH:\n"
        f"{excerpt}\n\n"
        "Match the register, sentence rhythm, paragraph length, and — above all "
        "— the density of concrete detail in those excerpts. Do NOT reuse their "
        "facts, client names, project names, countries, or figures: they are "
        "from other assignments. Write about THIS assignment in THAT voice."
    )


# The single hardest rule in the whole writer. A technical proposal that
# discloses price is disqualified under most procurement rules, and Cortech
# submits its financial proposal as a separate envelope. Enforced three
# ways: stated here, kept out of the context the model can see, and
# stripped from every generated section by utils/money_scrub.py.
NO_MONETARY_RULE = """
ABSOLUTE RULE — NO MONEY IN THIS DOCUMENT:
- State NO monetary amount anywhere: no budget, total, ceiling, unit rate,
  daily fee, per-diem, contract value, past-assignment value, cost estimate,
  contingency amount, or currency figure. Not in prose, not in a table cell,
  not in a bracket, not "approximately", not as a range.
- This applies to figures in ANY currency and to amounts written in words.
- Past assignments are evidenced by client, year, geography, scale of
  fieldwork, and outcome — never by contract value. If an experience table
  would normally carry a value column, use duration or scope instead.
- Never state that the proposal is within budget, competitively priced, or
  good value for money. Cost-competitiveness is asserted in the financial
  proposal, which is a separate submission and not your job here.
- Where cost is unavoidable as a topic, refer to "the financial proposal"
  with no figure attached.
- Efficiency and value are demonstrated through method, sequencing, team
  seniority mix, and reuse of existing data — never through price.
"""

COMPLETENESS_RULES = """
MANDATORY WRITING STANDARDS:
- This is a client-facing, submission-ready document. Write complete sections only.
- Never stop mid-sentence, mid-bullet, mid-table row, or mid-heading.
- If you open a heading, write the full body under it before moving on.
- You have the full tender documents in your context. Ground every claim in
  what they actually say, using the client's own terminology, named locations,
  named target groups, and named deliverables. A section that could be sent to
  a different client unchanged has failed.
- Align every claim with the ToR EVALUATION CRITERIA. Weighted criteria get
  proportionally more depth.
- Be specific: geographies, sample sizes, tools, dates, named past assignments,
  named experts.
- Do not invent evaluation criteria or requirements. Use what the tender states.
- Where the tender prescribes a structure, heading, or page limit, follow it
  exactly — the prescribed structure always beats Cortech's house structure.
- Professional development-consulting tone. No hollow phrases ("we are excited",
  "we believe", "our team is passionate", "this proposal aims", "we are pleased").
"""

WINNING_STANDARD = """
WHAT MAKES THIS PROPOSAL WIN:
This is scored against competitors who will also write a competent generic
response. The margin comes from four things, in this order:
1. Demonstrated comprehension — the client recognises their own assignment,
   context, constraints, and vocabulary in your text, including details that
   only appear in an annex.
2. Method that is decidable — a reader can tell exactly what will be done,
   by whom, in what sequence, with which tool, producing which output, and
   how quality is assured at each step. No method described only in the
   abstract.
3. Evidence over assertion — every capability claim is attached to a named
   past assignment, a named expert, a named tool, or a stated procedure.
4. Explicit scoring alignment — each scored criterion is addressed head-on,
   in proportion to its weight.
"""

QUALITY_SUFFIX = (
    "\n\nFinish the entire section. Never end mid-sentence, mid-list, or mid-table. "
    "Explicitly satisfy the ToR evaluation criteria from your system context with "
    "specific, evidence-based claims — not generic consulting language. "
    "Ground the content in the tender documents in your system context and use the "
    "client's own terms. State no monetary amount of any kind."
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


# Keys whose values are monetary and must never enter the writing context.
# Matched on whole snake_case tokens, not substrings — a substring match on
# "fee" would also drop a "feedback" key.
_MONETARY_KEY_TOKENS = {
    "budget", "budgets", "cost", "costs", "costing", "price", "pricing",
    "fee", "fees", "rate", "rates", "amount", "amounts", "currency",
    "usd", "kes", "eur", "gbp", "value", "values", "expenditure",
    "remuneration", "salary", "salaries", "perdiem", "honorarium", "ceiling",
}
_KEY_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _is_monetary_key(key: str) -> bool:
    tokens = set(_KEY_TOKEN_RE.split(str(key).lower()))
    return bool(tokens & _MONETARY_KEY_TOKENS)


def _strip_monetary_keys(node):
    """
    Recursively drop monetary keys and scrub monetary figures out of the
    strings that remain. The analysis JSON is dumped verbatim into the
    writing context, and it carries estimated_budget_usd straight from the
    ToR — leaving it there is how a ceiling ends up quoted back at the
    client in an executive summary.
    """
    if isinstance(node, dict):
        return {
            k: _strip_monetary_keys(v)
            for k, v in node.items()
            if not _is_monetary_key(k)
        }
    if isinstance(node, list):
        return [_strip_monetary_keys(v) for v in node]
    if isinstance(node, str):
        return strip_monetary_amounts(node)[0]
    return node


def _profile_for_writing() -> str:
    """
    CORTECH_PROFILE with its money lines removed. The profile states a
    typical budget range, which is useful to the analyzer for fit scoring
    and inadmissible in a drafted proposal.
    """
    return strip_monetary_amounts(CORTECH_PROFILE)[0]


def _build_guidance_block(
    analysis: dict,
    extra_context: str = "",
    submission_type: str = "FULL_PROPOSAL",
    tor_brief: str = "",
) -> str:
    """Build once per proposal — identical bytes across all section calls for cache hits."""
    style_guide = _load_style_guide(submission_type)
    writing_analysis = _strip_monetary_keys(copy.deepcopy(analysis))
    parts = [
        COMPLETENESS_RULES,
        NO_MONETARY_RULE,
        WINNING_STANDARD,
        _eval_criteria_block(analysis),
    ]
    if tor_brief.strip():
        parts.append(tor_brief.strip())
    parts.extend([
        f"CORTECH PROFILE:\n{_profile_for_writing()}",
        "OPPORTUNITY ANALYSIS (a structured reading of the tender documents — "
        "where it disagrees with the documents themselves, the documents win):\n"
        f"{json.dumps(writing_analysis, indent=2, sort_keys=True)}",
        _build_past_work_context(),
    ])
    if style_guide:
        parts.append(style_guide)
    if extra_context.strip():
        parts.append(extra_context.strip())
    return "\n\n".join(parts)


def build_system_blocks(
    analysis: dict,
    tor_text: str = "",
    extra_context: str = "",
    submission_type: str = "FULL_PROPOSAL",
) -> list[dict]:
    """
    The cached system prompt every section-writing call shares.

    Block 1 is the verbatim tender pack, block 2 the guidance derived from
    it. Two separate cache breakpoints, in that order, because the ToR
    comprehension pass sends block 1 alone and therefore warms it: the ten
    section calls that follow read the pack from cache rather than
    re-uploading it ten times.

    Returns a single guidance block when no tender text is available.
    """
    doc_block = tender_documents_block(tor_text)
    tor_brief = build_tor_brief(tor_text, analysis, doc_block=doc_block)
    guidance = _build_guidance_block(
        analysis, extra_context, submission_type, tor_brief=tor_brief
    )

    blocks = []
    if doc_block:
        blocks.append({
            "type": "text",
            "text": doc_block,
            "cache_control": {"type": "ephemeral"},
        })
    blocks.append({
        "type": "text",
        "text": guidance,
        "cache_control": {"type": "ephemeral"},
    })
    return blocks


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
    from database.airtable_client import _circuit_open, _note_failure
    if _circuit_open():
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
        from database.airtable_client import _note_failure
        _note_failure(e)
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


_MONEY_REWRITE_INSTRUCTION = """The section below states monetary amounts. A
technical proposal that discloses price is disqualified under the procurement
rules Cortech bids into, and cost is submitted separately in the financial
proposal.

Return the same section with every monetary amount removed — figures, ranges,
contract values, rates, per-diems, contingency amounts, totals, and amounts
written in words, in any currency. Do not replace them with placeholders.

Rewrite the surrounding sentence so it still reads naturally and still makes a
substantive point: past assignments are evidenced by client, year, geography,
and scale of fieldwork instead of value; efficiency is evidenced by method,
sequencing, and team composition instead of price. Drop any sentence whose only
content was the amount. Keep every table's column count and row count intact,
replacing a value column with duration or scope.

Change nothing else: keep all headings, all other facts, the same structure,
and the same length. Return only the corrected section text."""


def _enforce_no_monetary(section_name: str, text: str) -> str:
    """
    Last line of defence on the no-money rule.

    The prompt-level rule holds most of the time, but "most of the time" is
    not good enough for a rule that can void a bid, so anything that slips
    through gets one cheap rewrite pass and then, if it survives that, a
    deterministic strip. utils/money_scrub.py drops the offending sentence
    rather than the figure alone, so no broken sentence can reach a client.
    """
    if not text or not contains_monetary_amount(text):
        return text

    found = find_monetary_amounts(text)
    logger.warning(
        f"  [{section_name}] contains {len(found)} monetary amount(s) "
        f"({', '.join(found[:5])}{'…' if len(found) > 5 else ''}) — rewriting"
    )
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": f"{_MONEY_REWRITE_INSTRUCTION}\n\nSECTION:\n{text}",
            }],
        )
        rewritten = get_text(response).strip()
        # A truncated or empty rewrite is worse than the original — only take
        # it if it is complete, substantial, and actually clean.
        if (
            rewritten
            and len(rewritten) > len(text) * 0.5
            and not contains_monetary_amount(rewritten)
            and not _looks_truncated(rewritten)
        ):
            logger.success(f"  [{section_name}] monetary amounts rewritten out")
            return rewritten
    except Exception as e:
        logger.warning(f"  [{section_name}] money rewrite call failed: {e}")

    cleaned, removed = strip_monetary_amounts(text)
    logger.warning(
        f"  [{section_name}] stripped {len(removed)} monetary amount(s) "
        "deterministically — review this section's wording before submission"
    )
    return _trim_to_clean_end(cleaned)


def _generate_section(
    section_name: str,
    model: str,
    max_tokens: int,
    system_blocks: list[dict],
    user_prompt: str,
) -> str:
    """
    Generate a section and continue if the model hits the output cap
    or otherwise stops mid-sentence. Unfinished sentences in emailed
    drafts were caused by max_tokens cutoffs with no continuation.

    `system_blocks` is the cached tender-pack + guidance prompt from
    build_system_blocks(). The house-voice exemplar for this section is
    appended to the user prompt rather than the system blocks, so the
    cached prefix stays byte-identical across all sections.
    """
    logger.info(f"  Writing {section_name}...")
    system = system_blocks
    user_prompt = f"{user_prompt}{_exemplar_block(section_name)}"
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
            return _enforce_no_monetary(section_name, assembled.strip())
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
    return _enforce_no_monetary(section_name, cleaned)


def _run_parallel_sections(jobs: dict) -> dict:
    """Run independent section generators concurrently so wall-clock time
    is roughly one long section, not ten sequential ones."""
    sections = {}
    workers = min(4, max(1, len(jobs)))
    logger.info(f"  Writing {len(jobs)} sections in parallel (workers={workers})")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn): key for key, fn in jobs.items()}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                sections[key] = fut.result()
                logger.success(f"  [{key}] complete")
            except Exception as e:
                logger.error(f"  [{key}] failed: {e}")
                sections[key] = ""
    return sections


def _final_money_audit(sections: dict) -> None:
    """
    Log-only sweep after every section is written. _generate_section already
    enforces the rule per section; this catches anything assembled outside
    that path and makes a violation visible in the run log instead of only
    in the emailed .docx.
    """
    offenders = {
        key: find_monetary_amounts(value)
        for key, value in sections.items()
        if isinstance(value, str) and contains_monetary_amount(value)
    }
    if not offenders:
        logger.success("  Money audit clean — no monetary amounts in the draft")
        return
    for key, amounts in offenders.items():
        logger.error(
            f"  MONEY AUDIT FAILED [{key}]: {', '.join(amounts[:8])} — "
            "remove before submission"
        )


def _repair_weakest_section(
    sections: dict,
    analysis: dict,
    system_blocks: list[dict],
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

Rewrite the complete section from start to finish, working from the tender documents in your system context. Keep accurate facts (names, dates, sample sizes, past assignments, named experts). Strengthen alignment with the evaluation criteria and with the client's own terminology. Finish every sentence and every subsection. Do not leave headings without body text.{QUALITY_SUFFIX}"""
    sections[target] = _generate_section(
        f"{target}_repair",
        CLAUDE_MODEL_PROPOSAL,
        CLAUDE_MAX_TOKENS,
        system_blocks,
        user_prompt,
    )
    return sections


def generate_eoi(
    analysis: dict,
    matched_team_result: dict,
    opportunity_id: str = None,
    tor_text: str = "",
) -> dict:
    """
    Full Expression of Interest aligned to ToR evaluation/shortlisting
    criteria. House style: letter of interest, firm presentation,
    relevant experience table, resources in staff — complete prose,
    not a profile dump.

    `tor_text` is the tender pack as extracted by processors.downloader.
    It is read before any section is written — see tender_reader.py.
    """
    opportunity = analysis.get("opportunity", {})
    title = opportunity.get("title", "Unknown Assignment")
    client_name = opportunity.get("client", "Client")
    donor = opportunity.get("donor", "")

    extra_context = (
        get_relevant_lessons(client_name, donor)
        + get_donor_intelligence(donor, client_name)
    )
    logger.info(f"Generating EOI for: {title[:60]}")
    system_blocks = build_system_blocks(
        analysis, tor_text, extra_context, submission_type="EOI"
    )

    team_summary = json.dumps({
        role: {
            "name": m.get("consultant_name"),
            "score": m.get("similarity_score"),
            "justification": m.get("justification", ""),
        }
        for role, m in matched_team_result.get("matched_team", {}).items()
        if m.get("consultant_name") != "EXTERNAL RECRUITMENT NEEDED"
    }, indent=2)

    jobs = {
        "cover_letter": lambda: _generate_section(
            "eoi_cover",
            CLAUDE_MODEL_PROPOSAL,
            4096,
            system_blocks,
            f"""Write a complete Expression of Interest cover letter for Cortech Consulting Group.

ASSIGNMENT: {title}
CLIENT: {client_name}

This is an EOI / shortlisting submission, not a full technical proposal.
Do not write a methodology. Do write a finished letter of 4-5 paragraphs:
- Addressed to the procurement committee named in the tender documents
- A statement of interest that shows you have read the documents: name the
  assignment's purpose, geography, and target groups in the client's own terms
- Two or three specific past assignments that match this ToR, evidenced by
  client, year, geography, and scale — never by contract value
- Confirmation that Cortech can field a qualified team and will submit a
  full technical and financial proposal if shortlisted
- Sign off: Daud Hussein Ibrahim, Director, Cortech Consulting Group

{_eval_criteria_block(analysis)}{QUALITY_SUFFIX}""",
        ),
        "firm_profile": lambda: _generate_section(
            "eoi_firm",
            CLAUDE_MODEL_PROPOSAL,
            CLAUDE_MAX_TOKENS,
            system_blocks,
            f"""Write the 'Presentation of Cortech Consulting Group' section for this EOI.

ASSIGNMENT: {title}
CLIENT: {client_name}

400-600 words covering history, registrations, geographic presence,
thematic competence, and why the firm is qualified for THIS assignment.
Map credentials to the shortlisting criteria the tender documents state.
Use only facts from the CORTECH PROFILE. Complete every paragraph.
State no monetary amounts, including any typical budget range.

{_eval_criteria_block(analysis)}{QUALITY_SUFFIX}""",
        ),
        "relevant_experience": lambda: _generate_section(
            "eoi_experience",
            CLAUDE_MODEL_PROPOSAL,
            CLAUDE_MAX_TOKENS,
            system_blocks,
            f"""Write the 'Relevant Experience of Completed Assignments' section for this EOI.

ASSIGNMENT: {title}

Format as a markdown table with exactly these columns:
Project | Client | Country | Year | Scope delivered | Relevance to this assignment
There is deliberately no contract-value column — state no amounts anywhere.
'Scope delivered' carries the scale evidence instead: sample sizes, districts
covered, instruments used, number of KIIs/FGDs, or report outputs.
Use the past assignments in your system context. For each row, one sentence
connecting that assignment to THIS tender (geography, theme, method, or client type).
Follow the table with 2-3 paragraphs of narrative that explicitly address
the experience-related shortlisting criteria in the tender documents.

{_eval_criteria_block(analysis)}{QUALITY_SUFFIX}""",
        ),
        "key_experts": lambda: _generate_section(
            "eoi_experts",
            CLAUDE_MODEL_PROPOSAL,
            4096,
            system_blocks,
            f"""Write the 'Resources in Staff' section for this EOI.

ASSIGNMENT: {title}
PROPOSED EXPERTS:
{team_summary}

For each named expert: role on this assignment, 4-6 sentence bio, and
why they satisfy the personnel/shortlisting criteria stated in the tender
documents — quote the requirement they meet. If a role is unfilled, state
the recruitment profile in 3-4 complete sentences.
Close with a short availability and commitment paragraph.
State no fees, rates, or costs for any expert.

{_eval_criteria_block(analysis)}{QUALITY_SUFFIX}""",
        ),
    }
    sections = _run_parallel_sections(jobs)
    sections["submission_type"] = "EOI"
    sections["quality_score"] = generate_quality_self_score(sections, analysis)
    sections = _repair_weakest_section(sections, analysis, system_blocks)
    _final_money_audit(sections)

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
    opportunity_id: str = None,
    tor_text: str = "",
) -> dict:
    """
    Generate a proposal draft routed by bid_recommendation:
      BID   → all 10 sections on the proposal model, complete and
              aligned to ToR evaluation criteria
      WATCH → cover letter + executive summary only (analysis model)
    NO-BID callers should not invoke this function.

    `tor_text` is the tender pack as extracted by processors.downloader.
    It is read and turned into a compliance brief before a single section
    is drafted — see tender_reader.py.

    `budget` is accepted for interface stability and is deliberately NOT
    used in any drafted text: the technical proposal must state no
    monetary amount. The costed budget still reaches the team through the
    internal review email in reporting/email_report.py.
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
    logger.info(f"Generating proposal ({recommendation}) for: {title[:60]}")
    system_blocks = build_system_blocks(
        analysis, tor_text, extra_context, submission_type="FULL_PROPOSAL"
    )

    if recommendation == "WATCH":
        sections = {
            "cover_letter": generate_cover_letter(
                title, client_name, deadline, analysis, system_blocks,
                model=CLAUDE_MODEL,
            ),
            "executive_summary": generate_executive_summary(
                analysis, matched_team_result, system_blocks,
                model=CLAUDE_MODEL,
            ),
            "lightweight": True,
            "lightweight_reason": "WATCH recommendation — quick flag, not a full draft",
        }
        sections["quality_score"] = generate_quality_self_score(sections, analysis)
        sections = _repair_weakest_section(sections, analysis, system_blocks)
        _final_money_audit(sections)
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

    # BID — full 10-section draft, written concurrently
    sections = _run_parallel_sections({
        "cover_letter": lambda: generate_cover_letter(
            title, client_name, deadline, analysis, system_blocks,
        ),
        "executive_summary": lambda: generate_executive_summary(
            analysis, matched_team_result, system_blocks,
        ),
        "org_profile_and_track_record": lambda: generate_org_profile_and_track_record(
            analysis, system_blocks,
        ),
        "introduction_and_framework": lambda: generate_introduction_and_framework(
            analysis, system_blocks,
        ),
        "methodology": lambda: generate_methodology(analysis, system_blocks),
        "analysis_plan": lambda: generate_analysis_plan(analysis, system_blocks),
        "qa_and_ethics": lambda: generate_qa_and_ethics(analysis, system_blocks),
        "risk_register": lambda: generate_risk_register(analysis, system_blocks),
        "team_section": lambda: generate_team_section(
            matched_team_result, title, system_blocks, analysis,
        ),
        "work_plan": lambda: generate_work_plan(analysis, system_blocks),
    })
    sections["quality_score"] = generate_quality_self_score(sections, analysis)
    sections = _repair_weakest_section(sections, analysis, system_blocks)
    _final_money_audit(sections)

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
    system_blocks: list[dict],
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

Work from the tender documents in your system context, plus the CORTECH
PROFILE and the reading brief.

REQUIREMENTS:
- Address it to the procurement committee / contact named in the tender
  documents, with the tender reference number if one is stated
- 4-5 professional paragraphs
- Opening: state the assignment as the client framed it — purpose, geography,
  target groups — using their own terminology, so it is immediately clear the
  documents have been read
- Middle: Cortech's most relevant past assignments and the proposed team,
  each tied to a requirement the documents actually state
- Closing: confirm compliance with the stated submission requirements and
  availability for clarification
- Sign off from: Daud Hussein Ibrahim, Director, Cortech Consulting Group
- Professional development consulting sector tone
- Do NOT use hollow phrases like "we are excited" or "we are pleased"
- Be specific about capabilities, not generic
- State no monetary amounts — no fee, no budget, no past contract values
- Write a complete letter — every sentence finished
- 400-500 words{QUALITY_SUFFIX}"""

    return _generate_section("cover_letter", model, 4096, system_blocks, user_prompt)


def generate_executive_summary(
    analysis: dict,
    matched_team_result: dict,
    system_blocks: list[dict],
    model: str = CLAUDE_MODEL_PROPOSAL,
) -> str:
    """
    Generate executive summary.

    Takes no budget argument: the fourth paragraph used to be a budget and
    value-for-money statement, which is exactly the disclosure a technical
    proposal must not make. Delivery assurance replaces it.
    """
    opportunity = analysis.get("opportunity", {})

    user_prompt = f"""Write a comprehensive executive summary for a technical proposal.

ASSIGNMENT: {opportunity.get('title', '')}
CLIENT: {opportunity.get('client', '')}
DURATION: {opportunity.get('project_duration', '')}
LOCATION: {', '.join(opportunity.get('project_location', []))}

TEAM COVERAGE: {matched_team_result.get('coverage_percent', 0)}% internal match

Work from the tender documents and the reading brief in your system context,
plus the CORTECH PROFILE and past assignments.

Write a 4-paragraph executive summary:
1. The context and the specific problem the assignment addresses, named as the
   client names it
2. Cortech's proposed approach and what makes it distinctive for this
   assignment — the design choice, not a list of methods
3. Team composition highlights and coverage against the personnel requirements
   the documents state
4. Delivery assurance: how the work will be sequenced against the stated
   deadline, how quality is controlled, and what the client receives at each
   milestone. State no amounts and make no claim about price, budget
   compliance, or value for money — costs are covered in the separate
   financial proposal.

Professional, evidence-based, specific. 450-650 words. Complete all four
paragraphs — do not stop mid-paragraph.
Reference 2-3 specific past assignments as credibility evidence, identified by
client, year, and geography rather than contract value.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section("executive_summary", model, 4096, system_blocks, user_prompt)


def generate_methodology(analysis: dict, system_blocks: list[dict]) -> str:
    """Generate the detailed methodology section — longest and most important."""
    user_prompt = f"""Write a detailed methodology section for a technical proposal.

ASSIGNMENT: {analysis.get('opportunity', {}).get('title', '')}

Work primarily from the tender documents in your system context: the scope of
work, every stated deliverable, any methodology the documents require or
prohibit, and the scoring weight given to technical approach. The reading brief
lists what the scored criteria want to see. Use the client's own names for
phases, tools, target groups, and locations.

CORTECH'S STANDARD TOOLS:
- KoboToolbox for digital data collection
- SPSS for quantitative analysis
- NVivo for qualitative analysis
- Python/QGIS for spatial analysis
- OECD DAC evaluation criteria (Relevance, Effectiveness, Efficiency, Impact, Sustainability)
- Mixed-methods approach combining quantitative surveys with qualitative FGDs and KIIs
- Participatory approaches ensuring community voice
- Triangulation across multiple data sources

STRUCTURE THE METHODOLOGY AS (unless the tender documents prescribe a
different structure, in which case follow theirs exactly):
1. Overall Approach (2-3 paragraphs on the design rationale for THIS assignment)
2. Phase-by-phase breakdown (Inception → Field Work → Analysis → Reporting)
3. Specific method for each deliverable the documents list, deliverable by
   deliverable, naming the instrument, the respondents, and the output
4. Data quality assurance
5. Ethical considerations integration

Development sector professional language. Evidence-based. Specific tool names.
Reference KoboToolbox, SPSS, NVivo explicitly. 900-1400 words.
Complete ALL five numbered parts — do not stop inside part 3, 4, or 5.
Every method must be decidable: who does it, when, with which instrument,
producing which output. No method described only in the abstract.
Map the method explicitly to the scored criteria in the tender documents
(especially any methodology / technical-approach weighting).
State no costs, day rates, or budget figures — the financial proposal covers those.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "methodology", CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, system_blocks, user_prompt,
    )


def generate_team_section(
    matched_team_result: dict,
    title: str,
    system_blocks: list[dict],
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

Use the CORTECH PROFILE in your system context for team credentials, and the
personnel requirements stated in the tender documents as the bar to clear.
{_eval_criteria_block(analysis)}

Write:
1. Opening paragraph on overall team strength against the personnel criteria the
   tender documents actually state
2. Brief profile for each team member (4-6 sentences each — qualifications,
   relevant assignments, role on this job), each one closing on the specific
   stated requirement that person satisfies
3. Note on any external specialist to be recruited (if gaps exist)
4. Statement on team availability and commitment against the stated timeline

Professional, confident tone. 400-600 words. Complete every bio.
State no fees, day rates, or personnel costs.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "team_section", CLAUDE_MODEL_PROPOSAL, 4096, system_blocks, user_prompt,
    )


def generate_work_plan(analysis: dict, system_blocks: list[dict]) -> str:
    """Generate workplan/Gantt description."""
    duration = analysis.get("opportunity", {}).get("project_duration", "3 months")

    user_prompt = f"""Create a work plan narrative and Gantt chart description for a proposal.

PROJECT DURATION: {duration}

Use the deliverables, milestones, approval gates, and submission dates stated in
the tender documents in your system context. Where the documents fix a date or a
sequence, the work plan must match it.

Create:
1. Phase breakdown with timing (e.g., Phase 1: Inception - Week 1-2)
2. For each phase: key activities and outputs
3. Milestone dates, aligned to any deadline the documents state
4. Note on parallel vs sequential activities
5. A text-based Gantt table showing Month/Week vs Activities

Format the Gantt as a complete markdown table with every phase/activity row filled.
Include no payment amounts or cost columns — milestones are described by
deliverable and date only.
Professional. 500-700 words. Do not stop after a heading such as "GANTT CHART".
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "work_plan", CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, system_blocks, user_prompt,
    )


def generate_risk_register(analysis: dict, system_blocks: list[dict]) -> str:
    """Generate risk register."""
    location = analysis.get("opportunity", {}).get("project_location", ["East Africa"])
    thematic_areas = analysis.get("requirements", {}).get("thematic_areas", [])

    user_prompt = f"""Create a risk management section for a technical proposal.

PROJECT LOCATION: {location}
THEMATIC AREAS: {thematic_areas}

Generate 6-8 risks in this format for each:
| Risk Category | Description | Likelihood | Impact | Mitigation |

Include risks related to:
- Field access and security in the specific locations the tender names
- Data quality and collection
- Government and stakeholder engagement
- Community participation
- Timeline, sequencing, and approval gates
- Team availability

Prioritise the risks that are real for THIS assignment's context, geography, and
respondent groups as described in the tender documents — a generic register
scores nothing.
Format as a complete markdown table (every row finished) followed by 2 paragraphs
on overall risk management approach.
Mitigations must be operational, not financial: no contingency amounts, no cost
buffers, no budget figures anywhere in the table or the narrative.
Professional development sector language. 400-550 words.
Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "risk_register", CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, system_blocks, user_prompt,
    )


def generate_org_profile_and_track_record(
    analysis: dict, system_blocks: list[dict]
) -> str:
    """Generate 'Organisational Profile' + 'Related Previous Assignments'."""
    user_prompt = f"""Write two sections for a Cortech Consulting Group technical proposal.

Use the CORTECH PROFILE and past assignments from your system context, and the
capacity requirements stated in the tender documents.

SECTION 1 — ORGANISATIONAL PROFILE (300-400 words):
Cortech's history, registrations, certifications, geographic presence,
core thematic areas, and what distinguishes it from competitors.
Evidence-based, no generic claims. Include no turnover, budget range, or
other monetary figure.

SECTION 2 — RELATED PREVIOUS ASSIGNMENTS (table):
Format as a table with exactly these columns:
Project | Client | Country | Year | Scope delivered | Relevance to this assignment
There is deliberately no contract-value column — state no amounts anywhere.
'Scope delivered' carries the scale evidence: sample sizes, districts covered,
instruments used, numbers of KIIs/FGDs, or report outputs.
Use the past assignments listed in context. For each, add one sentence
connecting it directly to a requirement THIS tender actually states.

ASSIGNMENT CONTEXT (for relevance-mapping):
{analysis.get('opportunity', {}).get('title', '')}
{json.dumps(analysis.get('requirements', {}).get('thematic_areas', []), indent=2)}

Return both sections with clear headers. Complete every table row.
Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "org_profile_and_track_record",
        CLAUDE_MODEL_PROPOSAL,
        CLAUDE_MAX_TOKENS,
        system_blocks,
        user_prompt,
    )


def generate_introduction_and_framework(
    analysis: dict, system_blocks: list[dict]
) -> str:
    """
    Generate 'Introduction and Background' (6 sub-sections per
    PROPOSAL_STRUCTURE) plus 'Conceptual Framework'.
    """
    opportunity = analysis.get("opportunity", {})

    user_prompt = f"""Write two sections for a Cortech Consulting Group technical proposal.

ASSIGNMENT: {opportunity.get('title', '')}
CLIENT: {opportunity.get('client', '')}

This is the section where the client checks whether you actually read their
documents, and it is usually the cheapest place to lose the bid. Work from the
tender documents in your system context, not from the summary: use their
background, their stated problem, their objectives, their questions, and their
deliverable names, in their words.

SECTION 1 — INTRODUCTION AND BACKGROUND, with these exact sub-headers:
5.1 Context and Strategic Importance
5.2 Purpose and Objectives
5.3 Our Interpretation of the Assignment
5.4 Key Evaluation Questions
5.5 Deliverables
5.6 Understanding of Success
Each sub-section 2-4 sentences. Specific to this assignment, not generic.
Under 5.3, state at least one implication or constraint the documents imply but
do not spell out — that is what distinguishes comprehension from paraphrase.
Under 5.4 and 5.5, reproduce the client's own questions and deliverable names.

SECTION 2 — CONCEPTUAL FRAMEWORK (300-400 words):
The theoretical/analytical lens Cortech will apply (e.g. OECD DAC criteria,
theory of change, results framework) and why it fits this assignment
specifically. If the documents name a framework, use theirs.
Complete this section in full — do not stop mid-sentence.

Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "introduction_and_framework",
        CLAUDE_MODEL_PROPOSAL,
        CLAUDE_MAX_TOKENS,
        system_blocks,
        user_prompt,
    )


def generate_analysis_plan(analysis: dict, system_blocks: list[dict]) -> str:
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

Use the methodology requirements, deliverables, target populations, and scored
criteria as stated in the tender documents in your system context. Where the
documents specify a sample, a precision level, a disaggregation, or a reporting
breakdown, match it exactly.

{sampling_instruction}

SECTION 2 — DATA ANALYSIS PLAN (250-350 words):
How quantitative data will be analyzed (SPSS, descriptive/inferential
approach) and qualitative data (NVivo, thematic analysis), and how the
two will be triangulated.

Professional development consulting tone. Complete both sections — do not stop
inside the triangulation paragraph. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "analysis_plan", CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, system_blocks, user_prompt,
    )


def generate_qa_and_ethics(analysis: dict, system_blocks: list[dict]) -> str:
    """Generate 'Quality Assurance Framework' + 'Ethical Considerations and Safeguarding'."""
    location = analysis.get("opportunity", {}).get("project_location", ["East Africa"])

    user_prompt = f"""Write two sections for a Cortech Consulting Group technical proposal.

PROJECT LOCATION: {location}

Use the CORTECH PROFILE in your system context for certifications and policies,
and any QA, ethics, safeguarding, data-protection, or ethical-approval
requirement the tender documents state — quote their requirement and say how it
is met.

SECTION 1 — QUALITY ASSURANCE FRAMEWORK (250-350 words):
Data quality checks, peer review process, deliverable review stages,
client feedback loops — each tied to a named phase of the work plan.

SECTION 2 — ETHICAL CONSIDERATIONS AND SAFEGUARDING (250-350 words):
Reference Cortech's actual certifications: ISO certification, child
safeguarding policy, PSEA policy compliance. Cover informed consent,
data protection, protection of the specific vulnerable groups this
assignment involves, ethical clearance where the documents require it,
and do-no-harm principles. Complete both sections in full.

Professional development consulting tone. Do NOT use hollow phrases like "we are excited," "we believe," "our team is passionate," or "this proposal aims."{QUALITY_SUFFIX}"""

    return _generate_section(
        "qa_and_ethics", CLAUDE_MODEL_PROPOSAL, CLAUDE_MAX_TOKENS, system_blocks, user_prompt,
    )
