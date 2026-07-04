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

# Hard staff-role signals — reject immediately if title contains any of these
STAFF_ROLE_SIGNALS = [
    "chief of", "head of", "director of", "director -", "director,",
    "manager -", "manager,", "officer -", "officer,", "officer (",
    "coordinator -", "coordinator,", "coordinator (",
    "team leader (tl)", "consortium manager", "country director",
    "programme coordinator", "programme manager", "project manager",
    "project coordinator", "hr &", "human resources", "finance officer",
    "finance manager", "operations manager", "safety & wellness",
    "security officer", "security manager", "account administrator",
    "account manager", "talent director", "people and talent",
    "case management specialist", "education manager",
    "protection specialist", "nutrition officer", "health officer",
    "wash officer", "stagiaire", "asistente", "recursos humanos",
    "accountability assistant", "ingo forum", "programme funding",
    "grants manager", "grants officer", "mine action", "demining",
    "senior advisor", "technical advisor", "policy advisor",
    "legal advisor", "advocacy advisor", "communications officer",
    "information officer", "logistics officer", "procurement officer",
    "supply chain", "driver", "intern ", "volunteer ", "fellow ",
    "is looking for", "we are looking for", "we are hiring",
    "vacancy announcement", "job opening", "position announcement",
    "individual consultant",   # ← one person being hired, not a firm
    "national consultant",     # ← individual hire
    "international consultant",# ← individual hire
    "individual professional",
]

# Cortech's geographic focus — must appear somewhere in the posting
CORTECH_GEOGRAPHIES = [
    "somalia", "kenya", "ethiopia", "sudan", "south sudan",
    "djibouti", "eritrea", "uganda", "tanzania", "rwanda",
    "east africa", "horn of africa", "sub-saharan africa",
    "eastern africa", "igad", "nairobi", "mogadishu", "addis",
    "kampala", "dar es salaam", "kigali",
]

# Wrong geographies — reject if posting is ONLY about these
WRONG_GEOGRAPHIES = [
    "ukraine", "syria", "colombia", "dominican republic", "india",
    "afghanistan", "nigeria", "paris", "france", "middle east",
    "latin america", "central america", "south asia", "southeast asia",
    "central african republic", "myanmar", "bangladesh", "pakistan",
    "iraq", "jordan", "lebanon", "yemen", "libya", "mali", "niger",
    "burkina faso", "chad", "cameroon", "drc",
    "democratic republic of the congo", "mozambique", "zimbabwe",
    "zambia", "malawi",
]

# Anything not in this list at text level never reaches Claude.
# Only genuine procurement/contract language passes.
CONSULTANCY_MUST_HAVE = [
    "consultancy",
    "consulting firm",
    "consulting company",
    "terms of reference",
    "request for proposal",
    "request for quotation",
    "expression of interest",
    "call for proposals",
    "call for tenders",
    "invitation to bid",
    "competitive bidding",
    "framework contract",
    "service contract",
    "technical assistance contract",
    "procurement notice",
    "supplier",
    "service provider",
    "rfp",
    "tor",
    "eoi",
    "rfq",
    "itb",
    "open tender",
    "restricted tender",
]

# Thematic signals — must appear alongside the consultancy signal
THEMATIC_SIGNALS = [
    "evaluation", "assessment", "research", "monitoring",
    "mel", "meal", "survey design", "baseline", "endline",
    "mid-term review", "impact assessment", "capacity building",
    "data collection", "humanitarian", "refugee", "displacement",
    "livelihoods", "wash", "food security", "health system",
    "documentation", "knowledge management", "policy analysis",
    "rapid assessment", "needs assessment", "feasibility study",
    "situation analysis", "mapping study",
]


def quick_relevance_check(title: str, summary: str) -> bool:
    """
    Three-gate filter — all three must pass before an opportunity
    reaches Claude. Runs in microseconds at zero API cost.

    Gate 1: Explicit consultancy-contract signal required.
            No signal = rejected, regardless of anything else.
            This is what stops staff vacancies, advisor roles,
            and individual placements from burning Opus tokens.

    Gate 2: Must mention at least one Cortech geography.
            Must NOT be exclusively about a wrong geography.

    Gate 3: Must contain a relevant thematic signal.
    """
    title_lower = title.lower()
    text_lower  = (title + " " + summary).lower()

    # ── PRE-CHECK: Hard staff role rejection ──────────────────────────────
    if any(signal in title_lower for signal in STAFF_ROLE_SIGNALS):
        logger.debug(f"  FILTERED (staff role title): {title[:60]}")
        return False

    # ── GATE 1: Must be a consultancy contract ────────────────────────────
    # This is the primary gate. An opportunity that doesn't explicitly
    # use consultancy-contract language is not a firm-level bid opportunity
    # for Cortech, regardless of how thematically relevant it looks.
    has_consultancy_signal = any(
        s in text_lower for s in CONSULTANCY_MUST_HAVE
    )
    if not has_consultancy_signal:
        logger.debug(f"  FILTERED (no consultancy contract signal): {title[:60]}")
        return False

    # ── GATE 2: Geography check ───────────────────────────────────────────
    has_right_geography = any(
        geo in text_lower for geo in CORTECH_GEOGRAPHIES
    )
    is_wrong_geography = (
        any(geo in text_lower for geo in WRONG_GEOGRAPHIES)
        and not has_right_geography
    )

    if is_wrong_geography:
        logger.debug(f"  FILTERED (wrong geography): {title[:60]}")
        return False

    if not has_right_geography:
        logger.debug(f"  FILTERED (no target geography): {title[:60]}")
        return False

    # ── GATE 3: Thematic relevance ────────────────────────────────────────
    has_thematic_signal = any(
        s in text_lower for s in THEMATIC_SIGNALS
    )
    if not has_thematic_signal:
        logger.debug(f"  FILTERED (no thematic match): {title[:60]}")
        return False

    logger.debug(f"  PASSED all gates: {title[:60]}")
    return True

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