# monitors/assortis_email.py
"""
Parses the Assortis section out of the daily "ICA Daily Newsletter"
email. Unlike rss_monitor.py / scraper.py, there is no URL to fetch per
item — the newsletter blurb IS the source text. process_opportunity()
in main.py has a conditional path for this (see skip_fetch flag).
"""
import os
import re
from datetime import datetime
from bs4 import BeautifulSoup
from imap_tools import MailBox, AND
from loguru import logger

from monitors.rss_monitor import quick_relevance_check
from database.supabase_client import check_opportunity_exists, find_similar_opportunity

IMAP_HOST = os.getenv("IMAP_HOST")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USERNAME = os.getenv("IMAP_USERNAME")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD")
NEWSLETTER_SENDER = os.getenv("IMAP_NEWSLETTER_SENDER", "Info ICA World")
NEWSLETTER_SUBJECT = os.getenv("IMAP_NEWSLETTER_SUBJECT", "ICA Daily Newsletter")

# Newsletter blurbs are short by nature — this source needs a lower bar
# than the 200-char "insufficient text" threshold used for fetched ToR
# documents elsewhere in the pipeline. Do not reuse that constant here.
MIN_BLURB_LENGTH = 40


def _make_pseudo_url(title: str, received_date: datetime) -> str:
    """
    No real URL exists for these listings. Build a stable, deterministic
    pseudo-URL so check_opportunity_exists() dedup still works across
    re-runs (same title + same newsletter date -> same pseudo-URL).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    date_str = received_date.strftime("%Y-%m-%d")
    return f"assortis-newsletter://{date_str}/{slug}"


def _parse_assortis_section(html: str, received_date: datetime) -> list[dict]:
    """Extract each listing from the 'Assortis Business opportunities' block."""
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    # TODO(Cursor): the exact tag structure needs confirming against a
    # real saved .eml from this sender before this is production-solid —
    # inspect actual markup rather than assuming it matches the visual
    # layout exactly. The parsing logic below is a reasonable starting
    # shape based on the visible content, not a verified DOM structure.
    # The same email also contains an "ICA Projects & Vacancies" section
    # (separate from Assortis, its own listings) — a natural follow-up.
    header = soup.find(string=re.compile("Assortis Business opportunities"))
    if not header:
        logger.warning("Assortis section not found in this newsletter — layout may have changed")
        return listings

    section = header.find_parent().find_next("div") or header.find_parent()
    blocks = section.find_all(["b", "strong"])  # each listing title

    for title_tag in blocks:
        title = title_tag.get_text(strip=True)
        if not title or len(title) < 5:
            continue

        # Walk forward to the metadata line and description paragraph
        meta_text, desc_text = "", ""
        node = title_tag.find_next_sibling()
        collected = []
        while node and "Add to My projects" not in node.get_text():
            collected.append(node.get_text(" ", strip=True))
            node = node.find_next_sibling()
            if len(collected) >= 4:  # safety bound
                break
        if collected:
            meta_text = collected[0] if collected else ""
            desc_text = " ".join(collected[1:]) if len(collected) > 1 else ""

        deadline_match = re.search(r"Deadline:\s*([\d]{1,2}\s+\w+\s+\d{4})", meta_text)
        deadline = deadline_match.group(1) if deadline_match else ""

        full_text = f"{title}\n{meta_text}\n{desc_text}".strip()
        if len(full_text) < MIN_BLURB_LENGTH:
            continue

        listings.append({
            "title": title,
            "summary": desc_text or meta_text,
            "raw_text": full_text,          # this IS the full text — no fetch step needed
            "source_url": _make_pseudo_url(title, received_date),
            "source_portal": "Assortis (ICA Newsletter)",
            "deadline_hint": deadline,
            "skip_fetch": True,             # tells process_opportunity() there's nothing to download
        })

    return listings


def check_assortis_newsletter() -> list[dict]:
    """
    Connects to IMAP, finds unread newsletter emails from the configured
    sender, extracts Assortis listings, runs them through the same free
    filters every other source uses, and marks the email as seen once
    processed so re-runs don't reparse it.

    Returns a list of opportunity dicts in the SAME shape rss_monitor.py
    and scraper.py produce, so process_opportunity() can treat them
    uniformly except for the skip_fetch flag.
    """
    if not all([IMAP_HOST, IMAP_USERNAME, IMAP_PASSWORD]):
        logger.warning("IMAP credentials not configured — skipping Assortis newsletter check")
        return []

    new_opportunities = []
    try:
        with MailBox(IMAP_HOST, IMAP_PORT).login(IMAP_USERNAME, IMAP_PASSWORD) as mailbox:
            for msg in mailbox.fetch(
                AND(seen=False, from_=NEWSLETTER_SENDER, subject=NEWSLETTER_SUBJECT),
                mark_seen=False,
            ):
                listings = _parse_assortis_section(msg.html, msg.date)
                logger.info(f"  Parsed {len(listings)} Assortis listing(s) from newsletter dated {msg.date}")

                for item in listings:
                    if check_opportunity_exists(item["source_url"]):
                        continue
                    if not quick_relevance_check(item["title"], item["summary"]):
                        continue
                    if find_similar_opportunity(item["title"]):
                        logger.debug(f"  FILTERED (semantic duplicate, likely also on another portal): {item['title'][:60]}")
                        continue
                    new_opportunities.append(item)

                mailbox.flag(msg.uid, "\\Seen", True)  # mark processed regardless of filter outcome

    except Exception as e:
        logger.error(f"Assortis newsletter check failed: {e}")
        return []

    return new_opportunities
