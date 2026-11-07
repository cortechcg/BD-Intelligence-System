"""Bounded discovery for user-configured public procurement sources.

Only RSS/Atom and high-confidence public listing pages are activated. Fetches
use the existing DNS-pinned transport, manually validate every redirect, and
never run downloaded scripts or submit forms.
"""
from __future__ import annotations

import re
import time
import uuid
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
from bs4 import BeautifulSoup

from database import procurement_sources as registry
from database.supabase_client import check_opportunity_exists
from monitors.rss_monitor import quick_relevance_check
from utils.dns_pinned_http import PinnedResponse, exchange
from utils.urls import (
    UnsafeURLError,
    assert_public_http_url,
    assert_safe_redirect,
    canonicalize_url,
)

MAX_SOURCE_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_REDIRECTS = 2
SOURCE_HTTP_TIMEOUT_SECONDS = 6
SOURCE_TOTAL_TIMEOUT_SECONDS = 9
MAX_LISTING_ITEMS = 20
MAX_FEED_ITEMS = 40
MAX_SOURCE_CHECKS_PER_RUN = 8
MAX_SOURCE_RUN_SECONDS = 45
SOURCE_LEASE_SECONDS = 120
_HEADERS = {
    "User-Agent": "Cortech BD procurement monitor/1.0 (+public listing checks)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.5",
}
_PROCUREMENT_TERMS = re.compile(
    r"\b(tender|procurement|request for proposals?|rfp|request for quotations?|rfq|"
    r"expression of interest|eoi|invitation to bid|bid notice|contract notice|"
    r"call for proposals?|solicitation)\b",
    re.IGNORECASE,
)
_BOT_MARKERS = (
    "captcha required", "captcha verification", "complete the captcha",
    "verify you are human", "challenge-platform", "just a moment",
    "cf-browser-verification", "unusual traffic", "security check",
)
_AUTH_MARKERS = ("sign in to continue", "log in to continue", "authentication required", "login required")
_STRONG_CONTAINER = re.compile(r"tender|notice|procurement|opportun|listing|views-row", re.I)


class SourceProbeFailure(RuntimeError):
    def __init__(self, capability: str, reason: str):
        super().__init__(reason)
        self.capability = capability
        self.reason = reason


def _clean_text(value: object, limit: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _sanitize_candidate(url: str, base_url: str) -> str | None:
    absolute = urljoin(base_url, str(url or "").strip())
    try:
        assert_public_http_url(absolute, resolve=False)
        parsed = urlparse(absolute)
        host = (parsed.hostname or "").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        default_port = 80 if parsed.scheme.lower() == "http" else 443
        netloc = host if port is None or port == default_port else f"{host}:{port}"
    except UnsafeURLError:
        return None
    except ValueError:
        return None
    # Keep procurement query identifiers such as ref= while normalizing the
    # authority. Tracking removal belongs to the separate dedup identity.
    return urlunparse((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.params, parsed.query, "")) or None


def _identifier(url: str, explicit: str = "") -> str:
    raw = _clean_text(explicit, 160)
    if raw:
        return raw
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        query = {}
    for key in ("noticeid", "notice_id", "procurementid", "procurement_id", "reference", "ref", "id"):
        value = next((v for k, values in query.items() if k.lower() == key for v in values), "")
        if value:
            return _clean_text(value, 160)
    match = re.search(r"(?:notice|tender|procurement)[/_-]([A-Za-z0-9._-]{3,80})", url, re.I)
    return match.group(1) if match else ""


def _lot_metadata(text: str) -> tuple[str, str]:
    lot = re.search(
        r"\blot\s*(?:(?:no\.?|number|#|:)\s*([A-Za-z0-9][A-Za-z0-9._/-]{0,30})|\s+([0-9]+|[A-Z])\b)",
        text,
        re.I,
    )
    addendum = re.search(
        r"\baddend(?:um|a)\s*(?:(?:no\.?|number|#|:)\s*([A-Za-z0-9][A-Za-z0-9._/-]{0,30})|\s+([0-9]+|[A-Z])\b)",
        text,
        re.I,
    )
    lot_id = next((value for value in lot.groups() if value), "") if lot else ""
    addendum_id = next((value for value in addendum.groups() if value), "") if addendum else ""
    return lot_id, addendum_id


def _variant_url(url: str, lot_id: str, addendum_id: str) -> str:
    """Internal identity for one lot/addendum while retaining its fetch URL."""
    parsed = urlparse(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    stripped_reference = next(
        (value for key, value in query if key.lower() == "ref" and value),
        "",
    )
    if not lot_id and not addendum_id and not stripped_reference:
        return ""
    marker = "|".join(
        part for part in (
            f"lot:{lot_id}" if lot_id else "",
            f"addendum:{addendum_id}" if addendum_id else "",
            f"ref:{stripped_reference}" if stripped_reference else "",
        )
        if part
    )
    query.append(("__cortech_variant", marker))
    identity = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), ""))
    return canonicalize_url(identity) or identity


def _request_one(url: str, *, deadline: float) -> PinnedResponse:
    current = url
    for redirect_count in range(MAX_SOURCE_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Source check exceeded its total time limit")
        assert_public_http_url(current, resolve=False)
        response = exchange(
            current,
            headers=_HEADERS,
            timeout=min(SOURCE_HTTP_TIMEOUT_SECONDS, remaining),
            max_bytes=MAX_SOURCE_RESPONSE_BYTES,
            max_elapsed_seconds=remaining,
        )
        if len(response.body) > MAX_SOURCE_RESPONSE_BYTES:
            raise ValueError("Source response exceeds the 2 MiB limit.")
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                raise SourceProbeFailure("unsupported", "The source returned an incomplete redirect.")
            if redirect_count >= MAX_SOURCE_REDIRECTS:
                raise SourceProbeFailure("unsupported", "The source exceeded the redirect limit.")
            next_url = urljoin(current, location)
            # The next pinned exchange validates every DNS answer and connects
            # to one of them directly, avoiding a second unbounded resolver
            # lookup between validation and fetch.
            assert_safe_redirect(current, next_url, resolve=False)
            current = next_url
            continue
        return response
    raise SourceProbeFailure("unsupported", "The source exceeded the redirect limit.")


def _is_feed_document(body: bytes, content_type: str) -> bool:
    lower_type = content_type.lower()
    if any(marker in lower_type for marker in ("rss", "atom", "xml", "rdf")):
        return True
    head = body[:2048].lstrip().lower()
    return any(marker in head for marker in (b"<rss", b"<feed", b"<rdf:rdf", b"<?xml"))


def _feed_items(body: bytes, base_url: str, source: dict) -> list[dict]:
    parsed = feedparser.parse(body)
    if not parsed.entries and not (parsed.version or parsed.feed.get("title")):
        raise SourceProbeFailure("unsupported", "The response is not a valid RSS or Atom feed.")
    found: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in parsed.entries[:MAX_FEED_ITEMS]:
        title = _clean_text(entry.get("title"), 300)
        raw_link = entry.get("link") or ""
        link = _sanitize_candidate(raw_link, base_url)
        if not title or not link:
            continue
        summary = _clean_text(entry.get("summary") or entry.get("description") or "", 700)
        lot_id, addendum_id = _lot_metadata(f"{title} {summary}")
        identity_key = (
            canonicalize_url(link, preserve_query_keys={"ref"}), lot_id, addendum_id
        )
        if identity_key in seen:
            continue
        seen.add(identity_key)
        explicit_id = entry.get("id") or entry.get("guid") or ""
        variant_url = _variant_url(link, lot_id, addendum_id)
        found.append({
            "title": title,
            "source_url": link,
            "dedup_url": variant_url,
            "original_source_url": link,
            "summary": summary,
            "source_portal": source["name"],
            "published": _clean_text(entry.get("published") or entry.get("updated") or "", 80),
            "procurement_identifier": _identifier(link, str(explicit_id)),
            "lot_id": lot_id,
            "addendum_id": addendum_id,
            "_source_id": source["id"],
            "_source_type": "custom",
        })
    return found


def _listing_items(html: str, base_url: str, source: dict) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    page_text = _clean_text(soup.get_text(" ", strip=True), 16000)
    if any(marker in html[:20000].lower() for marker in _BOT_MARKERS):
        raise SourceProbeFailure(
            "captcha_blocked", "A CAPTCHA or anti-bot challenge blocks this public listing."
        )
    lowered = page_text.lower()
    login_form = soup.select_one("form input[type=password]") or soup.select_one("form[action*=login]")
    login_copy = any(word in lowered for word in ("sign in", "log in", "login"))
    if any(marker in lowered for marker in _AUTH_MARKERS) or (login_form and login_copy):
        raise SourceProbeFailure(
            "authentication_required", "This portal requires sign-in; authenticated crawling is not supported."
        )
    procurement_page = bool(_PROCUREMENT_TERMS.search(page_text))
    if not procurement_page:
        raise SourceProbeFailure(
            "unsupported", "This page does not identify itself as a public procurement listing."
        )

    found: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    structured_count = 0
    for anchor in soup.select("a[href]"):
        title = _clean_text(anchor.get_text(" ", strip=True), 300)
        if len(title) < 12:
            continue
        href = str(anchor.get("href") or "").strip()
        link = _sanitize_candidate(href, base_url)
        if not link or canonicalize_url(link, preserve_query_keys={"ref"}) == canonicalize_url(
            base_url, preserve_query_keys={"ref"}
        ):
            continue
        structured_parent = None
        for parent in anchor.parents:
            classes = " ".join(parent.get("class") or [])
            if parent.name in {"article", "li", "tr"} or _STRONG_CONTAINER.search(classes):
                structured_parent = parent
                break
            if parent.name in {"body", "html"}:
                break
        structured = structured_parent is not None
        if structured_parent is not None:
            container_text = _clean_text(structured_parent.get_text(" ", strip=True), 600)
        else:
            parent = anchor.parent
            parent_text = _clean_text(parent.get_text(" ", strip=True), 600) if parent else ""
            container_text = title if getattr(parent, "name", "") in {"body", "html"} or len(parent_text) > 1000 else parent_text
        lot_id, addendum_id = _lot_metadata(f"{title} {container_text}")
        identity_key = (
            canonicalize_url(link, preserve_query_keys={"ref"}), lot_id, addendum_id
        )
        if identity_key in seen:
            continue
        # Broad footer/navigation links do not make a listing. Require an
        # explicit procurement term in either the card or its anchor.
        context = title if not structured and container_text == title else f"{title} {container_text}"
        if not _PROCUREMENT_TERMS.search(context):
            continue
        if structured:
            structured_count += 1
        seen.add(identity_key)
        variant_url = _variant_url(link, lot_id, addendum_id)
        found.append({
            "title": title,
            "source_url": link,
            "dedup_url": variant_url,
            "original_source_url": link,
            "summary": container_text or title,
            "source_portal": source["name"],
            "published": "",
            "procurement_identifier": _identifier(link),
            "lot_id": lot_id,
            "addendum_id": addendum_id,
            "_source_id": source["id"],
            "_source_type": "custom",
        })
        if len(found) >= MAX_LISTING_ITEMS:
            break

    if not found or not (structured_count >= 1 or len(found) >= 2):
        raise SourceProbeFailure(
            "adapter_required",
            "The page appears procurement-related, but its listings cannot be extracted reliably without a portal adapter.",
        )
    return found


def probe_source(source: dict, *, deadline: float | None = None) -> dict:
    """Fetch and classify one source; returns a bounded public preview."""
    url = registry.normalize_source_url(str(source.get("url") or ""))
    started = time.monotonic()
    source_deadline = started + SOURCE_TOTAL_TIMEOUT_SECONDS
    if deadline is not None:
        source_deadline = min(source_deadline, deadline)
    response = _request_one(url, deadline=source_deadline)
    status = int(response.status_code)
    response_sample = response.body[:20000].decode("utf-8", errors="replace").lower()
    if any(marker in response_sample for marker in _BOT_MARKERS):
        raise SourceProbeFailure(
            "captcha_blocked", "A CAPTCHA or anti-bot challenge blocks this source."
        )
    if status == 401 or any(marker in response_sample for marker in _AUTH_MARKERS):
        raise SourceProbeFailure(
            "authentication_required", "The portal denied public access. Sign-in is not supported."
        )
    if status == 403:
        raise SourceProbeFailure(
            "access_restricted",
            "The portal denied public access (HTTP 403). It may require an account or permission; access controls are not bypassed.",
        )
    if status == 429:
        fallback = str(source.get("capability") or "")
        capability = fallback if fallback in registry.SUPPORTED_CAPABILITIES else "network_error"
        raise SourceProbeFailure(capability, "The provider rate-limited this check; retry after its limit resets.")
    if status in {408, 425} or status >= 500:
        fallback = str(source.get("capability") or "")
        capability = fallback if fallback in registry.SUPPORTED_CAPABILITIES else "network_error"
        raise SourceProbeFailure(capability, f"The provider returned HTTP {status}; retry after it recovers.")
    if status >= 400:
        raise SourceProbeFailure("unsupported", f"The source returned HTTP {status}.")
    content_type = str(response.headers.get("content-type") or "")
    if _is_feed_document(response.body, content_type):
        items = _feed_items(response.body, response.url, source)
        capability = "rss_atom"
    else:
        text = response.body.decode("utf-8", errors="replace")
        items = _listing_items(text, response.url, source)
        capability = "public_listing"
    preview = [
        {
            "title": item["title"][:240],
            "url": item["source_url"][:1200],
            "procurement_identifier": item.get("procurement_identifier", "")[:160],
            "lot_id": item.get("lot_id", "")[:80],
            "addendum_id": item.get("addendum_id", "")[:80],
        }
        for item in items[:5]
    ]
    return {"ok": True, "capability": capability, "items": items, "preview": preview, "error": ""}


def _safe_probe_error(exc: Exception, fallback_capability: str = "") -> tuple[str, str]:
    if isinstance(exc, SourceProbeFailure):
        return exc.capability, exc.reason
    if isinstance(exc, UnsafeURLError):
        return "unsupported", str(exc)[:300]
    if isinstance(exc, TimeoutError):
        capability = fallback_capability if fallback_capability in registry.SUPPORTED_CAPABILITIES else "network_error"
        return capability, "The source timed out during the bounded check. Retry after the provider is available."
    if isinstance(exc, ValueError) and any(
        marker in str(exc).lower() for marker in ("byte limit", "2 mib limit")
    ):
        capability = fallback_capability if fallback_capability in registry.SUPPORTED_CAPABILITIES else "unsupported"
        return capability, "The source response exceeded the 2 MiB limit."
    capability = fallback_capability if fallback_capability in registry.SUPPORTED_CAPABILITIES else "network_error"
    return capability, f"The source could not be checked ({type(exc).__name__}). Retry or request an adapter."


def test_saved_source(source_id: str) -> dict:
    """Run one manual check, even when paused, with a cross-process lease."""
    source = registry.get_source(source_id)
    if not source or source.get("source_type") != "custom":
        return {"ok": False, "error": "Custom source not found."}
    token = str(uuid.uuid4())
    if not registry.claim_source_run(
        source_id, token, allow_disabled=True, respect_backoff=False,
        lease_seconds=SOURCE_LEASE_SECONDS,
    ):
        return {"ok": False, "busy": True, "error": "A source check is running or its short provider cooldown has not elapsed. Wait briefly."}
    try:
        result = probe_source(source)
    except Exception as exc:
        capability, reason = _safe_probe_error(exc, str(source.get("capability") or ""))
        saved = registry.finish_source_run(
            source_id, token, success=False, error=reason, capability=capability,
        )
        if not saved:
            return {
                "ok": False,
                "capability": capability,
                "error": "The check failed, but its health result could not be saved. Retry after the active lease expires.",
                "preview": [],
            }
        return {"ok": False, "capability": capability, "error": reason, "preview": []}
    if not registry.source_is_enabled(source_id) and source.get("enabled"):
        registry.finish_source_run(
            source_id, token, success=False,
            error="The source was paused during this check; results were discarded.",
            cancelled=True,
        )
        return {"ok": False, "error": "The source was paused during this check; results were discarded."}
    saved = registry.finish_source_run(
        source_id, token, success=True, preview=result["preview"],
        capability=result["capability"],
    )
    if not saved:
        return {
            "ok": False,
            "capability": result["capability"],
            "error": "The check completed, but its health result could not be saved. Retry after the active lease expires.",
            "preview": [],
        }
    return result


def discover_configured_sources() -> tuple[list[dict], list[dict]]:
    """Check enabled custom sources for one scheduled discovery run.

    Every attempted source has an atomic database lease. A source is checked
    again after the network read so pausing it mid-flight drops its results.
    Each run checks at most eight one-page sources and 45 seconds of wall time.
    """
    sources = registry.list_sources(include_builtins=False)
    sources = [row for row in sources if row.get("enabled")]
    sources.sort(key=lambda row: str(row.get("last_attempt_at") or ""))
    deadline = time.monotonic() + MAX_SOURCE_RUN_SECONDS
    opportunities: list[dict] = []
    runs: list[dict] = []
    attempted = 0

    for source in sources:
        if attempted >= MAX_SOURCE_CHECKS_PER_RUN or time.monotonic() >= deadline:
            break
        source_id = str(source.get("id") or "")
        if not source_id:
            continue
        token = str(uuid.uuid4())
        try:
            claimed = registry.claim_source_run(
                source_id, token, allow_disabled=False, respect_backoff=True,
                lease_seconds=SOURCE_LEASE_SECONDS,
            )
        except registry.SourceRegistryUnavailable:
            break
        if not claimed:
            continue
        attempted += 1
        current = registry.get_source(source_id)
        if not current or not current.get("enabled"):
            runs.append({"source_id": source_id, "token": token, "cancelled": True})
            continue
        try:
            result = probe_source(current, deadline=deadline)
            # A pause during the fetch suppresses all results from that run.
            if not registry.source_is_enabled(source_id):
                runs.append({
                    "source_id": source_id, "token": token, "cancelled": True,
                    "error": "The source was paused during this check; results were discarded.",
                })
                continue
            passed: list[dict] = []
            for item in result["items"]:
                if check_opportunity_exists(item.get("dedup_url") or item["source_url"]):
                    continue
                if not quick_relevance_check(item["title"], item["summary"]):
                    continue
                passed.append(item)
            opportunities.extend(passed)
            runs.append({
                "source_id": source_id,
                "token": token,
                "success": True,
                "capability": result["capability"],
                "preview": result["preview"],
                "items": passed,
                "error": "",
            })
        except Exception as exc:
            capability, reason = _safe_probe_error(exc, str(current.get("capability") or ""))
            runs.append({
                "source_id": source_id,
                "token": token,
                "success": False,
                "capability": capability,
                "preview": [],
                "items": [],
                "error": reason,
            })

    return opportunities, runs


def finalize_discovery_runs(runs: list[dict], discovered_counts: dict[str, int]) -> None:
    """Persist health after main.py has passed results through its shared dedup ledger."""
    for run in runs or []:
        if run.get("cancelled"):
            registry.finish_source_run(
                run["source_id"], run["token"], success=False,
                error=str(run.get("error") or "Source paused before discovery completed."),
                cancelled=True,
            )
            continue
        registry.finish_source_run(
            run["source_id"], run["token"], success=bool(run.get("success")),
            error=str(run.get("error") or ""),
            discovered_delta=int(discovered_counts.get(run["source_id"], 0)),
            preview=run.get("preview") or [], capability=run.get("capability"),
        )
