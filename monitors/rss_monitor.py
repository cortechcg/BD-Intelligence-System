# monitors/rss_monitor.py
import feedparser
import httpx
from datetime import datetime
from loguru import logger
from bs4 import BeautifulSoup
from database.supabase_client import check_opportunity_exists, store_opportunity
from database.airtable_client import create_opportunity, log_agent_action
from config import RSS_FEEDS, CORTECH_PROFILE, SCRAPE_SOURCES
import anthropic
import json

client = anthropic.Anthropic()
# ── FILTER CONSTANTS ──────────────────────────────────────────────────────────

# ONLY reject titles that are unambiguously staff vacancies.
# Keep this list SHORT. Claude catches everything else via
# is_consultancy_contract. Over-blocking here kills real proposals.
DEFINITE_STAFF_SIGNALS = [
    "we are hiring",
    "we are looking for a ",
    "vacancy announcement",
    "job opening",
    "is seeking a ",
    "is recruiting a ",
    "employment opportunity",
    "individual consultant is sought",
    "individual professional is sought",
    "stagiaire",
    "recursos humanos",
    "driver wanted",
    "intern wanted",
]

# Cortech's geographic focus — presence of ANY of these is enough
CORTECH_GEOGRAPHIES = [
    "somalia", "somali", "kenya", "kenyan", "ethiopia", "ethiopian",
    "sudan", "south sudan", "djibouti", "eritrea", "uganda", "tanzania",
    "rwanda", "east africa", "horn of africa", "sub-saharan africa",
    "eastern africa", "igad", "nairobi", "mogadishu", "addis ababa",
    "kampala", "dar es salaam", "kigali", "africa",
]

# Topics Cortech works on — presence of ANY is enough
THEMATIC_SIGNALS = [
    "evaluation", "assessment", "research", "baseline", "endline",
    "mid-term", "impact", "survey", "data collection", "mapping",
    "feasibility", "needs assessment", "situation analysis",
    "capacity building", "training", "documentation", "mel",
    "meal", "monitoring", "livelihoods", "humanitarian", "refugee",
    "displacement", "wash", "water", "sanitation", "food security",
    "health", "protection", "resilience", "climate", "governance",
    "consultanc", "procurement", "tender", "rfp", "tor", "eoi",
    "proposal", "bid", "contract", "service", "evaluation",
    "study", "review", "analysis", "framework", "strategy",
]

# Only reject if posting is EXCLUSIVELY about these wrong geographies
# with zero mention of any Cortech geography
WRONG_GEOGRAPHIES_ONLY = [
    "ukraine", "syria", "colombia", "dominican republic",
    "afghanistan", "myanmar", "bangladesh", "pakistan",
    "iraq", "jordan", "lebanon", "yemen", "libya",
    "latin america", "central america", "south asia",
    "southeast asia", "paris", "france",
]


def quick_relevance_check(title: str, summary: str) -> bool:
    """
    Lightweight pre-filter. Zero token cost.

    PHILOSOPHY: This filter's job is to block OBVIOUS noise only.
    It is intentionally permissive. Claude's is_consultancy_contract
    boolean is the real quality gate — it reads the full document.

    Pass everything that could plausibly be a consultancy contract
    in Cortech's focus areas. Reject only what is definitively wrong.

    Gate 1: Hard reject unambiguous staff vacancy language in title
    Gate 2: Reject if exclusively about wrong geographies
    Gate 3: Pass if geography match OR thematic match OR either
            — just needs ONE signal anywhere in title+summary

    When in doubt: PASS IT THROUGH. Claude will handle it.
    """
    title_lower = title.lower()
    text_lower  = (title + " " + summary).lower()

    # ── GATE 1: Unambiguous staff vacancy language ────────────────────────
    # ONLY the phrases that NEVER appear in a genuine ToR or RFP.
    # Do not add role titles here — "Senior Advisor" could be a ToR
    # requirement, not a job title.
    if any(s in title_lower for s in DEFINITE_STAFF_SIGNALS):
        logger.debug(f"  FILTERED (definite staff signal): {title[:60]}")
        return False

    # ── GATE 2: Exclusively wrong geography ──────────────────────────────
    has_cortech_geo = any(g in text_lower for g in CORTECH_GEOGRAPHIES)
    is_wrong_geo_only = (
        any(g in text_lower for g in WRONG_GEOGRAPHIES_ONLY)
        and not has_cortech_geo
    )
    if is_wrong_geo_only:
        logger.debug(f"  FILTERED (wrong geography, no Africa): {title[:60]}")
        return False

    # ── GATE 3: At least ONE signal — geography OR thematic ───────────────
    # This is the permissive gate. ONE signal anywhere passes.
    # "Endline evaluation Somalia" → passes on both geography + thematic
    # "Procurement notice Kenya" → passes on geography alone
    # "Call for proposals Africa" → passes on geography + thematic
    has_thematic = any(s in text_lower for s in THEMATIC_SIGNALS)

    if has_cortech_geo or has_thematic:
        logger.debug(f"  PASSED: {title[:60]}")
        return True

    # Nothing relevant found at all
    logger.debug(f"  FILTERED (no relevant signal): {title[:60]}")
    return False

def extract_deadline_from_text(text: str) -> str:
    """Use Claude to extract deadline if not in RSS metadata."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"Extract the submission deadline date from this text. Return ONLY the date in ISO format (YYYY-MM-DD) or 'unknown' if not found:\n\n{text[:1000]}"
        }]
    )
    date_str = response.content[0].text.strip()
    return date_str if len(date_str) <= 20 else "unknown"


def monitor_rss_feeds() -> list[dict]:
    """Monitor all configured RSS feeds and return new opportunities."""
    new_opportunities = []

    for feed_config in RSS_FEEDS:
        logger.info(f"Checking RSS: {feed_config['name']}")

        try:
            feed = feedparser.parse(feed_config["url"])
            logger.info(f"  Found {len(feed.entries)} entries")
            
            filtered_count = 0
            passed_count = 0

            for entry in feed.entries:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                link = entry.get("link", "")

                if not link:
                    continue

                # Dedup check
                if check_opportunity_exists(link):
                    continue

                # Quick relevance filter
                if not quick_relevance_check(title, summary):
                    filtered_count += 1
                    continue

                passed_count += 1
                new_opportunities.append({
                    "title": title,
                    "source_url": link,
                    "summary": summary,
                    "source_portal": feed_config["name"],
                    "published": entry.get("published", ""),
                })
                logger.success(f"  NEW: {title[:60]}...")

            logger.info(f"  {passed_count} passed, {filtered_count} filtered out")
   
        except Exception as e:
            logger.error(f"RSS error for {feed_config['name']}: {e}")
            log_agent_action(
                action_type="Error",
                description=f"RSS monitor failed for {feed_config['name']}: {e}",
                status="Error",
                error_message=str(e)
            )

    logger.info(f"Total new opportunities from RSS: {len(new_opportunities)}")
    return new_opportunities



def scrape_somaliajobs(url: str) -> list[dict]:
    """
    Scrapes Somalia Jobs tender board.
    Page structure: list of tender cards with title, client, deadline.
    """
    opportunities = []
    try:
        response = httpx.get(url, timeout=20, follow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        # Somalia Jobs uses <div class="job-listing"> or similar cards
        # Adjust selector if site structure changes
        cards = (
            soup.select("div.tender-item")
            or soup.select("div.job-listing")
            or soup.select("article")
            or soup.select("tr.tender-row")
        )

        for card in cards:
            # Try multiple common link patterns
            link_el = card.select_one("a[href]")
            title_el = card.select_one("h2, h3, h4, .title, .tender-title")

            if not link_el or not title_el:
                continue

            title = title_el.get_text(strip=True)
            href  = link_el.get("href", "")
            link  = href if href.startswith("http") else f"https://www.somaliajobs.com{href}"
            summary = card.get_text(separator=" ", strip=True)[:500]

            if check_opportunity_exists(link):
                continue

            if not quick_relevance_check(title, summary):
                continue

            opportunities.append({
                "title":        title,
                "source_url":   link,
                "summary":      summary,
                "source_portal": "Somalia Jobs Tenders",
                "published":    "",
            })

    except Exception as e:
        logger.error(f"Somalia Jobs scrape failed: {e}")

    return opportunities


def scrape_generic_list(source: dict) -> list[dict]:
    """
    Generic scraper for procurement list pages.
    Extracts any anchor tag that looks like a tender/consultancy posting.
    Works as a best-effort fallback for sources without structured markup.
    """
    opportunities = []
    name = source["name"]
    url  = source["url"]

    try:
        response = httpx.get(
            url, timeout=20, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        # Collect all anchor tags with meaningful text
        for anchor in soup.select("a[href]"):
            title = anchor.get_text(strip=True)
            href  = anchor.get("href", "")

            if not title or len(title) < 15:
                continue

            # Only follow links that look like individual tender pages
            skip_patterns = [
                "login", "register", "contact", "about", "home",
                "privacy", "terms", "sitemap", "javascript:", "mailto:",
                "#",
            ]
            if any(p in href.lower() for p in skip_patterns):
                continue

            link = href if href.startswith("http") else (
                url.rstrip("/") + "/" + href.lstrip("/")
            )

            # Get surrounding text as summary context
            parent_text = ""
            if anchor.parent:
                parent_text = anchor.parent.get_text(separator=" ", strip=True)[:300]

            summary = parent_text or title

            if check_opportunity_exists(link):
                continue

            if not quick_relevance_check(title, summary):
                continue

            opportunities.append({
                "title":         title,
                "source_url":    link,
                "summary":       summary,
                "source_portal": name,
                "published":     "",
            })

    except Exception as e:
        logger.error(f"Scrape failed for {name}: {e}")

    return opportunities


def scrape_non_rss_sources() -> list[dict]:
    """
    Runs all non-RSS scrapers defined in config.SCRAPE_SOURCES.
    Called from run_pipeline() alongside monitor_rss_feeds().
    """
    all_found = []

    for source in SCRAPE_SOURCES:
        logger.info(f"Scraping: {source['name']}")

        if source["type"] == "somaliajobs":
            found = scrape_somaliajobs(source["url"])
        else:
            found = scrape_generic_list(source)

        logger.info(f"  {len(found)} passed filter from {source['name']}")
        all_found.extend(found)

    return all_found