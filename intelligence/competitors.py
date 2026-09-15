"""Assortis award-winner extraction (Phase 5).

Only labelled Awarded Firm(s) / equivalent fields on award-notice text.
Never inferred from silence. See docs/adr/009-competitor-relationship-intelligence.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from intelligence.organizations import (
    MatchResult,
    match_organization,
    sanitize_org_name,
)
from utils.untrusted import wrap_untrusted

STATUS_VERIFIED = "VERIFIED"
EVIDENCE_VERIFIED = "VERIFIED"

MAX_PAGE_CHARS = 120_000
MAX_EXCERPT_CHARS = 500
MAX_AWARDS_PER_RUN = 15

# Labelled winner fields actually used on Assortis contract pages
# (and conservative synonyms). A name not under a label is not stored.
_WINNER_LABEL = re.compile(
    r"(?:awarded\s+firm(?:s|\(\s*s\s*\))?|awarded\s+to|"
    r"successful\s+bidder(?:s|\(\s*s\s*\))?|"
    r"winning\s+(?:firm|consultant|contractor)(?:s)?)\s*:",
    re.I,
)
_STOP_LABEL = re.compile(
    r"^(?:final\s+evaluation\s+price|signed\s+contract\s+price|"
    r"type\b|date\s+published|there\s+are\s+no\s+documents|"
    r"contact\b|avenue\s+louise|minimum\s+qualifying\s+score|"
    r"duration\s+of\s+contract|notice\s+version)\b",
    re.I,
)
_ASSORTIS_ID = re.compile(r"\s*\(\d{4,}\)\s*$")
_COUNTRY_LINE = re.compile(r"^country\s*:", re.I)
_ADDRESS_LINE = re.compile(
    r"^(?:\d+|p\.?\s*o\.?\s*box\b)|"
    r"\b(?:avenue|street|rua|road|strada|oeiras|lisboa|brussels)\b",
    re.I,
)


@dataclass(frozen=True)
class AwardFact:
    observed_name: str
    excerpt: str
    source_url: str
    opportunity_title: str = ""
    evidence_status: str = EVIDENCE_VERIFIED
    match: MatchResult | None = None

    def as_dict(self) -> dict:
        payload = {
            "observed_name": self.observed_name,
            "excerpt": self.excerpt,
            "source_url": self.source_url,
            "opportunity_title": self.opportunity_title,
            "evidence_status": self.evidence_status,
            "citation": self.source_url,
        }
        if self.match is not None:
            payload["match"] = self.match.as_dict()
        return payload


def wrap_award_page(page_text: str) -> str:
    """Award HTML/text is untrusted data (ADR 002)."""
    return wrap_untrusted((page_text or "")[:MAX_PAGE_CHARS])


def _norm_contains(needle: str, haystack: str) -> bool:
    n = re.sub(r"[^a-z0-9]+", " ", (needle or "").casefold()).strip()
    h = re.sub(r"[^a-z0-9]+", " ", (haystack or "").casefold()).strip()
    return bool(n) and n in h


def cited_award_or_none(
    name: str,
    excerpt: str,
    source_url: str,
    *,
    opportunity_title: str = "",
) -> AwardFact | None:
    """Code gate: no URL, no excerpt, or name not in excerpt → nothing stored."""
    cleaned = sanitize_org_name(name)
    url = (source_url or "").strip()
    snippet = " ".join((excerpt or "").split())
    if len(snippet) > MAX_EXCERPT_CHARS:
        snippet = snippet[:MAX_EXCERPT_CHARS].rstrip()
    if not cleaned or not url or not snippet:
        return None
    if not _norm_contains(cleaned, snippet):
        return None
    return AwardFact(
        observed_name=cleaned,
        excerpt=snippet,
        source_url=url[:2000],
        opportunity_title=sanitize_org_name(opportunity_title)[:200],
        evidence_status=EVIDENCE_VERIFIED,
    )


def _is_skip_line(line: str) -> bool:
    text = (line or "").strip()
    if not text or len(text) < 4:
        return True
    if _COUNTRY_LINE.match(text):
        return True
    if _STOP_LABEL.match(text):
        return True
    if _ADDRESS_LINE.search(text) and not re.search(
        r"\b(?:ltd|limited|sa|gmbh|llc|consult|associat|group|bv)\b", text, re.I
    ):
        return True
    return False


def _firm_from_line(line: str) -> str:
    text = _ASSORTIS_ID.sub("", (line or "").strip()).strip(" ,;|-")
    text = sanitize_org_name(text)
    if not text or _is_skip_line(text):
        return ""
    lowered = text.casefold()
    if lowered.startswith("awarded firm"):
        return ""
    if "ignore previous instructions" in lowered:
        return ""
    return text


def extract_awarded_firms(
    page_text: str,
    *,
    source_url: str,
    title: str = "",
) -> list[AwardFact]:
    """Pull winner names only from labelled award fields present on the page."""
    wrap_award_page(page_text)  # ingest boundary; extraction stays on the page text
    raw = (page_text or "")[:MAX_PAGE_CHARS]
    if not raw.strip() or not (source_url or "").strip():
        return []

    match = _WINNER_LABEL.search(raw)
    if match is None:
        return []

    rest = raw[match.end() :]
    lines: list[str] = []
    for line in rest.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                # blank line after at least one firm: keep scanning a little
                continue
            continue
        if _STOP_LABEL.match(stripped):
            break
        lines.append(stripped)
        if len(lines) > 40:
            break
    if not lines:
        # Same-line "Awarded Firm(s): NAME"
        tail = rest.split("\n", 1)[0].strip()
        if tail:
            lines = [tail]

    facts: list[AwardFact] = []
    seen: set[str] = set()
    # Excerpt is the labelled block a human can search for on the URL.
    block = "Awarded Firm(s): " + " ".join(lines)
    for line in lines:
        name = _firm_from_line(line)
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        fact = cited_award_or_none(
            name, block, source_url, opportunity_title=title
        )
        if fact is None:
            continue
        seen.add(key)
        facts.append(fact)
    return facts


def bind_award_facts(
    facts: Sequence[AwardFact],
    index: Sequence | None = None,
) -> list[AwardFact]:
    """Attach Phase 2 matcher results. Never force-merges two different names."""
    working = list(index or ())
    bound: list[AwardFact] = []
    for fact in facts:
        match = match_organization(fact.observed_name, working)
        from intelligence.organizations import index_with_match

        working = index_with_match(working, match, entity_kind="unknown")
        bound.append(
            AwardFact(
                observed_name=fact.observed_name,
                excerpt=fact.excerpt,
                source_url=fact.source_url,
                opportunity_title=fact.opportunity_title,
                evidence_status=fact.evidence_status,
                match=match,
            )
        )
    return bound


def ingest_award_notices(
    notices: Sequence[dict] | None,
    *,
    page_text_by_url: dict[str, str] | None = None,
    fetch: bool = True,
    persist: bool = True,
    index: Sequence | None = None,
) -> list[AwardFact]:
    """Fetch (optional) + extract + optionally persist. Fail-open on errors."""
    from intelligence.organizations import index_with_match

    stored: list[AwardFact] = []
    working = list(index or ())
    texts = page_text_by_url or {}
    for notice in list(notices or ())[:MAX_AWARDS_PER_RUN]:
        if not isinstance(notice, dict):
            continue
        url = str(notice.get("source_url") or notice.get("fetch_url") or "").strip()
        title = str(notice.get("title") or "")
        if not url:
            continue
        page = texts.get(url)
        if page is None and fetch:
            page = _fetch_award_text(url)
        if not page:
            continue
        facts = extract_awarded_firms(page, source_url=url, title=title)
        bound = bind_award_facts(facts, working)
        for fact in bound:
            if fact.match is not None:
                working = index_with_match(
                    working, fact.match, entity_kind="unknown"
                )
            if persist:
                try:
                    from database import intelligence_facts as facts_store

                    facts_store.persist_award_fact(fact)
                except Exception:
                    pass
            stored.append(fact)
    return stored


def _fetch_award_text(url: str) -> str:
    """Same download path as tender pages; no ToR quality gate (award pages can be short)."""
    try:
        from processors.downloader import download_document, extract_text_from_html
        from utils.urls import assert_public_http_url

        assert_public_http_url(url, resolve=True)
        content = download_document(url)
        if not content:
            return ""
        if content[:4] == b"%PDF":
            from processors.downloader import extract_text_from_pdf

            return extract_text_from_pdf(content)[:MAX_PAGE_CHARS]
        return extract_text_from_html(
            content.decode("utf-8", errors="ignore")
        )[:MAX_PAGE_CHARS]
    except Exception:
        return ""
