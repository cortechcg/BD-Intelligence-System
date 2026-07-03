# monitors/rss_monitor.py
import feedparser
import httpx
from datetime import datetime
from loguru import logger
from database.supabase_client import check_opportunity_exists, store_opportunity
from database.airtable_client import create_opportunity, log_agent_action
from config import RSS_FEEDS, CORTECH_PROFILE
import anthropic
import json

client = anthropic.Anthropic()


# ── HARD FILTERS — reject before any API call ─────────────────────────────

# Staff role signals — titles that structurally indicate an employee
# vacancy regardless of thematic content
STAFF_ROLE_SIGNALS = [
    "chief of", "head of", "director of", "director -", "manager -",
    "officer -", "coordinator -", "team leader (tl)", "consortium manager",
    "country director", "programme coordinator", "programme manager",
    "project manager", "project coordinator", "hr &", "human resources",
    "finance officer", "finance manager", "operations manager",
    "safety & wellness", "security officer", "security manager",
    "account administrator", "account manager", "talent director",
    "people and talent", "case management specialist", "education manager",
    "gbv specialist", "protection specialist", "nutrition officer",
    "health officer", "wash officer", "stagiaire", "asistente",
    "recursos humanos", "accountability assistant", "ingo forum",
    "programme funding", "grants manager", "grants officer",
    "mine action", "demining",
]

# Cortech's actual geographic focus — East Africa and Horn of Africa
# Any posting outside these geographies is irrelevant regardless of theme
CORTECH_GEOGRAPHIES = [
    "somalia", "kenya", "ethiopia", "sudan", "south sudan",
    "djibouti", "eritrea", "uganda", "tanzania", "rwanda",
    "east africa", "horn of africa", "sub-saharan africa",
    "eastern africa", "igad", "nairobi", "mogadishu", "addis",
    "kampala", "dar es salaam", "kigali",
]

# Anti-geographies — if the posting is explicitly about these places
# and only these places, reject immediately
WRONG_GEOGRAPHIES = [
    "ukraine", "syria", "colombia", "dominican republic", "india",
    "afghanistan", "nigeria", "paris", "france", "middle east",
    "latin america", "central america", "south asia", "southeast asia",
    "central african republic", " car)", "south sudan aweil",
    "myanmar", "bangladesh", "pakistan", "iraq", "jordan", "lebanon",
    "yemen", "libya", "mali", "niger", "burkina faso", "chad",
    "cameroon", "congo", "drc", "democratic republic of the congo",
    "mozambique", "zimbabwe", "zambia", "malawi",
]

# Consultancy-shaped signals — must have at least one of these to pass
CONSULTANCY_SIGNALS = [
    "consultancy", "consultant", "terms of reference", "tor",
    "request for proposal", "rfp", "request for quotation", "rfq",
    "expression of interest", "eoi", "call for proposals",
    "technical assistance", "call for tenders", "procurement notice",
    "invitation to bid",
]

# Cortech's actual thematic focus
THEMATIC_SIGNALS = [
    "evaluation", "assessment", "research", "monitoring",
    "mel", "meal", "survey design", "baseline", "endline",
    "mid-term review", "impact assessment", "capacity building",
    "data collection", "humanitarian", "refugee", "displacement",
    "livelihoods", "wash", "food security", "health system",
    "documentation", "knowledge management", "policy analysis",
]


def quick_relevance_check(title: str, summary: str) -> bool:
    """
    Three-gate filter — all three must pass before an opportunity
    reaches Claude. Runs in microseconds at zero API cost.

    Gate 1: Reject staff vacancies by title pattern
    Gate 2: Require correct geography
    Gate 3: Require consultancy framing + thematic match
    """
    title_lower = title.lower()
    text_lower = (title + " " + summary).lower()

    # ── GATE 1: Staff role rejection ──────────────────────────────────────
    # Reject immediately if the title pattern signals an employee vacancy
    if any(signal in title_lower for signal in STAFF_ROLE_SIGNALS):
        logger.debug(f"  FILTERED (staff role): {title[:60]}")
        return False

    # ── GATE 2: Geography check ───────────────────────────────────────────
    # Must mention at least one Cortech geography somewhere in the text
    has_right_geography = any(geo in text_lower for geo in CORTECH_GEOGRAPHIES)

    # Hard reject if explicitly only about wrong geographies AND
    # no right geography is mentioned
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

    # ── GATE 3: Consultancy framing + thematic match ──────────────────────
    has_consultancy_signal = any(s in text_lower for s in CONSULTANCY_SIGNALS)
    has_thematic_signal = any(s in text_lower for s in THEMATIC_SIGNALS)

    if not has_consultancy_signal:
        logger.debug(f"  FILTERED (no consultancy framing): {title[:60]}")
        return False

    if not has_thematic_signal:
        logger.debug(f"  FILTERED (no thematic match): {title[:60]}")
        return False

    logger.debug(f"  PASSED filter: {title[:60]}")
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