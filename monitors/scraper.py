# monitors/scraper.py
"""
Browser-based scraper for tender sources that render content via JavaScript.
Uses Playwright (already installed) to load pages fully before extraction.
All results pass through the same three-gate filter as RSS entries.
"""

import asyncio
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup
from loguru import logger
from database.supabase_client import check_opportunity_exists
from monitors.rss_monitor import quick_relevance_check


# ── SOURCE DEFINITIONS ────────────────────────────────────────────────────────
# Each entry defines how to find tender links on that specific site.
# selector: CSS selector that matches individual tender/listing items
# link_selector: within each item, where the clickable link lives
# title_selector: within each item, where the title text lives
# base_url: prepended to relative hrefs

SCRAPE_SOURCES = [
    {
        "name":           "Somali Jobs Tenders",
        "url":            "https://www.somalijobs.com/tenders",
        "selector":       "div.job-item, article.tender, div.listing-item, tr.tender",
        "link_selector":  "a[href]",
        "title_selector": "h2, h3, h4, .title, a",
        "base_url":       "https://www.somalijobs.com",
        "wait_for":       "body",
        "timeout":        15000,
    },
    {
        "name":           "DRC Procurement",
        "url":            "https://pro.drc.ngo/suppliers/open-procurements/",
        "selector":       "article, div.procurement-item, div.tender-item, tr",
        "link_selector":  "a[href]",
        "title_selector": "h2, h3, h4, .title, a",
        "base_url":       "https://pro.drc.ngo",
        "wait_for":       "body",
        "timeout":        15000,
    },
    {
        "name":           "Save the Children Procurement",
        "url":            "https://www.savethechildren.net/procurement",
        "selector":       "article, div.tender, tr.tender-row, div.result-item",
        "link_selector":  "a[href]",
        "title_selector": "h2, h3, h4, .title, a",
        "base_url":       "https://www.savethechildren.net",
        "wait_for":       "body",
        "timeout":        15000,
    },
    {
        "name":           "CARE International Tenders",
        "url":            "https://www.care.org/about-us/procurement/",
        "selector":       "article, div.tender, div.opportunity, tr",
        "link_selector":  "a[href]",
        "title_selector": "h2, h3, h4, .title, a",
        "base_url":       "https://www.care.org",
        "wait_for":       "body",
        "timeout":        15000,
    },
    {
        "name":           "Welthungerhilfe Tenders",
        "url":            "https://www.welthungerhilfe.org/our-work/procurement/",
        "selector":       "article, div.tender-item, div.content-item, tr",
        "link_selector":  "a[href]",
        "title_selector": "h2, h3, h4, .title, a",
        "base_url":       "https://www.welthungerhilfe.org",
        "wait_for":       "body",
        "timeout":        15000,
    },
    {
        "name":           "NRC Tenders",
        "url":            "https://www.nrc.no/about-nrc/procurements/",
        "selector":       "article, div.tender, div.procurement-item, li.tender",
        "link_selector":  "a[href]",
        "title_selector": "h2, h3, h4, .title, a",
        "base_url":       "https://www.nrc.no",
        "wait_for":       "body",
        "timeout":        15000,
    },
    {
        "name":           "GIZ Tenders",
        "url":            "https://www.giz.de/en/html/tenders.html",
        "selector":       "div.tender, article, tr.tender-row, div.result",
        "link_selector":  "a[href]",
        "title_selector": "h2, h3, h4, .title, a",
        "base_url":       "https://www.giz.de",
        "wait_for":       "body",
        "timeout":        15000,
    },
    {
        "name":           "USAID Business Forecast",
        "url":            "https://www.usaid.gov/business-forecast",
        "selector":       "div.view-row, tr.views-row, div.opportunity, article",
        "link_selector":  "a[href]",
        "title_selector": "h2, h3, h4, .views-field-title, a",
        "base_url":       "https://www.usaid.gov",
        "wait_for":       "body",
        "timeout":        20000,
    },
    {
        "name":           "FCDO Contracts Finder",
        "url":            "https://www.contractsfinder.service.gov.uk/Search/Results?&KeywordsAndFilters=east+africa+consultancy",
        "selector":       "div.search-result, article, div.opportunity",
        "link_selector":  "a[href]",
        "title_selector": "h2, h3, h4, .title, a",
        "base_url":       "https://www.contractsfinder.service.gov.uk",
        "wait_for":       "div.search-result, body",
        "timeout":        20000,
    },
    {
        "name":           "Kenya Government PPIP",
        "url":            "https://tenders.go.ke/website/tenders/index",
        "selector":       "tr, div.tender-item, div.listing",
        "link_selector":  "a[href]",
        "title_selector": "td, h3, h4, a",
        "base_url":       "https://tenders.go.ke",
        "wait_for":       "body",
        "timeout":        20000,
    },
    {
        "name":           "IGAD Procurement",
        "url":            "https://igad.int/procurements/",
        "selector":       "article, div.post, div.tender, tr",
        "link_selector":  "a[href]",
        "title_selector": "h2, h3, h4, .entry-title, a",
        "base_url":       "https://igad.int",
        "wait_for":       "body",
        "timeout":        15000,
    },
]


# ── CORE BROWSER FETCHER ──────────────────────────────────────────────────────

async def fetch_page_content(
    browser,
    url: str,
    wait_for: str,
    timeout: int
) -> str | None:
    """
    Opens a page in a real browser, waits for content to render,
    returns the full rendered HTML. Returns None on any failure.
    """
    page = await browser.new_page()
    try:
        await page.set_extra_http_headers({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        })
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        # Wait for the specific element that confirms content has loaded
        try:
            await page.wait_for_selector(wait_for, timeout=8000)
        except PlaywrightTimeout:
            # Selector didn't appear but page loaded — proceed anyway
            pass
        # Extra buffer for JS-rendered content to finish painting
        await page.wait_for_timeout(2000)
        return await page.content()
    except PlaywrightTimeout:
        logger.warning(f"  Timeout loading: {url}")
        return None
    except Exception as e:
        logger.error(f"  Browser fetch error for {url}: {e}")
        return None
    finally:
        await page.close()


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
            "source_url":    full_url,
            "summary":       summary,
            "source_portal": name,
            "published":     "",
        })

    return found


# ── MAIN ASYNC RUNNER ─────────────────────────────────────────────────────────

async def run_all_scrapers_async() -> list[dict]:
    """
    Runs all scrapers concurrently inside a single browser session.
    Each source gets its own tab — parallel, efficient, one browser launch.
    """
    all_passed: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        for source in SCRAPE_SOURCES:
            name = source["name"]
            logger.info(f"  Scraping: {name}")

            html = await fetch_page_content(
                browser,
                source["url"],
                source["wait_for"],
                source["timeout"],
            )

            if not html:
                logger.warning(f"  No HTML returned for {name} — skipping")
                continue

            raw_items = parse_tenders_from_html(html, source)
            logger.debug(f"  {len(raw_items)} raw items extracted from {name}")

            passed   = 0
            filtered = 0
            for item in raw_items:
                # Dedup check
                if check_opportunity_exists(item["source_url"]):
                    continue
                # Three-gate relevance filter
                if not quick_relevance_check(item["title"], item["summary"]):
                    filtered += 1
                    continue
                passed += 1
                all_passed.append(item)

            logger.info(
                f"  {name}: {passed} passed filter | {filtered} rejected"
            )

        await browser.close()

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