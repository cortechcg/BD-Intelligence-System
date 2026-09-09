"""Deterministic claim-to-chunk grounding for drafted proposal text.

LLM drafts are not evidence. A past-assignment or named-expert claim is
VERIFIED only when a retrieved chunk (proposal embedding, CV match, or
CORTECH_PROFILE) contains that entity. Otherwise: NOT VERIFIED or
INSUFFICIENT EVIDENCE. Current-tender facts (this client, this title)
are tagged this_tender — they are not past-work inventions.
"""

from __future__ import annotations

import re

from config import CORTECH_PROFILE
from utils.money_scrub import strip_monetary_amounts

STATUSES = ("VERIFIED", "NOT VERIFIED", "INSUFFICIENT EVIDENCE")

_PAST_MARKERS = re.compile(
    r"\b("
    r"previously|prior assignment|past assignment|commissioned|"
    r"track record|similar assignment|earlier (?:evaluation|assignment)|"
    r"has delivered|have delivered|worked (?:for|with)|assignment for|"
    r"completed assignment|relevant experience|previous assignment|"
    r"endline evaluation|midline evaluation|baseline evaluation|"
    r"was retained|were retained"
    r")\b",
    re.I,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_PROPER_NOUN = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4})\b")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_STOP_PHRASES = {
    "terms of reference",
    "technical proposal",
    "executive summary",
    "cover letter",
    "expression of interest",
    "east africa",
    "horn of africa",
    "sub saharan africa",
    "united kingdom",
    "south sudan",
    "cortech consulting",
    "cortech consulting group",
    "not verified",
    "insufficient evidence",
}

_SKIP_KEYS = {
    "quality_score",
    "lightweight",
    "lightweight_reason",
    "submission_type",
    "claim_grounding",
}


def _norm(text: str) -> str:
    return _NON_ALNUM.sub(" ", (text or "").lower()).strip()


def _sentences(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    parts = _SENTENCE_SPLIT.split(raw)
    return [p.strip() for p in parts if p.strip()]


def _chunk(
    chunk_id: str,
    source: str,
    document: str,
    section: str,
    text: str,
    metadata: dict | None = None,
) -> dict:
    cleaned, _ = strip_monetary_amounts(text or "")
    return {
        "chunk_id": chunk_id,
        "source": source,
        "document": document,
        "section": section,
        "text": cleaned,
        "metadata": metadata or {},
    }


def build_evidence_chunks(
    *,
    past_matches: list[dict] | None = None,
    matched_team_result: dict | None = None,
    static_past_work: str = "",
    analysis: dict | None = None,
) -> list[dict]:
    chunks: list[dict] = []
    profile, _ = strip_monetary_amounts(CORTECH_PROFILE)
    chunks.append(_chunk(
        "profile:cortech",
        "CORTECH_PROFILE",
        "config.CORTECH_PROFILE",
        "profile",
        profile,
    ))
    if static_past_work.strip():
        chunks.append(_chunk(
            "static:CORTECH_PAST_WORK",
            "CORTECH_PAST_WORK",
            "intelligence.proposal_writer.CORTECH_PAST_WORK",
            "static_fallback",
            static_past_work,
        ))

    for i, match in enumerate(past_matches or []):
        if not isinstance(match, dict):
            continue
        meta = match.get("metadata") if isinstance(match.get("metadata"), dict) else {}
        title = str(match.get("project_title") or "").strip()
        lookup = (
            match.get("airtable_proposal_id")
            or match.get("id")
            or f"idx-{i}"
        )
        blob = " ".join([
            title,
            str(meta.get("client") or ""),
            str(meta.get("year") or ""),
            " ".join(str(x) for x in (meta.get("location") or []) if x),
            str(match.get("content_chunk") or ""),
        ])
        chunks.append(_chunk(
            f"proposal:{lookup}",
            "proposal_embeddings",
            title or f"past-proposal-{i}",
            "content_chunk",
            blob,
            {
                "project_title": title,
                "client": meta.get("client") or "",
                "year": meta.get("year") or "",
                "similarity": match.get("similarity"),
            },
        ))

    team = (matched_team_result or {}).get("matched_team") or {}
    if isinstance(team, dict):
        for role, person in team.items():
            if not isinstance(person, dict):
                continue
            name = str(person.get("consultant_name") or "").strip()
            if not name or name == "EXTERNAL RECRUITMENT NEEDED":
                continue
            cap = person.get("capability") if isinstance(person.get("capability"), dict) else {}
            why = " ".join(str(x) for x in (cap.get("why") or []) if x)
            evidence = " ".join(str(x) for x in (cap.get("evidence") or []) if x)
            cid = person.get("airtable_consultant_id") or name
            chunks.append(_chunk(
                f"cv:{cid}",
                "cv_match",
                name,
                str(role),
                f"{name} {role} {why} {evidence}",
                {"consultant_name": name, "role": role},
            ))

    opportunity = (analysis or {}).get("opportunity")
    if not isinstance(opportunity, dict):
        opportunity = {}
    this_client = str(opportunity.get("client") or "").strip()
    this_title = str(opportunity.get("title") or "").strip()
    this_donor = str(opportunity.get("donor") or "").strip()
    if this_client or this_title:
        chunks.append(_chunk(
            "this_tender",
            "opportunity_analysis",
            this_title or "current tender",
            "opportunity",
            f"{this_title} {this_client} {this_donor}",
            {"client": this_client, "title": this_title, "this_tender": True},
        ))
    return chunks


def _entity_map(chunks: list[dict]) -> dict[str, list[str]]:
    """Normalized entity phrase → chunk_ids that support it."""
    index: dict[str, list[str]] = {}

    def add(phrase: str, chunk_id: str) -> None:
        key = _norm(phrase)
        if len(key) < 4 or key in _STOP_PHRASES:
            return
        ids = index.setdefault(key, [])
        if chunk_id not in ids:
            ids.append(chunk_id)

    for chunk in chunks:
        cid = chunk["chunk_id"]
        meta = chunk.get("metadata") or {}
        add(str(meta.get("project_title") or ""), cid)
        add(str(meta.get("client") or ""), cid)
        add(str(meta.get("consultant_name") or ""), cid)
        add(str(meta.get("title") or ""), cid)
        # Significant stretches from the chunk text (proper nouns already
        # indexed via metadata). Also index the full normalized text for
        # substring containment checks later.
        add(chunk.get("document") or "", cid)
    return index


def _chunk_blob(chunks: list[dict]) -> dict[str, str]:
    return {c["chunk_id"]: _norm(c.get("text") or "") for c in chunks}


def _is_past_claim(sentence: str) -> bool:
    return bool(_PAST_MARKERS.search(sentence))


def _longest_entity_hits(sentence: str, entity_map: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Return (entity_norm, chunk_id) hits, longest entity first."""
    hay = _norm(sentence)
    hits = []
    for entity in sorted(entity_map, key=len, reverse=True):
        if len(entity) < 4:
            continue
        if entity in hay:
            hits.append((entity, entity_map[entity][0]))
    return hits


def _unknown_proper_nouns(sentence: str, entity_map: dict[str, list[str]], this_tender_norms: set[str]) -> list[str]:
    unknown = []
    for match in _PROPER_NOUN.finditer(sentence):
        phrase = match.group(1)
        key = _norm(phrase)
        if key in _STOP_PHRASES or key in this_tender_norms:
            continue
        if key in entity_map:
            continue
        # Already covered by a longer indexed entity
        if any(key in ent or ent in key for ent in entity_map if len(ent) >= len(key)):
            if any(key in ent for ent in entity_map):
                continue
        unknown.append(phrase)
    return unknown


def extract_claims(sections: dict, chunks: list[dict]) -> list[dict]:
    entity_map = _entity_map(chunks)
    blobs = _chunk_blob(chunks)
    this_tender_norms = set()
    for chunk in chunks:
        if chunk["chunk_id"] == "this_tender":
            meta = chunk.get("metadata") or {}
            for value in (meta.get("client"), meta.get("title")):
                n = _norm(str(value or ""))
                if n:
                    this_tender_norms.add(n)

    claims: list[dict] = []
    for section, body in sections.items():
        if section in _SKIP_KEYS or not isinstance(body, str):
            continue
        for sentence in _sentences(body):
            if "[NOT VERIFIED]" in sentence or "[INSUFFICIENT EVIDENCE]" in sentence:
                continue
            past = _is_past_claim(sentence)
            hits = _longest_entity_hits(sentence, entity_map)
            # Hits only on this_tender do not verify a *past* assignment claim
            supporting = [
                (ent, cid) for ent, cid in hits
                if cid != "this_tender" or not past
            ]
            unknown = _unknown_proper_nouns(sentence, entity_map, this_tender_norms) if past else []

            if past and unknown:
                status = "NOT VERIFIED"
                chunk_id = ""
                inference = "UNKNOWN"
            elif past and supporting:
                status = "VERIFIED"
                chunk_id = supporting[0][1]
                inference = "VERIFIED"
            elif past and not supporting:
                status = "INSUFFICIENT EVIDENCE"
                chunk_id = ""
                inference = "UNKNOWN"
            elif supporting and blobs.get(supporting[0][1]) and supporting[0][0] in (blobs.get(supporting[0][1]) or ""):
                # Named expert / known assignment mentioned without past-work
                # marker — still attach the chunk, do not nag.
                continue
            else:
                continue

            claims.append({
                "section": section,
                "sentence": sentence[:400],
                "status": status,
                "chunk_id": chunk_id,
                "source": next(
                    (c["source"] for c in chunks if c["chunk_id"] == chunk_id),
                    "",
                ),
                "inference_level": inference,
            })
    return claims


def annotate_unverified(sections: dict, claims: list[dict]) -> dict:
    """Append [NOT VERIFIED] to sentences that cite unknown past clients."""
    flagged = {
        (c["section"], c["sentence"])
        for c in claims
        if c["status"] == "NOT VERIFIED"
    }
    if not flagged:
        return sections
    out = dict(sections)
    for section, body in sections.items():
        if section in _SKIP_KEYS or not isinstance(body, str):
            continue
        rewritten = []
        for sentence in _sentences(body):
            key = (section, sentence[:400])
            if key in flagged and "[NOT VERIFIED]" not in sentence:
                core = sentence.rstrip()
                if core[-1:] in ".!?":
                    core = core[:-1].rstrip() + " [NOT VERIFIED]" + sentence.rstrip()[-1]
                else:
                    core = core + " [NOT VERIFIED]"
                rewritten.append(core)
            else:
                rewritten.append(sentence)
        out[section] = " ".join(rewritten)
    return out


def ground_sections(
    sections: dict,
    analysis: dict | None,
    matched_team_result: dict | None,
    *,
    past_matches: list[dict] | None = None,
    static_past_work: str = "",
) -> dict:
    """Attach claim_grounding and annotate unverified past-work sentences."""
    sections = sections or {}
    chunks = build_evidence_chunks(
        past_matches=past_matches,
        matched_team_result=matched_team_result,
        static_past_work=static_past_work,
        analysis=analysis,
    )
    claims = extract_claims(sections, chunks)
    annotated = annotate_unverified(sections, claims)
    verified = sum(1 for c in claims if c["status"] == "VERIFIED")
    not_verified = sum(1 for c in claims if c["status"] == "NOT VERIFIED")
    insufficient = sum(1 for c in claims if c["status"] == "INSUFFICIENT EVIDENCE")
    annotated["claim_grounding"] = {
        "claims": claims,
        "verified": verified,
        "not_verified": not_verified,
        "insufficient_evidence": insufficient,
        "chunk_count": len(chunks),
    }
    return annotated
