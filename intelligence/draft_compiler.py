# intelligence/draft_compiler.py
"""Turn a pile of chapters into one document.

Each extracted fact has one owner. The cover letter is first and the
executive summary is second. A later chapter loses a passage it repeats.
No model call lives here.
"""
from __future__ import annotations

import re

_LETTER_HEADING = re.compile(
    r"cover letter|letter of interest|letter of transmittal|letter of expression",
    re.I,
)
_EXEC_HEADING = re.compile(r"executive summary", re.I)
_LETTER_OPEN = re.compile(
    r"^\s*dear\s+(sir|madam|procurement|members|colleagues|mr|ms|mrs)\b",
    re.I,
)
_SIGNOFF = re.compile(r"\b(yours sincerely|yours faithfully|kind regards)\b", re.I)
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.M)
_MONEY = re.compile(
    r"(?:[$€£]\s?\d|\b\d[\d,]*\s*(?:usd|eur|gbp)\b|\b(?:daily rate|day rate|professional fee)s?\b)",
    re.I,
)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "and", "the", "of", "for", "with", "this", "that", "from", "into",
    "your", "our", "proposal", "section", "chapter",
}

# Body chapters a "Technical proposal" envelope would otherwise write again.
_BODY_CHAPTERS = (
    "introduction_and_framework",
    "understanding",
    "methodology",
    "approach_summary",
    "analysis_plan",
    "qa_and_ethics",
    "risk_register",
    "work_plan",
    "team_section",
    "key_experts",
    "org_profile_and_track_record",
    "relevant_experience",
    "firm_profile",
    "deliverables",
)

_PREFERRED_ORDER = (
    "cover_letter",
    "executive_summary",
    "firm_profile",
    "understanding",
    "approach_summary",
    "relevant_experience",
    "key_experts",
    "eligibility",
    "compliance_matrix",
    "org_profile_and_track_record",
    "introduction_and_framework",
    "deliverables",
    "methodology",
    "analysis_plan",
    "qa_and_ethics",
    "risk_register",
    "team_section",
    "work_plan",
)


def _tokens(text: str) -> set[str]:
    return {
        word for word in _WORD.findall((text or "").lower())
        if len(word) > 3 and word not in _STOP
    }


def _norm_para(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def chapters_owned_by_siblings(routes, headings) -> set[str]:
    """Body chapters a sibling heading already owns, so the envelope must not write them."""
    owned: set[str] = set()
    for route in routes or []:
        owned |= _chapters_for(str(route or ""), "")
    for heading in headings or []:
        owned |= _chapters_for("", str(heading or ""))
    return owned


def _chapters_for(route: str, heading: str) -> set[str]:
    route = (route or "").strip()
    heading = (heading or "").lower()
    found: set[str] = set()
    if route in _BODY_CHAPTERS:
        found.add(route)
    if route in {"team_section", "key_experts"}:
        found.update({"team_section", "key_experts"})
    if route in {"methodology", "approach_summary"}:
        found.update({"methodology", "approach_summary"})
    if route in {"understanding", "introduction_and_framework"}:
        found.update({"understanding", "introduction_and_framework"})
    if route in {"org_profile_and_track_record", "relevant_experience", "firm_profile"}:
        found.update({"org_profile_and_track_record", "relevant_experience", "firm_profile"})
    if re.search(r"methodolog|technical approach|proposed approach|research design|evaluation design|study design", heading):
        found.update({"methodology", "approach_summary"})
    if re.search(r"understanding of the assignment|understanding of the tor|interpretation of the assignment|conceptual framework", heading):
        found.update({"understanding", "introduction_and_framework"})
    if re.search(r"work[ -]?plan|gantt|timeline|schedule of activities", heading):
        found.add("work_plan")
    if re.search(r"sampling|data analysis|analysis plan", heading):
        found.add("analysis_plan")
    if re.search(r"quality assurance|ethic|safeguard|psea", heading):
        found.add("qa_and_ethics")
    if re.search(r"\brisk\b", heading):
        found.add("risk_register")
    if re.search(r"personnel|key expert|proposed team|staffing|core expert|resources in staff|team composition", heading):
        found.update({"team_section", "key_experts"})
    if re.search(r"relevant experience|related experience|track record|previous assignment|similar assignment|firm profile|company profile|organisational profile|organizational profile", heading):
        found.update({"org_profile_and_track_record", "relevant_experience", "firm_profile"})
    if re.search(r"scope of (the )?(work|services|deliverables)|deliverables", heading) and "methodolog" not in heading:
        found.add("deliverables")
    return found


def _outline_sections(outline: dict | None) -> list[dict]:
    outline = outline if isinstance(outline, dict) else {}
    return [item for item in (outline.get("sections") or []) if isinstance(item, dict) and item.get("key")]


def _claim_lines(analysis: dict) -> list[tuple[str, str]]:
    """(topic, sentence) from structured extraction. Nothing is invented here."""
    analysis = analysis if isinstance(analysis, dict) else {}
    opportunity = analysis.get("opportunity") if isinstance(analysis.get("opportunity"), dict) else {}
    lines: list[tuple[str, str]] = []
    title = str(opportunity.get("title") or "").strip()
    client = str(opportunity.get("client") or opportunity.get("client_name") or "").strip()
    if title or client:
        lines.append(("cover", " ".join(part for part in (title, client) if part)))
    places = []
    for key in ("project_location", "lots_or_sites", "target_groups"):
        value = opportunity.get(key)
        if isinstance(value, list):
            places.extend(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, str) and value.strip():
            places.append(value.strip())
    if places:
        lines.append(("cover", "Geography and people: " + "; ".join(places[:8])))
    for item in _named(analysis.get("deliverables")):
        lines.append(("deliverable", item))
    for item in _named(analysis.get("evaluation_criteria"), name_key="criterion"):
        lines.append(("criterion", item))
    for item in analysis.get("team_requirements") or []:
        if isinstance(item, dict) and str(item.get("role") or "").strip():
            lines.append(("team", str(item.get("role")).strip()))
    kept = []
    for topic, text in lines:
        if not text or _MONEY.search(text):
            continue
        kept.append((topic, text[:400]))
    return kept[:24]


def _named(value, name_key: str = "name") -> list[str]:
    if not isinstance(value, list):
        return []
    lines = []
    for item in value:
        if isinstance(item, str) and item.strip():
            lines.append(item.strip())
        elif isinstance(item, dict):
            name = str(item.get(name_key) or item.get("name") or item.get("text") or "").strip()
            desc = str(item.get("description") or "").strip()
            text = ": ".join(part for part in (name, desc) if part)
            if text:
                lines.append(text)
    return lines


def _owner_for(topic: str, text: str, sections: list[dict], body_owners: dict[str, str]) -> str:
    if not sections and not body_owners:
        return ""
    best_key = ""
    best = 0
    for item in sections:
        route = str(item.get("route") or "")
        heading = str(item.get("heading") or "")
        score = len(_tokens(text) & _tokens(heading))
        if topic == "cover" and (route == "cover_letter" or _LETTER_HEADING.search(heading)):
            score += 5
        if topic == "criterion" and route in {"methodology", "approach_summary"}:
            score += 1
        if topic == "deliverable" and (route == "deliverables" or "deliverable" in heading.lower()):
            score += 4
        if topic == "team" and route in {"team_section", "key_experts"}:
            score += 4
        if topic == "cover" and route == "executive_summary":
            score += 1
        if score > best:
            best = score
            best_key = str(item.get("key"))
    if best_key and best > 0:
        return best_key
    preferred = {
        "cover": ("cover_letter",),
        "deliverable": ("deliverables", "methodology", "approach_summary"),
        "criterion": ("methodology", "approach_summary", "understanding", "introduction_and_framework"),
        "team": ("team_section", "key_experts"),
    }.get(topic, ("methodology", "approach_summary"))
    for route in preferred:
        if route in body_owners:
            return body_owners[route]
        for item in sections:
            if item.get("route") == route or item.get("key") == route:
                return str(item["key"])
    if sections:
        return str(sections[0]["key"])
    return ""


_HOUSE_TECHNICAL = (
    ("cover_letter", "Cover letter", "cover_letter"),
    ("executive_summary", "Executive summary", "executive_summary"),
    ("org_profile_and_track_record", "Organisational profile", "org_profile_and_track_record"),
    ("introduction_and_framework", "Understanding of the assignment", "introduction_and_framework"),
    ("methodology", "Methodology", "methodology"),
    ("analysis_plan", "Data analysis plan", "analysis_plan"),
    ("qa_and_ethics", "Quality assurance and ethics", "qa_and_ethics"),
    ("risk_register", "Risk management", "risk_register"),
    ("team_section", "Proposed team", "team_section"),
    ("work_plan", "Work plan", "work_plan"),
)
_HOUSE_EOI = (
    ("cover_letter", "Letter of interest", "cover_letter"),
    ("firm_profile", "Firm profile", "firm_profile"),
    ("understanding", "Understanding of the assignment", "understanding"),
    ("approach_summary", "Proposed approach", "approach_summary"),
    ("relevant_experience", "Relevant experience", "relevant_experience"),
    ("key_experts", "Key experts", "key_experts"),
    ("eligibility", "Eligibility", "eligibility"),
)


def _house_sections(submission_type: str) -> list[dict]:
    rows = _HOUSE_EOI if submission_type == "EOI" else _HOUSE_TECHNICAL
    return [{"key": key, "heading": heading, "route": route} for key, heading, route in rows]


def build_claim_sheet(analysis: dict | None, outline: dict | None, submission_type: str = "FULL_PROPOSAL") -> dict:
    """One owner per extracted fact. Empty analysis yields an empty sheet."""
    sections = _outline_sections(outline)
    if not sections:
        sections = _house_sections(submission_type)
    owned_by_siblings = chapters_owned_by_siblings(
        [item.get("route") for item in sections],
        [item.get("heading") for item in sections],
    )
    body_owners = {}
    for item in sections:
        if item.get("route") != "technical_proposal_body":
            continue
        for chapter in _BODY_CHAPTERS:
            if chapter in owned_by_siblings:
                continue
            body_owners.setdefault(chapter, chapter)
    claims = []
    seen = set()
    for topic, text in _claim_lines(analysis or {}):
        owner = _owner_for(topic, text, sections, body_owners)
        if not owner:
            continue
        mark = (owner, _norm_para(text))
        if mark in seen:
            continue
        seen.add(mark)
        claims.append({"owner": owner, "topic": topic, "text": text})
    return {
        "sections": [
            {
                "key": str(item.get("key")),
                "heading": str(item.get("heading") or item.get("key")),
                "route": str(item.get("route") or ""),
            }
            for item in sections
        ],
        "claims": claims,
    }


def brief_for_section(sheet: dict | None, key: str) -> str:
    """The instruction prepended to one chapter. Empty when there is no sheet."""
    sheet = sheet if isinstance(sheet, dict) else {}
    claims = [item for item in (sheet.get("claims") or []) if isinstance(item, dict)]
    sections = [item for item in (sheet.get("sections") or []) if isinstance(item, dict)]
    if not claims and not sections:
        return ""
    mine = next((item for item in sections if str(item.get("key")) == key), None)
    heading = str((mine or {}).get("heading") or key.replace("_", " "))
    outline_keys = {str(item.get("key")) for item in sections}
    mine_claims = [str(item.get("text") or "") for item in claims if item.get("owner") == key and item.get("text")]
    scored = [
        str(item.get("text") or "")
        for item in claims
        if item.get("owner") == key and item.get("topic") == "criterion" and item.get("text")
    ]
    if key in outline_keys:
        others = [
            str(item.get("heading") or item.get("key"))
            for item in sections
            if str(item.get("key")) != key
        ]
    else:
        others = [
            str(item.get("heading") or item.get("key"))
            for item in sections
            if item.get("route") != "technical_proposal_body"
        ]
    lines = [
        f"CHAPTER OWNERSHIP: write only “{heading}”.",
        "Do not write any other chapter inside this one.",
        "Do not add a cover letter, a salutation, or a sign-off unless this chapter is the cover letter.",
    ]
    if mine_claims:
        lines.append("FACTS THIS CHAPTER OWNS. Use these. Do not invent dates, sites, sample sizes, or deliverables beyond them and the tender pack:")
        lines.extend(f"- {text}" for text in mine_claims[:8])
    if scored:
        lines.append(
            "SCORED ANSWERS. A panel marks these. Each one is its own paragraph "
            "of at least 60 words: the criterion's name, how Cortech meets it "
            "here, and the evidence. Naming it is not a mark."
        )
        lines.extend(f"- {text}" for text in scored[:8])
    if others:
        lines.append("OTHER CHAPTERS ALREADY COVER (do not write them again):")
        lines.extend(f"- {text}" for text in others[:12])
    return "\n".join(lines)


def _is_cover(key: str, heading: str, route: str) -> bool:
    if route == "cover_letter" or key in {"cover_letter", "letter_of_interest"}:
        return True
    return bool(_LETTER_HEADING.search(heading or ""))


def _is_exec(key: str, heading: str, route: str) -> bool:
    if route == "executive_summary" or key == "executive_summary":
        return True
    return bool(_EXEC_HEADING.search(heading or ""))


def _order_pairs(sections: dict) -> list[tuple[str, str]]:
    order = sections.get("section_order")
    pairs = []
    if isinstance(order, list) and order:
        for item in order:
            if isinstance(item, dict) and item.get("key"):
                pairs.append((str(item["key"]), str(item.get("heading") or item["key"])))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append((str(item[0]), str(item[1] or item[0])))
    if pairs:
        return pairs
    present = {
        key for key, value in sections.items()
        if isinstance(value, str) and value.strip()
    }
    ranked = [(key, key.replace("_", " ").title()) for key in _PREFERRED_ORDER if key in present]
    ranked_keys = {key for key, _heading in ranked}
    for key in present:
        if key not in ranked_keys:
            ranked.append((key, key.replace("_", " ").title()))
    return ranked


def lock_front_matter(sections: dict) -> dict:
    """Cover letter first, executive summary second, then the approved order."""
    sections = sections if isinstance(sections, dict) else {}
    routes = {}
    outline = sections.get("submission_outline") if isinstance(sections.get("submission_outline"), dict) else {}
    for item in _outline_sections(outline):
        routes[str(item["key"])] = str(item.get("route") or "")
    pairs = _order_pairs(sections)
    known = {key for key, _heading in pairs}
    for key, value in sections.items():
        if not isinstance(value, str) or not value.strip() or key in known:
            continue
        heading = key.replace("_", " ")
        if _is_cover(key, heading, routes.get(key, "")) or _is_exec(key, heading, routes.get(key, "")):
            pairs.append((key, heading.title()))
    covers = []
    summaries = []
    rest = []
    for key, heading in pairs:
        route = routes.get(key, "")
        if _is_cover(key, heading, route):
            covers.append((key, heading))
        elif _is_exec(key, heading, route):
            summaries.append((key, heading))
        else:
            rest.append((key, heading))
    sections["section_order"] = covers + summaries + rest
    return sections


def _paragraphs(text: str) -> list[str]:
    return re.split(r"\n\s*\n", text or "")


def _similar(left: str, right: str) -> bool:
    a = _tokens(left)
    b = _tokens(right)
    if len(a) < 12 or len(b) < 12:
        return _norm_para(left) == _norm_para(right) and len(_norm_para(left).split()) >= 18
    overlap = len(a & b) / len(a | b)
    return overlap >= 0.72


def strip_repeated_passages(sections: dict) -> int:
    """Drop a later paragraph that repeats an earlier chapter. Returns how many were dropped."""
    order = [key for key, _heading in _order_pairs(sections)]
    seen: list[str] = []
    dropped = 0
    for key in order:
        text = sections.get(key)
        if not isinstance(text, str) or not text.strip():
            continue
        kept = []
        for para in _paragraphs(text):
            if any(_similar(para, earlier) for earlier in seen):
                dropped += 1
                continue
            kept.append(para)
            if len(_tokens(para)) >= 12:
                seen.append(para)
        sections[key] = "\n\n".join(part for part in kept if part.strip()).strip()
    return dropped


def _same_chapter(left: str, right: str) -> bool:
    a = _tokens(left)
    b = _tokens(right)
    if not a or not b:
        return False
    return a <= b or b <= a or len(a & b) >= 2


def strip_nested_chapters(sections: dict) -> int:
    """Remove a ## block whose title is already its own chapter."""
    order = _order_pairs(sections)
    headings = [(key, heading) for key, heading in order]
    dropped = 0
    for key, _heading in headings:
        text = sections.get(key)
        if not isinstance(text, str) or "##" not in text:
            continue
        owned = [heading for other, heading in headings if other != key]
        pieces = re.split(r"(?=^#{1,3}\s+)", text, flags=re.M)
        kept = []
        for piece in pieces:
            match = _HEADING.match(piece.strip())
            title = match.group(2) if match else ""
            if title and any(_same_chapter(title, other) for other in owned):
                dropped += 1
                continue
            kept.append(piece)
        sections[key] = "\n\n".join(part.strip() for part in kept if part.strip()).strip()
    return dropped


def strip_buried_letters(sections: dict) -> int:
    """A letter that belongs at the front is removed from a later chapter."""
    order = _order_pairs(sections)
    routes = {}
    outline = sections.get("submission_outline") if isinstance(sections.get("submission_outline"), dict) else {}
    for item in _outline_sections(outline):
        routes[str(item["key"])] = str(item.get("route") or "")
    has_letter = any(
        _is_cover(key, heading, routes.get(key, ""))
        and isinstance(sections.get(key), str)
        and sections.get(key).strip()
        for key, heading in order
    )
    if not has_letter:
        return 0
    dropped = 0
    for key, heading in order:
        if _is_cover(key, heading, routes.get(key, "")):
            continue
        text = sections.get(key)
        if not isinstance(text, str):
            continue
        paras = _paragraphs(text)
        cut = None
        for index, para in enumerate(paras):
            if _LETTER_OPEN.search(para) and any(
                _SIGNOFF.search(later) for later in paras[index:]
            ):
                cut = index
                break
        if cut is None:
            continue
        dropped += 1
        sections[key] = "\n\n".join(paras[:cut]).strip()
    return dropped


def compile_client_draft(sections: dict) -> dict:
    """Order the file, then drop repeated chapters and a letter buried at the end."""
    sections = lock_front_matter(sections if isinstance(sections, dict) else {})
    nested = strip_nested_chapters(sections)
    repeated = strip_repeated_passages(sections)
    buried = strip_buried_letters(sections)
    sections["draft_editor"] = {
        "nested_chapters_removed": nested,
        "repeated_passages_removed": repeated,
        "buried_letters_removed": buried,
    }
    return sections
