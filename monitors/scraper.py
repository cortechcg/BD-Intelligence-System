# monitors/scraper.py
"""
Browser-based scraper for tender sources that render content via JavaScript.
Uses Playwright (already installed) to load pages fully before extraction.
All results pass through the same three-gate filter as RSS entries.
"""

import asyncio
from urllib.parse import urljoin

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup
from loguru import logger
from database.supabase_client import check_opportunity_exists
from monitors.rss_monitor import quick_relevance_check
from utils.browser_security import launch_chromium, prepare_browser_page
from config import MAX_DOCUMENT_BYTES, MAX_DOWNLOAD_REDIRECTS
from utils.urls import (
    UnsafeURLError,
    assert_public_http_url,
    assert_safe_redirect,
    canonicalize_url,
)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
MIN_USEFUL_HTML_CHARS = 5000
DEFAULT_TIMEOUT_MS = 45000


# ── SOURCE DEFINITIONS ────────────────────────────────────────────────────────
# Each entry defines how to find tender links on that specific site.
# selector: CSS selector that matches individual tender/listing items
# link_selector: within each item, where the clickable link lives
# title_selector: within each item, where the title text lives
# base_url: prepended to relative hrefs

SCRAPE_SOURCES = [
    {
        "name":              "Somali Jobs Tenders",
        "url":               "https://www.somalijobs.com/tenders",
        "selector":          "a[href*='/tenders/']",
        "link_selector":     "a[href]",
        "title_selector":    "a",
        "base_url":          "https://www.somalijobs.com",
        "needs_browser":     True,
        "wait_for":          'a[href*="/tenders/"]',
        "href_must_contain": "/tenders/",
        "post_load_wait_ms": 8000,
        "scroll_selector":   'a[href*="/tenders/"]',
        "timeout":           DEFAULT_TIMEOUT_MS,
    },
    {
        "name":              "Save the Children tenders",
        "url":               "https://www.savethechildren.net/tenders",
        "selector":          "div.three_col-listing-card",
        "link_selector":     "a[href]",
        "title_selector":    "h3.three_col-listing-card__h3",
        "base_url":          "https://www.savethechildren.net",
        "needs_browser":     False,
        "href_must_contain": "/tenders/",
        "timeout":           DEFAULT_TIMEOUT_MS,
    },
    {
        "name":              "African Development Bank procurement",
        "url":               "https://www.afdb.org/en/projects-and-operations/procurement",
        "selector":          "div.views-field-title a[href*='/en/documents/']",
        "link_selector":     "a[href]",
        "title_selector":    "a",
        "base_url":          "https://www.afdb.org",
        "needs_browser":     False,
        "href_must_contain": "/en/documents/",
        "timeout":           DEFAULT_TIMEOUT_MS,
    },
    {
        "name":              "Danish Refugee Council tenders",
        "url":               "https://drc.ngo/en/tenders/",
        "selector":          "article.tenderList__item:not([data-status='archived'])",
        "link_selector":     "a[href]",
        "title_selector":    "span.tenderList__item__title",
        "base_url":          "https://drc.ngo",
        "needs_browser":     False,
        "href_must_contain": "/en/tenders/",
        "timeout":           DEFAULT_TIMEOUT_MS,
    },
    {
        "name":              "UNDP procurement notices",
        "url":               "https://procurement-notices.undp.org/",
        "selector":          "a[href*='view_notice.cfm'], a[href*='view_negotiation.cfm']",
        "link_selector":     "a[href]",
        "title_selector":    "span",
        "base_url":          "https://procurement-notices.undp.org",
        "needs_browser":     False,
        "timeout":           DEFAULT_TIMEOUT_MS,
    },
    {
        "name":              "World Bank procurement notices",
        "url":               "https://projects.worldbank.org/en/projects-operations/opportunities?srce=both",
        "json_url":          (
            "https://search.worldbank.org/api/v2/procnotices"
            "?format=json&fl=id,bid_description,project_ctry_name,project_name,notice_type,notice_status"
            "&srt=submission_date%20desc,id%20asc&apilang=en&rows=20&os=0"
        ),
        "json_kind":         "worldbank",
        "base_url":          "https://projects.worldbank.org",
        "needs_browser":     False,
        "timeout":           DEFAULT_TIMEOUT_MS,
    },
    {
        "name":              "World Bank consultancy EOIs",
        "url":               "https://wbgeprocure-rfxnow.worldbank.org/rfxnow/public/advertisement/index.html",
        "json_url":          (
            "https://wbgeprocure-rfxnow.worldbank.org/rfxnow/json/advertisement/activeAdvertisements.json"
        ),
        "json_kind":         "rfxnow",
        "base_url":          "https://wbgeprocure-rfxnow.worldbank.org",
        "needs_browser":     False,
        "timeout":           DEFAULT_TIMEOUT_MS,
    },
    {
        "name":              "UNGM procurement notices",
        "url":               "https://www.ungm.org/Public/Notice",
        "selector":          "a[href*='/Public/Notice/']",
        "link_selector":     "a[href]",
        "title_selector":    "a",
        "base_url":          "https://www.ungm.org",
        "needs_browser":     True,
        "wait_for":          "a[href*='/Public/Notice/']",
        "href_must_contain": "/Public/Notice/",
        "post_load_wait_ms": 4000,
        "timeout":           DEFAULT_TIMEOUT_MS,
    },
]


# ── PAGE FETCHERS ─────────────────────────────────────────────────────────────

async def fetch_with_httpx(url: str, timeout_ms: int) -> str | None:
    """Fast static fetch with the same per-hop SSRF policy as downloads."""
    current_url = url
    try:
        async with httpx.AsyncClient(
            # Redirect targets are untrusted and must be checked before a
            # socket is opened. httpx's automatic redirect follower cannot do
            # that for us.
            follow_redirects=False,
            timeout=timeout_ms / 1000,
            headers=HTTP_HEADERS,
        ) as client:
            for redirect_count in range(MAX_DOWNLOAD_REDIRECTS + 1):
                assert_public_http_url(current_url, resolve=True)
                async with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            response.raise_for_status()
                            raise RuntimeError("Redirect response did not include a Location header")
                        if redirect_count >= MAX_DOWNLOAD_REDIRECTS:
                            raise RuntimeError("Too many redirects while fetching a scraper page")
                        next_url = urljoin(current_url, location)
                        assert_safe_redirect(current_url, next_url)
                        current_url = next_url
                        continue

                    # Many NGO portals return 404 for legacy URLs but still
                    # serve useful HTML, so preserve that legacy behavior.
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_DOCUMENT_BYTES:
                            raise ValueError("Scraper page exceeds configured byte limit")
                    text = body.decode(response.encoding or "utf-8", errors="replace")
                    if len(text) >= MIN_USEFUL_HTML_CHARS:
                        return text
                    logger.debug(
                        f"  httpx returned thin page ({response.status_code}, "
                        f"{len(text)} chars): {url}"
                    )
                    return None
    except (UnsafeURLError, ValueError) as e:
        logger.warning(f"  Static scraper fetch blocked: {e}")
    except Exception as e:
        logger.debug(f"  httpx fetch failed for {url}: {e}")
    return None


async def _scroll_to_load_all(
    page,
    item_selector: str,
    max_scrolls: int = 40,
    settle_ms: int = 2500,
) -> None:
    """
    Repeatedly scroll to the bottom to trigger lazy-loaded / infinite-scroll
    content. Stops when the number of matching items stops growing for two
    consecutive scrolls, or when max_scrolls is reached.

    Many tender portals (e.g. somalijobs.com) render only ~20 items initially
    and append more as the user scrolls — without this we'd only ever see the
    first page's worth.
    """
    previous_count = -1
    stable_rounds = 0
    for _ in range(max_scrolls):
        try:
            count = await page.eval_on_selector_all(
                item_selector, "els => els.length"
            )
        except Exception:
            count = 0

        if count <= previous_count:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0
        previous_count = count

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(settle_ms)


async def fetch_with_browser(
    browser,
    url: str,
    wait_for: str | None,
    timeout_ms: int,
    post_load_wait_ms: int = 2000,
    scroll_selector: str | None = None,
) -> str | None:
    """
    Playwright fetch using wait_until='commit' — many NGO sites never fire
    domcontentloaded within a reasonable window but commit immediately.

    When scroll_selector is provided, the page is scrolled repeatedly to
    exhaust lazy-loaded / infinite-scroll listings before the HTML is captured.
    """
    page = await browser.new_page()
    try:
        await prepare_browser_page(page)
        await page.set_extra_http_headers(HTTP_HEADERS)
        await page.goto(url, wait_until="commit", timeout=timeout_ms)
        if wait_for:
            try:
                await page.wait_for_selector(
                    wait_for,
                    timeout=min(timeout_ms, 20000),
                )
            except PlaywrightTimeout:
                pass
        await page.wait_for_timeout(post_load_wait_ms)
        if scroll_selector:
            await _scroll_to_load_all(page, scroll_selector)
        html = await page.content()
        if len(html) >= MIN_USEFUL_HTML_CHARS:
            return html
        logger.debug(
            f"  browser returned thin page ({len(html)} chars): {url}"
        )
        return html or None
    except PlaywrightTimeout:
        logger.warning(f"  Timeout loading: {url}")
        return None
    except Exception as e:
        logger.error(f"  Browser fetch error for {url}: {e}")
        return None
    finally:
        await page.close()


async def fetch_page_content(
    browser,
    url: str,
    wait_for: str | None,
    timeout_ms: int,
    needs_browser: bool = False,
    post_load_wait_ms: int = 2000,
    scroll_selector: str | None = None,
) -> str | None:
    """
    httpx first for speed; Playwright when the page is JS-rendered or thin.

    A scroll_selector forces the browser path (httpx can't scroll) so that
    lazy-loaded listings are fully exhausted.
    """
    if not needs_browser and not scroll_selector:
        html = await fetch_with_httpx(url, timeout_ms)
        if html:
            logger.debug(f"  Loaded via httpx ({len(html):,} chars): {url}")
            return html

    html = await fetch_with_browser(
        browser, url, wait_for, timeout_ms, post_load_wait_ms, scroll_selector
    )
    if html:
        logger.debug(f"  Loaded via browser ({len(html):,} chars): {url}")
        return html

    if not needs_browser:
        html = await fetch_with_browser(
            browser, url, wait_for, timeout_ms, post_load_wait_ms, scroll_selector
        )
        if html:
            logger.debug(
                f"  Loaded via browser fallback ({len(html):,} chars): {url}"
            )
    return html


# ── PER-SOURCE PARSER ─────────────────────────────────────────────────────────

def parse_tenders_from_html(
    html: str,
    source: dict
) -> list[dict]:
    """
    Parses rendered HTML to extract tender title + link pairs.
    Uses multiple CSS selectors in priority order so that if the
    site's primary card structure isn't found, fallback selectors
    still extract something useful.

    Returns list of raw opportunity dicts (not yet deduped or filtered —
    that happens in the calling function).
    """
    soup     = BeautifulSoup(html, "lxml")
    base_url = source["base_url"]
    name     = source["name"]
    found    = []

    # Try each comma-separated selector in priority order
    selectors = [s.strip() for s in source["selector"].split(",")]
    items     = []
    for sel in selectors:
        items = soup.select(sel)
        if items:
            break

    if not items:
        # Ultimate fallback: scan ALL anchors on the page for anything
        # that looks like a tender listing by text length heuristic
        logger.debug(f"  No structured items found for {name} — using anchor fallback")
        items = soup.select("a[href]")

    skip_fragments = {
        "#", "javascript:", "mailto:", "/login", "/register",
        "/contact", "/about", "/home", "/privacy", "/terms",
        "/sitemap", "/cart", "/account",
    }

    for item in items:
        # Find the link
        link_el = (
            item if item.name == "a"
            else item.select_one("a[href]")
        )
        if not link_el:
            continue

        href = link_el.get("href", "").strip()
        if not href or any(f in href.lower() for f in skip_fragments):
            continue

        href_filter = source.get("href_must_contain")
        if href_filter and href_filter not in href:
            continue

        # Build absolute URL
        if href.startswith("http"):
            full_url = href
        elif href.startswith("/"):
            full_url = base_url.rstrip("/") + href
        else:
            full_url = base_url.rstrip("/") + "/" + href.lstrip("/")

        # Find the title — try item's title selector, then link text
        title = ""
        title_selectors = [s.strip() for s in source["title_selector"].split(",")]
        for tsel in title_selectors:
            title_el = item.select_one(tsel)
            if title_el:
                title = title_el.get_text(separator=" ", strip=True)
                break
        if not title:
            title = link_el.get_text(separator=" ", strip=True)
        if not title or len(title) < 10:
            continue

        # Summary = surrounding text for the three-gate filter to use
        summary = item.get_text(separator=" ", strip=True)[:400]

        found.append({
            "title":         title,
            "source_url":    canonicalize_url(full_url) or full_url,
            "summary":       summary,
            "source_portal": name,
            "published":     "",
        })

    return found


def listings_from_json(payload: dict, source: dict) -> list[dict]:
    """Turn a portal JSON list into the same dicts the HTML parser returns.

    Only title, link, and a short public summary are kept. Contact fields
    that some payloads include are ignored.
    """
    if not isinstance(payload, dict):
        return []
    name = source.get("name", "")
    kind = source.get("json_kind")
    found: list[dict] = []

    if kind == "worldbank":
        rows = payload.get("procnotices") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            notice_id = str(row.get("id") or "").strip()
            title = str(row.get("bid_description") or row.get("project_name") or "").strip()
            if len(notice_id) < 4 or len(title) < 10:
                continue
            country = str(row.get("project_ctry_name") or "").strip()
            notice_type = str(row.get("notice_type") or "").strip()
            url = (
                "https://projects.worldbank.org/en/projects-operations/"
                f"procurement-detail/{notice_id}"
            )
            found.append({
                "title": title,
                "source_url": canonicalize_url(url) or url,
                "summary": " ".join(part for part in (country, notice_type, title) if part)[:400],
                "source_portal": name,
                "published": "",
            })
        return found

    if kind == "rfxnow":
        rows = payload.get("advertisementList") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            notice_id = str(row.get("id") or "").strip()
            title = str(row.get("procurementTitle") or "").strip()
            if not notice_id.isdigit() or len(title) < 10:
                continue
            url = (
                "https://wbgeprocure-rfxnow.worldbank.org/rfxnow/public/"
                f"advertisement/{notice_id}/view.html"
            )
            found.append({
                "title": title,
                "source_url": canonicalize_url(url) or url,
                "summary": title[:400],
                "source_portal": name,
                "published": "",
            })
    return found


async def fetch_json(url: str, timeout_ms: int) -> dict | None:
    """GET a public JSON list. Same redirect checks as the HTML fetch."""
    current_url = url
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout_ms / 1000,
            headers={**HTTP_HEADERS, "Accept": "application/json"},
        ) as client:
            for redirect_count in range(MAX_DOWNLOAD_REDIRECTS + 1):
                assert_public_http_url(current_url, resolve=True)
                response = await client.get(current_url)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirect_count >= MAX_DOWNLOAD_REDIRECTS:
                        return None
                    next_url = urljoin(current_url, location)
                    assert_safe_redirect(current_url, next_url)
                    current_url = next_url
                    continue
                if response.status_code >= 400:
                    return None
                data = response.json()
                return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning(f"  JSON list fetch failed (non-fatal): {e}")
    return None


# ── MAIN ASYNC RUNNER ─────────────────────────────────────────────────────────

async def scrape_one_source(browser, source: dict) -> list[dict]:
    """Fetch, parse, dedupe, and filter a single scrape source."""
    name = source.get("name", "unknown")
    logger.info(f"  Scraping: {name}")
    try:
        if source.get("json_url"):
            payload = await fetch_json(
                source["json_url"],
                source.get("timeout", DEFAULT_TIMEOUT_MS),
            )
            if not payload:
                logger.warning(f"  No JSON returned for {name} — skipping")
                return []
            raw_items = listings_from_json(payload, source)
        else:
            html = await fetch_page_content(
                browser,
                source["url"],
                source.get("wait_for"),
                source.get("timeout", DEFAULT_TIMEOUT_MS),
                needs_browser=source.get("needs_browser", False),
                post_load_wait_ms=source.get("post_load_wait_ms", 2000),
                scroll_selector=source.get("scroll_selector"),
            )
            if not html:
                logger.warning(f"  No HTML returned for {name} — skipping")
                return []
            raw_items = parse_tenders_from_html(html, source)

        logger.debug(f"  {len(raw_items)} raw items extracted from {name}")

        passed: list[dict] = []
        filtered = 0
        for item in raw_items:
            if check_opportunity_exists(item["source_url"]):
                continue
            if not quick_relevance_check(item["title"], item["summary"]):
                filtered += 1
                continue
            passed.append(item)

        logger.info(f"  {name}: {len(passed)} passed filter | {filtered} rejected")
        return passed
    except Exception as e:
        logger.error(f"  Scraper failed for {name} (non-fatal): {e}")
        return []


async def run_all_scrapers_async() -> list[dict]:
    """
    Runs all scrapers concurrently inside a single browser session.
    Each source gets its own tab — parallel, efficient, one browser launch.
    """
    async with async_playwright() as p:
        browser = await launch_chromium(p)

        results = await asyncio.gather(
            *[
                scrape_one_source(browser, source)
                for source in SCRAPE_SOURCES
            ],
            return_exceptions=True,
        )
        await browser.close()

    all_passed: list[dict] = []
    for batch in results:
        if isinstance(batch, Exception):
            logger.error(f"  Scraper source crashed (non-fatal): {batch}")
            continue
        all_passed.extend(batch)
    return all_passed


# ── SYNC WRAPPER (called from main.py) ───────────────────────────────────────

def scrape_non_rss_sources() -> list[dict]:
    """
    Sync entry point. Called from run_pipeline() in main.py.
    Runs the async browser scrapers and returns filtered results.
    """
    try:
        return asyncio.run(run_all_scrapers_async())
    except Exception as e:
        logger.error(f"Non-RSS scraper crashed: {e}")
        return []
