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
    "tender_brief",
    "win_strategy",
    "document_lock",
    "section_order",
    "omitted_financial",
    "submission_outline",
}

_PERSON_RE = re.compile(
    r"\b((?:Dr|Mr|Mrs|Ms|Prof)\.?\s+[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+|"
    r"[A-Z][a-z]+\s+[A-Z]\.\s+[A-Z][a-z]+|"
    r"[A-Z][a-z]+\s+[A-Z][a-z]+)\b"
)
_PERSON_CLAIM = re.compile(
    r"\b(holds an? |years of experience|lead consultant|team leader|"
    r"proposed (?:expert|team)|will be led by|cv of)\b",
    re.I,
)
_ORG_NOISE = re.compile(
    r"\b(international|foundation|children|committee|alliance|agency|"
    r"ministry|government|consulting|group|programme|program|project|"
    r"evaluation|endline|baseline)\b",
    re.I,
)
_EMPTY_SECTION = "[INSUFFICIENT EVIDENCE]"


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
    locations = opportunity.get("project_location") or []
    lots = opportunity.get("lots_or_sites") or []
    targets = opportunity.get("target_groups") or []
    if not isinstance(locations, list):
        locations = [locations] if locations else []
    if not isinstance(lots, list):
        lots = [lots] if lots else []
    if not isinstance(targets, list):
        targets = [targets] if targets else []
    if this_client or this_title:
        chunks.append(_chunk(
            "this_tender",
            "opportunity_analysis",
            this_title or "current tender",
            "opportunity",
            " ".join(
                str(x) for x in (
                    [this_title, this_client, this_donor]
                    + list(locations) + list(lots) + list(targets)
                ) if x
            ),
            {
                "client": this_client,
                "title": this_title,
                "this_tender": True,
                "locations": locations,
                "lots": lots,
                "targets": targets,
            },
        ))
    return chunks


def _profile_client_names(profile: str) -> list[str]:
    marker = "KEY CLIENTS"
    if marker not in (profile or ""):
        return []
    chunk = profile.split(marker, 1)[1]
    for stop in ("REGISTRATIONS:", "CERTIFICATIONS:", "COMPETITIVE STRENGTHS:"):
        if stop in chunk:
            chunk = chunk.split(stop, 1)[0]
    names = []
    for part in re.split(r"[\n,]", chunk):
        name = part.strip(" -:\t")
        if len(name) >= 4:
            names.append(name)
    return names


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
        if chunk["chunk_id"] == "profile:cortech":
            for client in _profile_client_names(chunk.get("text") or ""):
                add(client, cid)
        for loc in meta.get("locations") or []:
            add(str(loc), cid)
        for lot in meta.get("lots") or []:
            add(str(lot), cid)
        for group in meta.get("targets") or []:
            add(str(group), cid)
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


def _this_tender_norms(chunks: list[dict]) -> set[str]:
    norms: set[str] = set()
    for chunk in chunks:
        if chunk["chunk_id"] != "this_tender":
            continue
        meta = chunk.get("metadata") or {}
        for value in (
            [meta.get("client"), meta.get("title")]
            + list(meta.get("locations") or [])
            + list(meta.get("lots") or [])
            + list(meta.get("targets") or [])
        ):
            n = _norm(str(value or ""))
            if n:
                norms.add(n)
    return norms


def _supported_name(phrase: str, entity_map: dict[str, list[str]], blobs: dict[str, str]) -> bool:
    key = _norm(phrase)
    if len(key) < 4:
        return False
    if key in entity_map:
        return True
    return any(key in blob for blob in blobs.values() if blob)


def _unknown_proper_nouns(
    sentence: str,
    entity_map: dict[str, list[str]],
    blobs: dict[str, str],
    this_tender_norms: set[str],
) -> list[str]:
    unknown = []
    for match in _PROPER_NOUN.finditer(sentence):
        phrase = match.group(1)
        key = _norm(phrase)
        if key in _STOP_PHRASES or key in this_tender_norms:
            continue
        if _supported_name(phrase, entity_map, blobs):
            continue
        unknown.append(phrase)
    return unknown


def _unknown_people(
    sentence: str,
    entity_map: dict[str, list[str]],
    blobs: dict[str, str],
    this_tender_norms: set[str],
) -> list[str]:
    if not _PERSON_CLAIM.search(sentence):
        return []
    unknown = []
    for match in _PERSON_RE.finditer(sentence):
        phrase = match.group(1)
        key = _norm(phrase)
        if key in _STOP_PHRASES or key in this_tender_norms:
            continue
        if _ORG_NOISE.search(phrase):
            continue
        if _supported_name(phrase, entity_map, blobs):
            continue
        unknown.append(phrase)
    return unknown


def extract_claims(sections: dict, chunks: list[dict]) -> list[dict]:
    entity_map = _entity_map(chunks)
    blobs = _chunk_blob(chunks)
    this_norms = _this_tender_norms(chunks)

    claims: list[dict] = []
    for section, body in sections.items():
        if section in _SKIP_KEYS or not isinstance(body, str):
            continue
        for sentence in _sentences(body):
            if "[NOT VERIFIED]" in sentence or "[INSUFFICIENT EVIDENCE]" in sentence:
                continue
            past = _is_past_claim(sentence)
            hits = _longest_entity_hits(sentence, entity_map)
            supporting = [
                (ent, cid) for ent, cid in hits
                if cid != "this_tender" or not past
            ]
            unknown_orgs = (
                _unknown_proper_nouns(sentence, entity_map, blobs, this_norms)
                if past else []
            )
            unknown_people = _unknown_people(
                sentence, entity_map, blobs, this_norms
            )

            if (past and unknown_orgs) or unknown_people:
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


def redact_unverified_claims(sections: dict, claims: list[dict]) -> tuple[dict, list[dict]]:
    """Remove invented assertions from client-facing text.

    Tagging a false client or CV [NOT VERIFIED] still ships misinformation.
    Unsupported named claims are dropped. Empty sections become an explicit
    evidence gap rather than a plausible-sounding filler.
    """
    flagged = {
        (c["section"], c["sentence"])
        for c in claims
        if c["status"] == "NOT VERIFIED"
    }
    if not flagged:
        return sections, []
    out = dict(sections)
    removed: list[dict] = []
    for section, body in sections.items():
        if section in _SKIP_KEYS or not isinstance(body, str):
            continue
        kept = []
        for sentence in _sentences(body):
            key = (section, sentence[:400])
            if key in flagged or "[NOT VERIFIED]" in sentence:
                removed.append({"section": section, "sentence": sentence[:400]})
                continue
            kept.append(sentence)
        if not kept and body.strip():
            out[section] = _EMPTY_SECTION
        elif "|" in body:
            out[section] = "\n".join(kept) if kept else _EMPTY_SECTION
        else:
            out[section] = " ".join(kept)
    return out, removed


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
    """Attach claim_grounding and strip unverified assertions from the draft."""
    sections = sections or {}
    chunks = build_evidence_chunks(
        past_matches=past_matches,
        matched_team_result=matched_team_result,
        static_past_work=static_past_work,
        analysis=analysis,
    )
    claims = extract_claims(sections, chunks)
    redacted, removed = redact_unverified_claims(sections, claims)
    verified = sum(1 for c in claims if c["status"] == "VERIFIED")
    not_verified = sum(1 for c in claims if c["status"] == "NOT VERIFIED")
    insufficient = sum(1 for c in claims if c["status"] == "INSUFFICIENT EVIDENCE")
    redacted["claim_grounding"] = {
        "claims": claims,
        "verified": verified,
        "not_verified": not_verified,
        "insufficient_evidence": insufficient,
        "removed_unverified": removed,
        "chunk_count": len(chunks),
    }
    return redacted
