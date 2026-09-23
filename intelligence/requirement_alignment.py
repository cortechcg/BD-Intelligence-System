"""Trace a draft back to extracted tender requirements.

Statuses are addressed, partially_addressed, or not_addressed.
A row is addressed only when a real drafted section contains the evidence
cited on that row. If the trace fails, the requirement stays not_addressed.
This is the same fail-closed rule as claim grounding: an untraceable
requirement is never treated as satisfied.
"""

from __future__ import annotations

import re
from datetime import datetime

from intelligence.tender_reader import estimated_pages, word_count

STATUSES = ("addressed", "partially_addressed", "not_addressed")

# Metadata stored beside the draft. Not client-facing sections.
_SKIP_KEYS = {
    "submission_type",
    "lightweight",
    "lightweight_reason",
    "quality_score",
    "claim_grounding",
    "tender_brief",
    "win_strategy",
    "document_lock",
    "section_order",
    "omitted_financial",
    "submission_outline",
    "required_attachments",
    "required_forms",
    "format_compliance",
    "requirement_alignment",
}

# Words that appear in almost every technical proposal. Matching only these
# does not show that a scored criterion was answered.
_WEAK = {
    "approach",
    "activities",
    "capacity",
    "criteria",
    "criterion",
    "deliverable",
    "deliverables",
    "evaluation",
    "experience",
    "implementation",
    "management",
    "methodology",
    "objective",
    "objectives",
    "plan",
    "proposal",
    "quality",
    "relevant",
    "report",
    "team",
    "technical",
    "understanding",
    "work",
}

_STOP = _WEAK | {
    "about",
    "after",
    "against",
    "also",
    "and",
    "any",
    "are",
    "based",
    "been",
    "before",
    "between",
    "bidder",
    "both",
    "consultant",
    "during",
    "each",
    "for",
    "from",
    "have",
    "including",
    "into",
    "must",
    "not",
    "only",
    "other",
    "over",
    "proposed",
    "required",
    "requirement",
    "requirements",
    "section",
    "shall",
    "should",
    "such",
    "than",
    "that",
    "their",
    "these",
    "this",
    "those",
    "through",
    "across",
    "been",
    "details",
    "similar",
    "three",
    "under",
    "used",
    "using",
    "were",
    "when",
    "whom",
    "where",
    "which",
    "while",
    "will",
    "with",
    "within",
    "without",
}

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'+-]{3,}", re.I)
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")


def align_draft_to_requirements(
    analysis: dict | None,
    sections: dict | None,
    outline: dict | None = None,
) -> dict:
    """Cross-check drafted sections against extracted requirements."""
    try:
        requirements = collect_requirements(analysis, outline)
        drafted = _draft_sections(sections)
        rows = [_trace(requirement, drafted, sections or {}) for requirement in requirements]
        return _report(rows)
    except Exception as exc:
        return fail_closed_requirement_alignment(
            analysis,
            outline=outline,
            error=str(exc),
        )


def fail_closed_requirement_alignment(
    analysis: dict | None = None,
    outline: dict | None = None,
    *,
    error: str = "",
) -> dict:
    """Every extracted requirement stays not addressed when tracing cannot run."""
    try:
        requirements = collect_requirements(analysis, outline)
    except Exception:
        requirements = []
    rows = []
    for requirement in requirements:
        row = dict(requirement)
        row["status"] = "not_addressed"
        row["section"] = ""
        row["quote"] = ""
        row["evidence"] = "Not traced; not treated as satisfied."
        rows.append(row)
    return _report(rows, error=error or "alignment failed closed")


def collect_requirements(analysis: dict | None, outline: dict | None = None) -> list[dict]:
    """Structured requirements the draft can be checked against. No inference."""
    analysis = analysis if isinstance(analysis, dict) else {}
    outline = outline if isinstance(outline, dict) else {}
    submission = analysis.get("submission_requirements")
    if not isinstance(submission, dict):
        submission = {}
    opportunity = analysis.get("opportunity")
    if not isinstance(opportunity, dict):
        opportunity = {}

    rows: list[dict] = []
    seen: set[str] = set()

    def add(row: dict) -> None:
        key = re.sub(r"[^a-z0-9]+", " ", (row.get("label") or "").lower()).strip()
        kind = row.get("kind") or ""
        if len(key) < 3:
            return
        token = f"{kind}|{key}"
        for existing in seen:
            existing_kind, existing_key = existing.split("|", 1)
            if existing_kind != kind:
                continue
            if key == existing_key or key in existing_key or existing_key in key:
                return
        seen.add(token)
        rows.append(row)

    criteria = analysis.get("evaluation_criteria")
    if isinstance(criteria, list):
        for index, item in enumerate(criteria):
            if not isinstance(item, dict):
                continue
            name = _clean(item.get("criterion"))
            if not name:
                continue
            weight = item.get("weight_percent")
            label = name
            if isinstance(weight, (int, float)) and not isinstance(weight, bool):
                label = f"{name} ({weight:g}%)"
            add({
                "id": f"evaluation_criterion:{index}",
                "kind": "evaluation_criterion",
                "label": label,
                "text": " ".join(
                    part for part in (name, _clean(item.get("description"))) if part
                ),
                "weight_percent": weight if isinstance(weight, (int, float)) else None,
                "source": f"evaluation_criteria[{index}]",
            })

    # The outline is the chapter list the writer actually used. The analyzer
    # often stores that list as one compound sentence, which will never match
    # a drafted heading. Prefer the split headings when they exist.
    outline_sections = [
        item for item in (outline.get("sections") or [])
        if isinstance(item, dict) and _clean(item.get("heading"))
    ]
    if outline.get("prescribed") and outline_sections:
        prescribed_rows = [
            (item.get("heading"), "submission_outline.sections")
            for item in outline_sections
        ]
    else:
        raw_prescribed = submission.get("prescribed_proposal_sections")
        if not isinstance(raw_prescribed, list):
            raw_prescribed = []
        prescribed_rows = [
            (heading, "submission_requirements.prescribed_proposal_sections")
            for heading in raw_prescribed
        ]
    for index, (heading, source) in enumerate(prescribed_rows):
        title = _clean(heading)
        if not title:
            continue
        add({
            "id": f"prescribed_section:{index}",
            "kind": "prescribed_section",
            "label": title,
            "text": title,
            "source": source,
        })

    annexes = submission.get("required_annexes")
    if not isinstance(annexes, list):
        annexes = []
    annexes = list(annexes)
    for extra in list(outline.get("required_attachments") or []) + list(
        outline.get("required_forms") or []
    ):
        annexes.append(extra)
    for index, name in enumerate(annexes):
        title = _clean(name)
        if not title:
            continue
        add({
            "id": f"required_annex:{index}",
            "kind": "required_annex",
            "label": title,
            "text": title,
            "source": "submission_requirements.required_annexes",
        })

    conditions = submission.get("disqualifying_conditions")
    if isinstance(conditions, list):
        for index, condition in enumerate(conditions):
            text = _clean(condition)
            if not text:
                continue
            add({
                "id": f"disqualifier:{index}",
                "kind": "disqualifying_condition",
                "label": text,
                "text": text,
                "source": "submission_requirements.disqualifying_conditions",
            })

    deadline = _clean(opportunity.get("submission_deadline"))
    if deadline:
        add({
            "id": "deadline",
            "kind": "deadline",
            "label": f"Submission deadline {deadline}",
            "text": deadline,
            "source": "opportunity.submission_deadline",
        })

    page_limit = submission.get("technical_proposal_page_limit")
    if isinstance(page_limit, (int, float)) and not isinstance(page_limit, bool) and page_limit > 0:
        add({
            "id": "page_limit",
            "kind": "page_limit",
            "label": f"Technical proposal page limit ({page_limit:g})",
            "text": "",
            "page_limit": float(page_limit),
            "source": "submission_requirements.technical_proposal_page_limit",
        })

    if submission.get("financial_proposal_required") is True:
        add({
            "id": "financial",
            "kind": "financial_proposal",
            "label": "Financial proposal",
            "text": "",
            "source": "submission_requirements.financial_proposal_required",
        })
    if submission.get("cvs_required") is True:
        add({
            "id": "cvs",
            "kind": "cvs",
            "label": "CVs required",
            "text": "",
            "source": "submission_requirements.cvs_required",
        })
    return rows


def _trace(requirement: dict, drafted: list[tuple[str, str, str]], sections: dict) -> dict:
    kind = requirement.get("kind")
    if kind == "evaluation_criterion" or kind == "deadline":
        return _trace_lexical(requirement, drafted)
    if kind == "prescribed_section" or kind == "required_annex":
        return _trace_heading(requirement, drafted, sections)
    if kind == "page_limit":
        return _trace_page_limit(requirement, drafted)
    if kind == "financial_proposal":
        return _flag(
            requirement,
            "This pipeline drafts the technical file only. The financial envelope is not in the draft.",
        )
    if kind == "cvs":
        return _trace_cvs(requirement, drafted)
    if kind == "disqualifying_condition":
        return _flag(
            requirement,
            "Disqualifying condition extracted from the tender. A mention in the draft is not treated as compliance.",
        )
    return _flag(requirement, "Not traced; not treated as satisfied.")


def _trace_lexical(requirement: dict, drafted: list[tuple[str, str, str]]) -> dict:
    name_text, detail_text = _criterion_parts(requirement)
    name_strong, name_weak = _terms(name_text)
    detail_strong, _detail_weak = _terms(detail_text)
    if kind_is_deadline(requirement):
        name_strong = _deadline_terms(requirement.get("text") or "")
        name_weak = []
        detail_strong = []
    if not name_strong and len(name_weak) < 2 and len(detail_strong) < 2:
        return _flag(
            requirement,
            "Too few distinctive terms to trace to a section. Not treated as satisfied.",
        )
    best_section = ""
    best_quote = ""
    best_name: list[str] = []
    best_detail: list[str] = []
    best_score = 0
    for key, heading, text in drafted:
        heading_bonus = 4 if any(_contains(heading, term) for term in name_strong) else 0
        for sentence in _sentences(text):
            found_name = [term for term in name_strong if _contains(sentence, term)]
            found_detail = [term for term in detail_strong if _contains(sentence, term)]
            score = len(found_name) * 5 + len(found_detail) + heading_bonus
            if score > best_score and sentence in text:
                best_score = score
                best_section = key
                best_quote = sentence.strip()[:400]
                best_name = found_name
                best_detail = found_detail
    if not best_quote or best_quote not in _section_text(drafted, best_section):
        return _flag(requirement, "No drafted sentence contains this requirement. Not treated as satisfied.")
    # The criterion's own words must be in the quote. Description overlap
    # alone (method names repeated in an experience paragraph) is partial.
    addressed = len(best_name) >= 2 or (
        kind_is_deadline(requirement) and len(best_name) >= 1
    )
    partial = (not addressed) and (len(best_name) >= 1 or len(best_detail) >= 2)
    if not addressed and not partial:
        return _flag(requirement, "No drafted sentence contains this requirement. Not treated as satisfied.")
    status = "addressed" if addressed else "partially_addressed"
    return _cited(
        requirement,
        status,
        best_section,
        best_quote,
        f"Cited from {best_section}.",
    )


def _trace_heading(requirement: dict, drafted: list[tuple[str, str, str]], sections: dict) -> dict:
    wanted = _norm(requirement.get("text") or "")
    wanted_terms = [term for term in wanted.split() if term not in _STOP and len(term) >= 4]
    if len(wanted) < 3:
        return _flag(requirement, "Heading was empty. Not treated as satisfied.")
    for key, heading, text in drafted:
        heading_norm = _norm(heading)
        key_norm = _norm(key.replace("_", " "))
        heading_hit = wanted in heading_norm or wanted in key_norm or (
            wanted_terms and all(term in heading_norm or term in key_norm for term in wanted_terms)
        )
        if not heading_hit:
            continue
        quote = _opening(text)
        if not quote or quote not in text:
            continue
        words = word_count(text)
        # Ignore a leading heading line. A chapter titled correctly but filled
        # with unrelated prose is not treated as satisfying the requirement.
        # A heading made only of generic words has nothing further to trace.
        first, _, rest = text.partition("\n")
        body = rest if _norm(first) == wanted or _norm(first) == _norm(heading) else text
        content_quote = ""
        if wanted_terms:
            needed = min(2, len(wanted_terms))
            for sentence in _sentences(body):
                hits = [term for term in wanted_terms if _contains(sentence, term)]
                if len(hits) >= needed and sentence in text:
                    content_quote = sentence.strip()[:400]
                    break
        else:
            content_quote = _opening(body or text)
        traced = bool(content_quote) and content_quote in text
        if words >= 80 and traced:
            return _cited(
                requirement,
                "addressed",
                key,
                content_quote,
                f"Section {key} is present ({words} words) and its body uses this requirement's terms.",
            )
        if words >= 20 and quote in text:
            return _cited(
                requirement,
                "partially_addressed",
                key,
                quote,
                f"Section {key} is present ({words} words) but its body was not traced to this requirement.",
            )
    _ = sections
    return _flag(
        requirement,
        "No drafted section heading matches this requirement.",
    )


def _trace_page_limit(requirement: dict, drafted: list[tuple[str, str, str]]) -> dict:
    if not drafted:
        return _flag(requirement, "No draft to measure. Page limit is not treated as satisfied.")
    limit = float(requirement.get("page_limit") or 0)
    body = "\n\n".join(text for _key, _heading, text in drafted)
    pages = estimated_pages(body)
    words = word_count(body)
    if pages > limit * 1.08:
        return _flag(
            requirement,
            f"Estimated {pages:g} pages from {words} words, over the stated limit of {limit:g}. "
            "Rendered pages were not measured.",
        )
    row = _flag(
        requirement,
        f"Estimated {pages:g} pages from {words} words, within the stated limit of {limit:g}. "
        "Rendered pages were not measured, so the limit is not treated as satisfied.",
    )
    row["status"] = "partially_addressed"
    return row


def _trace_cvs(requirement: dict, drafted: list[tuple[str, str, str]]) -> dict:
    for key, _heading, text in drafted:
        if key not in {"team_section", "key_experts", "experts"} and "team" not in key and "expert" not in key:
            continue
        quote = _opening(text)
        if word_count(text) >= 40 and quote and quote in text:
            row = _cited(
                requirement,
                "partially_addressed",
                key,
                quote,
                "A team narrative is drafted. CVs are not attached, so this is not treated as satisfied.",
            )
            row["status"] = "partially_addressed"
            return row
    return _flag(requirement, "ToR requires CVs. No team section in the draft, and CVs are not attached.")


def _report(rows: list[dict], error: str = "") -> dict:
    for row in rows:
        if row.get("status") not in STATUSES:
            row["status"] = "not_addressed"
            row["section"] = ""
            row["quote"] = ""
    criteria = [row for row in rows if row.get("kind") == "evaluation_criterion"]
    criteria_addressed = sum(1 for row in criteria if row.get("status") == "addressed")
    missing = [
        row.get("label") or ""
        for row in criteria
        if row.get("status") == "not_addressed"
    ]
    other_missing = [
        row.get("label") or ""
        for row in rows
        if row.get("kind") != "evaluation_criterion" and row.get("status") == "not_addressed"
    ]
    if criteria:
        headline = (
            f"This draft addresses {criteria_addressed} of {len(criteria)} evaluation criteria"
        )
        if missing:
            headline += "; missing: " + "; ".join(missing[:8])
        partial_criteria = sum(
            1 for row in criteria if row.get("status") == "partially_addressed"
        )
        headline += "."
        if partial_criteria:
            headline += (
                f" {partial_criteria} of {len(criteria)} are only partially addressed."
            )
    elif rows:
        headline = "No evaluation criteria were extracted. Other requirements are listed below."
    else:
        headline = (
            "No evaluation criteria or mandatory items were extracted, "
            "so this draft was not checked against a requirement list."
        )
    disqualifiers = [
        row for row in rows if row.get("kind") == "disqualifying_condition"
    ]
    other_missing = [
        item for item in other_missing
        if item and not any(item == (row.get("label") or "") for row in disqualifiers)
    ]
    if other_missing:
        headline += " Not addressed: " + "; ".join(other_missing[:6]) + "."
    if disqualifiers:
        headline += (
            f" {len(disqualifiers)} disqualifying conditions were extracted "
            "and are not treated as satisfied."
        )
    return {
        "rows": rows,
        "addressed": sum(1 for row in rows if row.get("status") == "addressed"),
        "partially_addressed": sum(
            1 for row in rows if row.get("status") == "partially_addressed"
        ),
        "not_addressed": sum(1 for row in rows if row.get("status") == "not_addressed"),
        "total": len(rows),
        "criteria_addressed": criteria_addressed,
        "criteria_total": len(criteria),
        "missing": [item for item in missing if item],
        "headline": headline,
        "method": "section_trace",
        "error": error,
    }


def _draft_sections(sections: dict | None) -> list[tuple[str, str, str]]:
    sections = sections if isinstance(sections, dict) else {}
    headings = {}
    order = sections.get("section_order")
    if isinstance(order, list):
        for item in order:
            if isinstance(item, dict) and item.get("key"):
                headings[str(item["key"])] = str(item.get("heading") or item["key"])
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                headings[str(item[0])] = str(item[1] or item[0])
    drafted = []
    for key, value in sections.items():
        if key in _SKIP_KEYS or not isinstance(value, str) or not value.strip():
            continue
        drafted.append((str(key), headings.get(str(key), str(key).replace("_", " ")), value))
    return drafted


def _criterion_parts(requirement: dict) -> tuple[str, str]:
    """Name is what must be quoted. Description can only support a partial."""
    text = requirement.get("text") or ""
    if requirement.get("kind") != "evaluation_criterion":
        return text, ""
    label = requirement.get("label") or ""
    # Label may end with " (40%)".
    name = re.sub(r"\s*\([^)]*%\)\s*$", "", label).strip()
    if name and text.lower().startswith(name.lower()):
        detail = text[len(name):].strip()
    else:
        detail = ""
        name = text
    return name, detail


def _terms(text: str) -> tuple[list[str], list[str]]:
    strong = []
    weak = []
    seen = set()
    for match in _WORD_RE.findall(text or ""):
        term = match.lower()
        if term in seen or term.isdigit():
            continue
        seen.add(term)
        if term in _STOP and term not in _WEAK:
            continue
        if term in _WEAK:
            weak.append(term)
        else:
            strong.append(term)
    return strong, weak


def _deadline_terms(value: str) -> list[str]:
    text = (value or "").strip()
    forms = [text.lower()]
    parsed = None
    for pattern in ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(text, pattern)
            break
        except ValueError:
            continue
    if parsed is not None:
        forms.extend([
            parsed.strftime("%Y-%m-%d").lower(),
            parsed.strftime("%d %B %Y").lower(),
            parsed.strftime("%B %d, %Y").lower(),
            parsed.strftime("%d/%m/%Y").lower(),
            f"{parsed.day} {parsed.strftime('%B').lower()} {parsed.year}",
        ])
    unique = []
    for form in forms:
        if form and form not in unique:
            unique.append(form)
    return unique


def kind_is_deadline(requirement: dict) -> bool:
    return requirement.get("kind") == "deadline"


def _contains(text: str, term: str) -> bool:
    if " " in term or "/" in term or "-" in term:
        return term.lower() in (text or "").lower()
    return re.search(
        rf"(?<![a-z0-9]){re.escape(term)}(?:s|es)?(?![a-z0-9])",
        text or "",
        re.I,
    ) is not None


def _sentences(text: str) -> list[str]:
    """Return exact slices of `text` so a citation can be checked as a substring."""
    source = text or ""
    parts = []
    for match in _SENTENCE_RE.finditer(source):
        sentence = match.group().strip()
        if len(" ".join(sentence.split())) >= 12 and sentence in source:
            parts.append(sentence)
    if parts:
        return parts
    fallback = source.strip()[:400]
    return [fallback] if fallback else []


def _opening(text: str) -> str:
    sentences = _sentences(text)
    return sentences[0][:400] if sentences else ""


def _section_text(drafted: list[tuple[str, str, str]], key: str) -> str:
    for drafted_key, _heading, text in drafted:
        if drafted_key == key:
            return text
    return ""


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _clean(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _flag(requirement: dict, evidence: str) -> dict:
    row = dict(requirement)
    row["status"] = "not_addressed"
    row["section"] = ""
    row["quote"] = ""
    row["evidence"] = evidence
    return row


def _cited(requirement: dict, status: str, section: str, quote: str, evidence: str) -> dict:
    row = dict(requirement)
    row["status"] = status if status in STATUSES else "not_addressed"
    row["section"] = section
    row["quote"] = quote
    row["evidence"] = evidence
    if row["status"] != "not_addressed" and (not section or not quote):
        row["status"] = "not_addressed"
        row["section"] = ""
        row["quote"] = ""
        row["evidence"] = "Citation missing. Not treated as satisfied."
    return row
