"""Canonical client/donor matching and observed-record roll-up (Phase 2).

Deterministic normalize + exact/fuzzy matching. No LLM. Roll-up counts are
code aggregations of stored rows. See docs/adr/006-client-organizations.md.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence

from utils.untrusted import wrap_untrusted

STATUS_VERIFIED = "VERIFIED"
STATUS_INFERRED = "INFERRED"
STATUS_UNKNOWN = "UNKNOWN"

MAX_ORG_NAME_CHARS = 200
COMPACT_EXACT_MIN_CHARS = 6
FUZZY_MIN_COMPACT_LEN = 10
FUZZY_MAX_LEN_DIFF = 2
FUZZY_THRESHOLD = 0.95

PLACEHOLDER_NAMES = frozenset({
    "",
    "unknown",
    "unknown client",
    "unknown donor",
    "n/a",
    "na",
    "none",
    "null",
    "tbd",
    "not specified",
    "not stated",
    "not provided",
    "-",
    "--",
})

LEGAL_SUFFIXES = frozenset({
    "ltd",
    "limited",
    "inc",
    "incorporated",
    "llc",
    "plc",
    "gmbh",
    "bv",
    "sa",
    "pty",
    "co",
    "corp",
    "corporation",
})

_ALIAS_PATH = Path(__file__).with_name("organization_aliases.json")
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class OrgNameForms:
    raw: str
    sanitized: str
    spaced: str
    compact: str
    first_token: str
    usable: bool


@dataclass(frozen=True)
class OrgIndexEntry:
    organization_id: str
    canonical_name: str
    normalized_name: str
    normalized_compact: str
    alias_normalized: str = ""
    alias_compact: str = ""
    alias_source: str = "observed"
    entity_kind: str = "unknown"


@dataclass(frozen=True)
class MatchResult:
    status: str
    method: str
    confidence: float
    organization_id: str | None
    canonical_name: str
    is_new_candidate: bool
    query_raw: str
    query_sanitized: str
    query_spaced: str
    query_compact: str
    entity_kind: str = "unknown"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "method": self.method,
            "confidence": self.confidence,
            "organization_id": self.organization_id,
            "canonical_name": self.canonical_name,
            "is_new_candidate": self.is_new_candidate,
            "query_raw": self.query_raw,
            "query_sanitized": self.query_sanitized,
            "query_spaced": self.query_spaced,
            "query_compact": self.query_compact,
            "entity_kind": self.entity_kind,
        }


@dataclass(frozen=True)
class ObservedRecord:
    source_kind: str
    source_id: str
    title: str
    role: str
    observed_name: str
    outcome: str = "UNKNOWN"
    outcome_status: str = STATUS_UNKNOWN

    def identity(self) -> tuple[str, str, str]:
        return (self.source_kind, str(self.source_id), self.role)

    def as_dict(self) -> dict:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "title": self.title,
            "role": self.role,
            "observed_name": self.observed_name,
            "outcome": self.outcome,
            "outcome_status": self.outcome_status,
        }


def sanitize_org_name(raw) -> str:
    """Length-cap and strip control characters. Empty if nothing printable remains."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    cleaned = []
    for ch in raw:
        if ch in "\n\r\t":
            cleaned.append(" ")
            continue
        if not ch.isprintable():
            continue
        cleaned.append(ch)
    text = _SPACE_RE.sub(" ", "".join(cleaned)).strip()
    if len(text) > MAX_ORG_NAME_CHARS:
        text = text[:MAX_ORG_NAME_CHARS].rstrip()
    return text


def org_name_for_prompt(raw) -> str:
    """Document-derived org names are untrusted wherever they enter an LLM prompt."""
    return wrap_untrusted(sanitize_org_name(raw))


def normalize_org_name(raw) -> OrgNameForms:
    sanitized = sanitize_org_name(raw)
    if not sanitized:
        return OrgNameForms(
            raw="" if raw is None else str(raw),
            sanitized="",
            spaced="",
            compact="",
            first_token="",
            usable=False,
        )
    nfkc = unicodedata.normalize("NFKC", sanitized).casefold()
    spaced = _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", nfkc)).strip()
    tokens = [t for t in spaced.split(" ") if t]
    stripped = list(tokens)
    while len(stripped) > 1 and stripped[-1] in LEGAL_SUFFIXES:
        stripped.pop()
    if stripped:
        tokens = stripped
        spaced = " ".join(tokens)
    compact = "".join(tokens)
    first = tokens[0] if tokens else ""
    placeholder = spaced in PLACEHOLDER_NAMES or compact in PLACEHOLDER_NAMES
    return OrgNameForms(
        raw="" if raw is None else str(raw)[: MAX_ORG_NAME_CHARS * 2],
        sanitized=sanitized,
        spaced=spaced,
        compact=compact,
        first_token=first,
        usable=bool(spaced) and not placeholder,
    )


def load_explicit_aliases(path: Path | None = None) -> list[dict]:
    """Human-checked alias pairs. Production file is empty on purpose."""
    target = path or _ALIAS_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    out = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        canonical = sanitize_org_name(row.get("canonical") or row.get("name"))
        alias = sanitize_org_name(row.get("alias"))
        if canonical and alias:
            out.append({"canonical": canonical, "alias": alias})
    return out


def explicit_alias_entries(
    aliases: Sequence[dict] | None = None,
) -> list[OrgIndexEntry]:
    """Build index rows for explicit aliases. Each pair is one org identity."""
    rows = list(aliases) if aliases is not None else load_explicit_aliases()
    grouped: dict[str, dict] = {}
    for row in rows:
        canonical = sanitize_org_name(row.get("canonical"))
        alias = sanitize_org_name(row.get("alias"))
        if not canonical or not alias:
            continue
        forms = normalize_org_name(canonical)
        if not forms.usable:
            continue
        key = forms.spaced
        bucket = grouped.setdefault(
            key,
            {
                "organization_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"org-alias:{key}")),
                "canonical_name": canonical,
                "normalized_name": forms.spaced,
                "normalized_compact": forms.compact,
                "aliases": [],
            },
        )
        bucket["aliases"].append(alias)
    entries: list[OrgIndexEntry] = []
    for bucket in grouped.values():
        entries.append(
            OrgIndexEntry(
                organization_id=bucket["organization_id"],
                canonical_name=bucket["canonical_name"],
                normalized_name=bucket["normalized_name"],
                normalized_compact=bucket["normalized_compact"],
                alias_normalized=bucket["normalized_name"],
                alias_compact=bucket["normalized_compact"],
                alias_source="explicit",
            )
        )
        for alias in bucket["aliases"]:
            alias_forms = normalize_org_name(alias)
            if not alias_forms.usable:
                continue
            entries.append(
                OrgIndexEntry(
                    organization_id=bucket["organization_id"],
                    canonical_name=bucket["canonical_name"],
                    normalized_name=bucket["normalized_name"],
                    normalized_compact=bucket["normalized_compact"],
                    alias_normalized=alias_forms.spaced,
                    alias_compact=alias_forms.compact,
                    alias_source="explicit",
                )
            )
    return entries


def _empty_match(raw, forms: OrgNameForms) -> MatchResult:
    return MatchResult(
        status=STATUS_UNKNOWN,
        method="empty",
        confidence=0.0,
        organization_id=None,
        canonical_name="",
        is_new_candidate=False,
        query_raw="" if raw is None else str(raw)[: MAX_ORG_NAME_CHARS * 2],
        query_sanitized=forms.sanitized,
        query_spaced=forms.spaced,
        query_compact=forms.compact,
    )


def _candidate_match(raw, forms: OrgNameForms) -> MatchResult:
    org_id = str(uuid.uuid4())
    return MatchResult(
        status=STATUS_UNKNOWN,
        method="new_candidate",
        confidence=0.0,
        organization_id=org_id,
        canonical_name=forms.sanitized,
        is_new_candidate=True,
        query_raw="" if raw is None else str(raw)[: MAX_ORG_NAME_CHARS * 2],
        query_sanitized=forms.sanitized,
        query_spaced=forms.spaced,
        query_compact=forms.compact,
    )


def _hit(
    entry: OrgIndexEntry,
    *,
    status: str,
    method: str,
    confidence: float,
    raw,
    forms: OrgNameForms,
) -> MatchResult:
    return MatchResult(
        status=status,
        method=method,
        confidence=round(float(confidence), 4),
        organization_id=entry.organization_id,
        canonical_name=entry.canonical_name,
        is_new_candidate=False,
        query_raw="" if raw is None else str(raw)[: MAX_ORG_NAME_CHARS * 2],
        query_sanitized=forms.sanitized,
        query_spaced=forms.spaced,
        query_compact=forms.compact,
        entity_kind=entry.entity_kind,
    )


def match_organization(
    raw,
    index: Sequence[OrgIndexEntry] | None = None,
) -> MatchResult:
    """Resolve one extracted name against a normalized index. Never force-merges."""
    forms = normalize_org_name(raw)
    if not forms.usable:
        return _empty_match(raw, forms)

    entries = list(index or ())
    exact_spaced = None
    exact_compact = None
    exact_alias = None
    best_fuzzy: tuple[float, OrgIndexEntry] | None = None

    for entry in entries:
        alias_spaced = entry.alias_normalized or entry.normalized_name
        alias_compact = entry.alias_compact or entry.normalized_compact
        if forms.spaced and forms.spaced == alias_spaced:
            if entry.alias_source == "explicit" and exact_alias is None:
                exact_alias = entry
            elif exact_spaced is None:
                exact_spaced = entry
            continue
        if (
            forms.compact
            and len(forms.compact) >= COMPACT_EXACT_MIN_CHARS
            and forms.compact == alias_compact
        ):
            if entry.alias_source == "explicit" and exact_alias is None:
                exact_alias = entry
            elif exact_compact is None:
                exact_compact = entry
            continue
        if (
            len(forms.compact) >= FUZZY_MIN_COMPACT_LEN
            and len(alias_compact) >= FUZZY_MIN_COMPACT_LEN
            and abs(len(forms.compact) - len(alias_compact)) <= FUZZY_MAX_LEN_DIFF
            and forms.first_token
            and forms.first_token == (alias_spaced.split(" ") or [""])[0]
        ):
            ratio = SequenceMatcher(None, forms.compact, alias_compact).ratio()
            if ratio >= FUZZY_THRESHOLD and (
                best_fuzzy is None or ratio > best_fuzzy[0]
            ):
                best_fuzzy = (ratio, entry)

    if exact_alias is not None:
        return _hit(
            exact_alias,
            status=STATUS_VERIFIED,
            method="explicit_alias",
            confidence=1.0,
            raw=raw,
            forms=forms,
        )
    if exact_spaced is not None:
        return _hit(
            exact_spaced,
            status=STATUS_VERIFIED,
            method="exact_spaced",
            confidence=1.0,
            raw=raw,
            forms=forms,
        )
    if exact_compact is not None:
        return _hit(
            exact_compact,
            status=STATUS_VERIFIED,
            method="exact_compact",
            confidence=1.0,
            raw=raw,
            forms=forms,
        )
    if best_fuzzy is not None:
        return _hit(
            best_fuzzy[1],
            status=STATUS_INFERRED,
            method="fuzzy",
            confidence=best_fuzzy[0],
            raw=raw,
            forms=forms,
        )
    return _candidate_match(raw, forms)


def entry_from_match(match: MatchResult, *, entity_kind: str = "unknown") -> OrgIndexEntry | None:
    if not match.organization_id or not match.query_spaced:
        return None
    return OrgIndexEntry(
        organization_id=match.organization_id,
        canonical_name=match.canonical_name or match.query_sanitized,
        normalized_name=match.query_spaced,
        normalized_compact=match.query_compact,
        alias_normalized=match.query_spaced,
        alias_compact=match.query_compact,
        alias_source="observed",
        entity_kind=entity_kind,
    )


def index_with_match(
    index: Sequence[OrgIndexEntry],
    match: MatchResult,
    *,
    entity_kind: str = "unknown",
) -> list[OrgIndexEntry]:
    """Return a new index that includes this match's spelling."""
    out = list(index)
    entry = entry_from_match(match, entity_kind=entity_kind)
    if entry is None:
        return out
    for existing in out:
        if (
            existing.organization_id == entry.organization_id
            and existing.alias_normalized == entry.alias_normalized
        ):
            return out
    out.append(entry)
    return out


def normalize_outcome(raw, *, won_flag=None) -> tuple[str, str]:
    """Map stored outcome fields. Unchecked won checkbox is UNKNOWN, not Lost."""
    if isinstance(raw, str):
        token = raw.strip().casefold()
        if token in {"won", "win", "awarded"}:
            return "WON", STATUS_VERIFIED
        if token in {"lost", "loss", "not awarded", "unsuccessful"}:
            return "LOST", STATUS_VERIFIED
    if won_flag is True:
        return "WON", STATUS_VERIFIED
    return "UNKNOWN", STATUS_UNKNOWN


def _dedupe_records(records: Iterable[ObservedRecord]) -> list[ObservedRecord]:
    seen: dict[tuple[str, str, str], ObservedRecord] = {}
    for rec in records:
        if not isinstance(rec, ObservedRecord):
            continue
        key = rec.identity()
        prior = seen.get(key)
        if prior is None:
            seen[key] = rec
            continue
        rank = {"WON": 2, "LOST": 2, "UNKNOWN": 0}
        if rank.get(rec.outcome, 0) > rank.get(prior.outcome, 0):
            seen[key] = rec
        elif (
            rec.outcome == prior.outcome
            and rec.outcome_status == STATUS_VERIFIED
            and prior.outcome_status != STATUS_VERIFIED
        ):
            seen[key] = rec
    return list(seen.values())


def rollup_organization(
    match: MatchResult,
    records: Sequence[ObservedRecord] | None = None,
    *,
    current_source_id: str | None = None,
    current_source_kind: str = "opportunity",
    role: str = "client",
    known_client_factor: dict | None = None,
) -> dict:
    """Code counts from stored rows. UNKNOWN outcomes stay UNKNOWN."""
    current_id = str(current_source_id or "").strip()
    past = []
    for rec in _dedupe_records(records or ()):
        if current_id and rec.source_id == current_id and rec.source_kind == current_source_kind:
            continue
        past.append(rec)

    won = [r for r in past if r.outcome == "WON" and r.outcome_status == STATUS_VERIFIED]
    lost = [r for r in past if r.outcome == "LOST" and r.outcome_status == STATUS_VERIFIED]
    unknown = [r for r in past if r not in won and r not in lost]
    n = len(past)
    citations = [r.as_dict() for r in past[:12]]

    if match.method == "empty":
        headline = (
            "No client name was extracted. Client organization is UNKNOWN. "
            "Cortech has bid on 0 opportunities from this client before because "
            "there is no client to match."
            if role == "client"
            else (
                "No distinct donor name was extracted. Donor organization is UNKNOWN."
            )
        )
        outcome_note = "Outcomes are UNKNOWN — they are not treated as wins or as a 0% win rate."
    elif n == 0:
        label = "this client" if role == "client" else "this donor"
        headline = (
            f"Cortech has bid on 0 opportunities from {label} before "
            "(no matching past opportunity or proposal records in the observed store)."
        )
        outcome_note = (
            "There are no stored outcomes to count. Missing outcomes are UNKNOWN, "
            "not zeros dressed as a win rate."
        )
    else:
        label = "this client" if role == "client" else "this donor"
        headline = (
            f"Cortech has bid on {n} opportunities from {label} before, "
            f"won {len(won)}, lost {len(lost)}."
        )
        if unknown:
            outcome_note = (
                f"Won/Lost is UNKNOWN for {len(unknown)} of those records — "
                "unknown outcomes are not treated as losses or as a 0% win rate."
            )
        else:
            outcome_note = (
                "Won/Lost counts are from stored outcome rows only (win/loss memory "
                "or an explicit won=true proposal flag)."
            )

    overlap_note = ""
    overlap_hits: list[str] = []
    if isinstance(known_client_factor, dict):
        overlap_note = str(known_client_factor.get("evidence") or "")
        hits = known_client_factor.get("hits")
        if isinstance(hits, list):
            overlap_hits = [str(h) for h in hits if h]

    return {
        "role": role,
        "match": match.as_dict(),
        "opportunities_before": n,
        "won": len(won),
        "lost": len(lost),
        "unknown_outcomes": len(unknown),
        "headline": headline,
        "outcome_note": outcome_note,
        "citations": citations,
        "known_client_overlap": overlap_note,
        "known_client_hits": overlap_hits,
        "counts_are": "code aggregation of stored rows, not an LLM estimate",
    }


def empty_client_intelligence() -> dict:
    empty = match_organization("")
    return {
        "client": rollup_organization(empty, [], role="client"),
        "donor": None,
        "same_org": False,
        "storage": "unavailable",
    }


def known_client_factor_from_analysis(analysis: dict | None) -> dict | None:
    if not isinstance(analysis, dict):
        return None
    intel = analysis.get("bid_intelligence")
    if not isinstance(intel, dict):
        return None
    for factor in intel.get("factor_evidence") or []:
        if isinstance(factor, dict) and factor.get("name") == "known_client":
            return factor
    return None


def _filter_records_for_match(
    match: MatchResult,
    records: Sequence[ObservedRecord],
    index: Sequence[OrgIndexEntry],
) -> list[ObservedRecord]:
    if not match.organization_id:
        return []
    kept = []
    for rec in records:
        rec_match = match_organization(rec.observed_name, index)
        if rec_match.organization_id == match.organization_id and not rec_match.is_new_candidate:
            kept.append(rec)
    return kept


def build_client_intelligence(
    *,
    client: str = "",
    donor: str = "",
    title: str = "",
    opportunity_id: str = "",
    known_client_factor: dict | None = None,
    index: Sequence[OrgIndexEntry] | None = None,
    observed: Sequence[ObservedRecord] | None = None,
    persist: bool = True,
    fetch_stored: bool = True,
    explicit_aliases: Sequence[dict] | None = None,
) -> dict:
    """Match client (and donor if distinct) and roll up observed history.

    IO is fail-open: missing tables/config become an empty index. Tests can pass
    ``index`` / ``observed`` and set ``fetch_stored=False`` / ``persist=False``.
    """
    working_index: list[OrgIndexEntry] = list(index) if index is not None else []
    storage = "injected" if index is not None else "unavailable"
    org_store = None
    needs_store = persist or (fetch_stored and index is None) or fetch_stored
    if needs_store:
        from database import organizations as org_store

    if index is None:
        working_index.extend(explicit_alias_entries(explicit_aliases))
        if fetch_stored and org_store is not None:
            try:
                loaded, storage = org_store.load_organization_index()
                working_index.extend(loaded)
            except Exception:
                storage = "unavailable"
    elif explicit_aliases:
        working_index.extend(explicit_alias_entries(explicit_aliases))

    stored_records: list[ObservedRecord] = list(observed or ())

    client_match = match_organization(client, working_index)
    working_index = index_with_match(working_index, client_match, entity_kind="client")

    donor_forms = normalize_org_name(donor)
    donor_match = None
    same_org = False
    if donor_forms.usable:
        donor_match = match_organization(donor, working_index)
        if (
            client_match.organization_id
            and donor_match.organization_id == client_match.organization_id
            and donor_match.method != "empty"
        ):
            same_org = True
        else:
            working_index = index_with_match(
                working_index, donor_match, entity_kind="donor"
            )

    if persist and org_store is not None:
        try:
            client_match = org_store.persist_match(client_match, entity_kind="client")
            if donor_match is not None and not same_org:
                donor_match = org_store.persist_match(donor_match, entity_kind="donor")
            if opportunity_id and client_match.organization_id:
                org_store.persist_observation(
                    client_match,
                    ObservedRecord(
                        source_kind="opportunity",
                        source_id=str(opportunity_id),
                        title=sanitize_org_name(title)[:200],
                        role="client",
                        observed_name=client_match.query_sanitized or sanitize_org_name(client),
                        outcome="UNKNOWN",
                        outcome_status=STATUS_UNKNOWN,
                    ),
                )
            if (
                opportunity_id
                and donor_match is not None
                and donor_match.organization_id
                and not same_org
            ):
                org_store.persist_observation(
                    donor_match,
                    ObservedRecord(
                        source_kind="opportunity",
                        source_id=str(opportunity_id),
                        title=sanitize_org_name(title)[:200],
                        role="donor",
                        observed_name=donor_match.query_sanitized or sanitize_org_name(donor),
                        outcome="UNKNOWN",
                        outcome_status=STATUS_UNKNOWN,
                    ),
                )
        except Exception:
            pass

    if fetch_stored and org_store is not None and client_match.organization_id:
        try:
            stored_records.extend(
                org_store.load_records_for_organization(client_match.organization_id)
            )
            stored_records.extend(
                org_store.collect_matching_store_records(client_match, working_index)
            )
        except Exception:
            pass
    if (
        fetch_stored
        and org_store is not None
        and donor_match is not None
        and donor_match.organization_id
        and not same_org
    ):
        try:
            stored_records.extend(
                org_store.load_records_for_organization(donor_match.organization_id)
            )
            stored_records.extend(
                org_store.collect_matching_store_records(donor_match, working_index)
            )
        except Exception:
            pass

    client_records: list[ObservedRecord] = []
    if client_match.organization_id:
        client_records = _filter_records_for_match(
            client_match, stored_records, working_index
        )

    client_rollup = rollup_organization(
        client_match,
        client_records,
        current_source_id=opportunity_id,
        role="client",
        known_client_factor=known_client_factor,
    )

    donor_payload = None
    if donor_match is not None and not same_org:
        donor_records = _filter_records_for_match(
            donor_match, stored_records, working_index
        )
        donor_payload = rollup_organization(
            donor_match,
            donor_records,
            current_source_id=opportunity_id,
            role="donor",
            known_client_factor=None,
        )
    elif same_org:
        donor_payload = {
            "role": "donor",
            "match": donor_match.as_dict() if donor_match else None,
            "same_organization_as_client": True,
            "headline": (
                "Donor name resolved to the same organization as the client "
                "(matcher identity, not a guessed relationship)."
            ),
            "outcome_note": "",
            "citations": [],
            "opportunities_before": client_rollup["opportunities_before"],
            "won": client_rollup["won"],
            "lost": client_rollup["lost"],
            "unknown_outcomes": client_rollup["unknown_outcomes"],
        }

    return {
        "client": client_rollup,
        "donor": donor_payload,
        "same_org": same_org,
        "storage": storage,
    }


def reviewer_sentences(payload: dict | None) -> list[str]:
    """Plain-language lines for the review email / checklist."""
    if not isinstance(payload, dict):
        return []
    lines: list[str] = []
    client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
    match = client.get("match") if isinstance(client.get("match"), dict) else {}
    name = match.get("canonical_name") or match.get("query_sanitized") or ""
    if name:
        method = match.get("method") or "unknown"
        status = match.get("status") or STATUS_UNKNOWN
        lines.append(f"{name} — match: {method} ({status})")
    if client.get("headline"):
        lines.append(str(client["headline"]))
    if client.get("outcome_note"):
        lines.append(str(client["outcome_note"]))
    overlap = client.get("known_client_overlap")
    if overlap:
        lines.append(str(overlap))
    for cite in (client.get("citations") or [])[:8]:
        if not isinstance(cite, dict):
            continue
        title = cite.get("title") or cite.get("source_id") or "untitled"
        lines.append(
            f"Cited: {title} ({cite.get('source_kind')} {cite.get('source_id')}) "
            f"— outcome {cite.get('outcome')}"
        )
    donor = payload.get("donor")
    if isinstance(donor, dict) and donor.get("headline"):
        lines.append(str(donor["headline"]))
        if donor.get("outcome_note"):
            lines.append(str(donor["outcome_note"]))
    return lines
