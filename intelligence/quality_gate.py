"""The quality gate every draft passes before anyone is told it is ready.

The gate scores the draft that was actually written, not the model's opinion
of it. A draft is READY only when it scores at least QUALITY_BAR of 100 and
has no blocker. Anything else is BLOCKED, and the review email subject and the
dashboard both say so.

Criteria, in order, and the source is always named:

1. ``tor``             the ToR's evaluation criteria with the ToR's weights;
2. ``tor_unweighted``  the ToR's criteria, weighted equally. The ToR states
                       no weights, so no weight figure is claimed;
3. ``reader``          the reading brief's "Scored criteria" list, weighted
                       equally (a summary is not a source for weights);
4. ``standard_inferred`` a standard technical-evaluation list, every row
                       marked inferred, used only when nothing else exists.

Each criterion earns the traced alignment status of the drafted text
(addressed 1, partial 0.5, not addressed 0). A criterion whose cited section
rests on [INSUFFICIENT EVIDENCE], [NOT VERIFIED], a placeholder, or an
unverified claim earns zero.

Blockers hold the draft at BLOCKED whatever the score, because a procurement
or technical committee would see them at once: placeholders, unverified named
past work or unsupported claims, leaked template numbering ("Section 1",
"5.1"), and an evaluation weight that the ToR does not state.
"""
from __future__ import annotations

import copy
import re

QUALITY_BAR = 70
MAX_REPAIR_ROUNDS = 2

READY = "READY"
BLOCKED = "BLOCKED"

SOURCE_TOR = "tor"
SOURCE_TOR_UNWEIGHTED = "tor_unweighted"
SOURCE_READER = "reader"
SOURCE_STANDARD = "standard_inferred"

SOURCE_NOTES = {
    SOURCE_TOR: "Scored against the ToR's evaluation criteria and the ToR's weights.",
    SOURCE_TOR_UNWEIGHTED: (
        "Scored against the ToR's evaluation criteria. The ToR states no weights, "
        "so the criteria are weighted equally and no weight figure is claimed."
    ),
    SOURCE_READER: (
        "The ToR's extraction holds no evaluation criteria. Scored against the "
        "reading brief's criteria, weighted equally. Confirm the evaluation method "
        "with the client."
    ),
    SOURCE_STANDARD: (
        "Neither the ToR nor the reading brief states evaluation criteria. Scored "
        "against standard technical-evaluation criteria, every one INFERRED. "
        "Confirm the evaluation method with the client."
    ),
}

#: Used only when neither the ToR nor the reader gives criteria.
STANDARD_CRITERIA = (
    ("Understanding of the assignment and context",
     "Shows the client's objectives, scope, context and intended use of the results."),
    ("Technical approach and methodology",
     "Method, sampling, tools, analysis and ethics fitted to the stated objectives."),
    ("Work plan and timeline",
     "Activities, deliverables and dates consistent with the stated period."),
    ("Team qualifications and experience",
     "Named experts whose qualifications match the roles the assignment needs."),
    ("Relevant firm experience",
     "Verified past assignments similar in subject, geography and client type."),
    ("Quality assurance, risk and safeguarding",
     "Data quality controls, risks with mitigations, safeguarding and do-no-harm."),
)

_META_KEYS = {
    "submission_type", "lightweight", "lightweight_reason", "quality_score",
    "claim_grounding", "tender_brief", "win_strategy", "document_lock",
    "section_order", "omitted_financial", "submission_outline",
    "required_attachments", "required_forms", "format_compliance",
    "requirement_alignment", "claim_sheet", "draft_editor", "committee_grade",
    "quality_gate",
}

_MARKERS = re.compile(
    r"\[(?:INSUFFICIENT EVIDENCE|NOT VERIFIED|UNKNOWN|TBD|TBC|TO BE CONFIRMED|PLACEHOLDER|"
    r"insert[^\]]*|add[^\]]*|name[^\]]*|date[^\]]*|client[^\]]*|X+)\]"
    r"|\bLorem ipsum\b|\bXX+\b|\bTBD\b|\bTBC\b|<insert[^>]*>|\{\{[^}]*\}\}",
    re.IGNORECASE,
)
# House numbering used as a heading: "Section 1: Introduction", "## Section 5.1",
# "**Section 2 — Method**". A citation of the client's own document inside a
# sentence ("Section 11.1 of the Request for Tender") is not a leak.
_SECTION_WORD = re.compile(
    r"(?m)(?:^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*Section\s+(\d+(?:\.\d+)*)\b"
    r"|\bSection\s+(\d+(?:\.\d+)*)\s*(?::|—|–|\s-\s))",
    re.IGNORECASE,
)
_NUMBERED_LINE = re.compile(r"(?m)^\s*(?:#{1,6}\s*)?(\d+(?:\.\d+)+)\.?\s+\S")
_AMOUNT = r"\b\d{1,3}\s?(?:%|(?:percent|per cent|points|marks)\b)"
_WEIGHT_CLAIM = re.compile(
    _AMOUNT + r"[^.\n]{0,60}"
    r"\b(?:weight|weighted|weighting|score|scoring|evaluation|technical|financial|marks?)\b"
    r"|\b(?:weight|weighted|weighting|evaluation|technical score|financial score)\b"
    r"[^.\n]{0,40}" + _AMOUNT,
    re.IGNORECASE,
)
_FRACTION = {"addressed": 1.0, "partially_addressed": 0.5}


def _clean(value) -> str:
    return " ".join(str(value or "").split()).strip()


def _drafted(sections: dict) -> dict[str, str]:
    return {
        str(k): v for k, v in (sections or {}).items()
        if k not in _META_KEYS and isinstance(v, str) and v.strip()
    }


# ── Criteria ────────────────────────────────────────────────────────────────

_READER_HEADING = re.compile(
    r"(?ms)^##\s*(?:Scored criteria[^\n]*|Shortlisting[^\n]*criteria[^\n]*)\n(.*?)(?=^##\s|\Z)"
)
_READER_BULLET = re.compile(r"(?m)^(?:[-*]|\d+[.)])\s+(.+)$")
_NONE_STATED = re.compile(
    r"none stated|not stated|no scoring matrix|no evaluation criteria|do not invent|"
    r"no weights|not specified|none given|not provided",
    re.IGNORECASE,
)


def reader_criteria(tender_brief: str) -> list[dict]:
    """Top-level bullets of the reading brief's scored-criteria list."""
    match = _READER_HEADING.search(tender_brief or "")
    if not match:
        return []
    out = []
    for bullet in _READER_BULLET.findall(match.group(1)):
        text = _clean(bullet)
        if not text or _NONE_STATED.search(text):
            continue
        bold = re.match(r"\*\*(.+?)\*\*", text)
        name = bold.group(1) if bold else re.split(r"\s[—–-]\s|:", text, maxsplit=1)[0]
        name = re.sub(r"\s*\((?:\d+[^)]*|unweighted)\)\s*", " ", name)
        name = _clean(name.replace("*", "")).rstrip(":").strip()
        if len(name) < 4:
            continue
        description = _clean(re.sub(r"\*\*.+?\*\*", "", text, count=1)).lstrip(":—–- ").strip()
        out.append({"criterion": name[:160], "description": description[:400], "weight_percent": None})
    return out


def gate_criteria(analysis: dict | None, sections: dict | None) -> tuple[list[dict], str]:
    """(criteria, source). Never returns a weight the ToR did not state."""
    analysis = analysis if isinstance(analysis, dict) else {}
    tor = [
        item for item in (analysis.get("evaluation_criteria") or [])
        if isinstance(item, dict) and _clean(item.get("criterion"))
    ]
    if tor:
        weights = [item.get("weight_percent") for item in tor]
        stated = all(
            isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0 for w in weights
        )
        rows = [
            {
                "criterion": _clean(item.get("criterion")),
                "description": _clean(item.get("description")),
                "weight_percent": float(item["weight_percent"]) if stated else None,
            }
            for item in tor
        ]
        return rows, SOURCE_TOR if stated else SOURCE_TOR_UNWEIGHTED
    reader = reader_criteria(str((sections or {}).get("tender_brief") or ""))
    if reader:
        return reader, SOURCE_READER
    return [
        {"criterion": f"{name} (inferred)", "description": desc, "weight_percent": None}
        for name, desc in STANDARD_CRITERIA
    ], SOURCE_STANDARD


# ── Blockers ────────────────────────────────────────────────────────────────

def _prescribed_numbers(sections: dict) -> set[str]:
    """Numbering the client's own outline uses is not leaked numbering."""
    outline = sections.get("submission_outline") if isinstance(sections.get("submission_outline"), dict) else {}
    if not outline.get("prescribed"):
        return set()
    found = set()
    for item in outline.get("sections") or []:
        heading = str((item or {}).get("heading") or "") if isinstance(item, dict) else ""
        found.update(re.findall(r"\d+(?:\.\d+)+|\b\d+\b", heading))
    return found


def _is_citation_sentence(text: str, match: re.Match) -> bool:
    """'Section 6 of the ToR requires ...' opens a sentence; it is not a heading."""
    line_end = text.find("\n", match.end())
    rest = text[match.end(): line_end if line_end >= 0 else len(text)]
    if re.match(r"\s*(?::|—|–|\s-\s)", rest):
        return False
    if re.match(r"\s+(?:of|in|also|asks|requires|states|sets|lists|says)\b", rest, re.IGNORECASE):
        return True
    return len(rest.split()) > 12


def find_blockers(sections: dict, criteria_source: str) -> list[dict]:
    """Every defect a committee would see, by section. Values are quoted short."""
    blockers: list[dict] = []
    allowed_numbers = _prescribed_numbers(sections)
    for key, text in _drafted(sections).items():
        for m in _MARKERS.finditer(text):
            blockers.append({"kind": "placeholder", "section": key, "text": m.group(0)[:60]})
        for m in _SECTION_WORD.finditer(text):
            number = m.group(1) or m.group(2)
            if m.group(1) and _is_citation_sentence(text, m):
                continue
            if m.group(2) and not re.search(r"[.!?\]]\s*$", text[max(0, m.start() - 3):m.start()]):
                continue  # "... in section 4: Home, About" is a reference
            if number not in allowed_numbers:
                blockers.append({
                    "kind": "template_numbering", "section": key, "text": f"Section {number}",
                })
        for m in _NUMBERED_LINE.finditer(text):
            if m.group(1) not in allowed_numbers:
                blockers.append({"kind": "template_numbering", "section": key, "text": m.group(1)})
        if criteria_source != SOURCE_TOR:
            for m in _WEIGHT_CLAIM.finditer(text):
                blockers.append({
                    "kind": "invented_weight", "section": key, "text": _clean(m.group(0))[:80],
                })
    grounding = sections.get("claim_grounding") if isinstance(sections.get("claim_grounding"), dict) else {}
    for claim in grounding.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        status = claim.get("status")
        if status in ("NOT VERIFIED", "INSUFFICIENT EVIDENCE"):
            blockers.append({
                "kind": "unverified_claim" if status == "NOT VERIFIED" else "unsupported_claim",
                "section": str(claim.get("section") or ""),
                "text": _clean(claim.get("claim") or claim.get("sentence") or "")[:80],
            })
    if grounding.get("error"):
        blockers.append({"kind": "unverified_claim", "section": "", "text": "claim grounding failed"})
    return blockers


# ── Score ──────────────────────────────────────────────────────────────────

def score_draft(sections: dict, analysis: dict | None) -> dict:
    """Score the draft as written. Pure apart from the alignment trace."""
    from intelligence.requirement_alignment import align_draft_to_requirements

    sections = sections if isinstance(sections, dict) else {}
    criteria, source = gate_criteria(analysis, sections)
    scored_analysis = copy.deepcopy(analysis) if isinstance(analysis, dict) else {}
    scored_analysis["evaluation_criteria"] = criteria
    report = align_draft_to_requirements(
        scored_analysis, sections, outline=sections.get("submission_outline") or {}
    )
    blockers = find_blockers(sections, source)
    tainted = {b["section"] for b in blockers if b["kind"] in (
        "placeholder", "unverified_claim", "unsupported_claim",
    ) and b["section"]}

    rows = [r for r in report.get("rows") or [] if r.get("kind") == "evaluation_criterion"]
    weights = [r.get("weight_percent") if source == SOURCE_TOR else None for r in rows]
    if any(w is None for w in weights):
        weights = [1.0] * len(rows)
    total = sum(weights) or 1.0
    earned = 0.0
    out_rows = []
    for row, weight in zip(rows, weights):
        fraction = _FRACTION.get(row.get("status"), 0.0)
        section = str(row.get("section") or "")
        reason = str(row.get("reason") or "")
        if fraction and section in tainted:
            fraction = 0.0
            reason = (
                f"Earns zero: section '{section}' rests on [INSUFFICIENT EVIDENCE], "
                "a placeholder or an unverified claim."
            )
        earned += weight * fraction
        out_rows.append({
            "criterion": row.get("label") or row.get("text"),
            "weight": weight if source == SOURCE_TOR else None,
            "status": row.get("status"),
            "earned": round(fraction, 2),
            "section": section,
            "reason": reason,
            "inferred": source == SOURCE_STANDARD,
        })
    score = round(100.0 * earned / total, 1) if rows else 0.0
    status = READY if score >= QUALITY_BAR and not blockers else BLOCKED
    return {
        "status": status,
        "score": score,
        "bar": QUALITY_BAR,
        "criteria_source": source,
        "criteria_note": SOURCE_NOTES[source],
        "rows": out_rows,
        "blockers": blockers,
        "blocker_counts": _counts(blockers),
        "headline": headline(status, score, blockers, source),
    }


def _counts(blockers: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for b in blockers:
        counts[b["kind"]] = counts.get(b["kind"], 0) + 1
    return counts


_KIND_LABEL = {
    "placeholder": "placeholders",
    "unverified_claim": "unverified named past-work claims",
    "unsupported_claim": "unsupported claims",
    "template_numbering": "leaked template numbering",
    "invented_weight": "evaluation weights the ToR does not state",
}


def headline(status: str, score: float, blockers: list[dict], source: str) -> str:
    text = f"Quality {score:g}/100 (bar {QUALITY_BAR}) — {status}."
    counts = _counts(blockers)
    if counts:
        text += " " + "; ".join(
            f"{n} {_KIND_LABEL.get(kind, kind)}" for kind, n in sorted(counts.items())
        ) + "."
    if source != SOURCE_TOR:
        text += " " + SOURCE_NOTES[source]
    return text


def is_ready(sections: dict | None) -> bool:
    gate = (sections or {}).get("quality_gate") if isinstance(sections, dict) else None
    return isinstance(gate, dict) and gate.get("status") == READY


def enforce(sections: dict, analysis: dict | None, repair=None, max_rounds: int = MAX_REPAIR_ROUNDS) -> dict:
    """Score, repair what failed, and re-score, at most ``max_rounds`` times.

    ``repair(sections, gate) -> sections`` rewrites the failing sections and
    re-grounds claims. Without it the first score stands. The final gate is
    stored on ``sections['quality_gate']`` with every round's score.
    """
    gate = score_draft(sections, analysis)
    history = [gate["score"]]
    rounds = 0
    while gate["status"] != READY and repair is not None and rounds < max_rounds:
        rounds += 1
        sections = repair(sections, gate)
        gate = score_draft(sections, analysis)
        history.append(gate["score"])
    gate["repair_rounds"] = rounds
    gate["score_history"] = history
    sections["quality_gate"] = gate
    return sections


def sections_to_repair(gate: dict) -> dict[str, dict]:
    """Per section: blockers to remove and criteria to answer."""
    plan: dict[str, dict] = {}
    for b in gate.get("blockers") or []:
        if b.get("section"):
            plan.setdefault(b["section"], {"blockers": [], "criteria": []})["blockers"].append(b)
    for row in gate.get("rows") or []:
        if row.get("earned", 0) < 1 and row.get("section"):
            plan.setdefault(row["section"], {"blockers": [], "criteria": []})["criteria"].append(row)
    return plan
