# monitors/assortis_email.py
"""
Parses opportunity listings out of the daily "ICA Daily Newsletter"
email (sender: info@icaworld.net).

Two sections of that email carry opportunities:
  * "Assortis Business opportunities" — the main tender feed
    (~15-25 listings/day), linking to assortis.com/tpl/bsc_view.asp
  * "ICA Projects & Vacancies" — member-posted project opportunities
    (0-4/day), linking to icaworld.net/Intranet/ProjectByNewsletter

Both link types resolve to a full, publicly-fetchable listing page, so
these behave like every other source: the newsletter gives us the URL
and a summary blurb, and processors/downloader.py fetches the detail.
The blurb is kept as `fallback_text` for when the fetch fails, and is
prepended to the fetched page so the newsletter's own metadata line
(donor, country, deadline) always reaches the analyzer.

Dedup note: the newsletter rewrites its links on every send — the
Assortis `open=` access token and the ICA `utn=` token are different in
each day's email, and `uid=` differs per listing. So the URL we FETCH
and the URL we DEDUP on are deliberately not the same string; see
`dedup_url` below. Without this, the same tender re-listed the next day
would look brand new and be re-analyzed at full API cost.
"""
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from imap_tools import MailBox, AND
from loguru import logger

from config import (
    IMAP_HOST,
    IMAP_PORT,
    IMAP_USERNAME,
    IMAP_PASSWORD,
    IMAP_FOLDER,
    IMAP_NEWSLETTER_SENDER,
    IMAP_NEWSLETTER_SUBJECT,
    IMAP_LOOKBACK_DAYS,
)
from monitors.rss_monitor import quick_relevance_check
from database.supabase_client import check_opportunity_exists, find_similar_opportunity

# Newsletter blurbs are short by nature — this source needs a lower bar
# than the 200-char "insufficient text" threshold used for fetched ToR
# documents elsewhere in the pipeline. Do not reuse that constant here.
MIN_BLURB_LENGTH = 40

ASSORTIS_PORTAL = "Assortis (ICA Newsletter)"
ICA_PORTAL = "ICA World (ICA Newsletter)"

_ASSORTIS_BASE = "https://www.assortis.com/tpl/bsc_view.asp"
_ICA_BASE = "https://www.icaworld.net/Intranet/ProjectByNewsletter"

# The newsletter also lists DataType=contract items — those are AWARD
# notices (someone else already won), not open tenders. Only busop is
# an opportunity to bid on.
_ASSORTIS_OPEN_TYPE = "busop"


def _norm(text: str) -> str:
    """Collapse the hard-wrapped whitespace the newsletter's HTML is full of."""
    return re.sub(r"\s+", " ", text or "").strip()


def _assortis_urls(href: str) -> tuple[str, str] | None:
    """
    Returns (fetch_url, dedup_url) for an Assortis business-opportunity
    link, or None if this href is not one.

    fetch_url keeps `open=` — it is the access token that makes the page
    readable without a logged-in session, and it changes every newsletter.
    dedup_url keeps only the listing id, which is stable forever.
    """
    parsed = urlparse(href)
    if "assortis.com" not in parsed.netloc.lower():
        return None
    if not parsed.path.lower().endswith("bsc_view.asp"):
        return None

    query = parse_qs(parsed.query)
    data_type = (query.get("DataType") or [""])[0].lower()
    listing_id = (query.get("id") or [""])[0].strip()
    open_token = (query.get("open") or [""])[0].strip()

    if data_type != _ASSORTIS_OPEN_TYPE or not listing_id:
        return None

    fetch_url = f"{_ASSORTIS_BASE}?id={listing_id}&DataType={_ASSORTIS_OPEN_TYPE}"
    if open_token:
        fetch_url = (
            f"{_ASSORTIS_BASE}?open={open_token}"
            f"&id={listing_id}&DataType={_ASSORTIS_OPEN_TYPE}"
        )
    dedup_url = f"{_ASSORTIS_BASE}?id={listing_id}&DataType={_ASSORTIS_OPEN_TYPE}"
    return fetch_url, dedup_url


def _ica_project_urls(href: str) -> tuple[str, str] | None:
    """
    Returns (fetch_url, dedup_url) for an ICA member project link, or
    None. `utn=` is the per-newsletter token — needed to fetch, useless
    for identity. p2id is the project id.
    """
    parsed = urlparse(href)
    if "icaworld.net" not in parsed.netloc.lower():
        return None
    if "projectbynewsletter" not in parsed.path.lower():
        return None

    query = parse_qs(parsed.query)
    project_id = (query.get("p2id") or [""])[0].strip()
    if not project_id:
        return None

    return href, f"{_ICA_BASE}?p2id={project_id}"


def _parse_meta_line(meta: str) -> dict:
    """
    Both sections use a pipe-delimited metadata line, e.g.
      "Kenya | WB | Services | Deadline: 01 Sep 2026 | Update"
      "Multi-country | ADB, GEF | TA | Individual Consultants"
    Empty positions are normal — the newsletter emits "| |" freely.
    """
    parts = [p.strip() for p in meta.split("|")]
    deadline_match = re.search(
        r"Deadline:\s*(\d{1,2}\s+\w+\s+\d{4})", meta, re.IGNORECASE
    )
    return {
        "location_hint": parts[0] if parts else "",
        "donor_hint": parts[1] if len(parts) > 1 else "",
        "deadline_hint": deadline_match.group(1) if deadline_match else "",
    }


def _listing_from_anchor(anchor, fetch_url: str, dedup_url: str, portal: str) -> dict | None:
    """
    Build one opportunity dict from a title anchor.

    Anchored on the link rather than on the surrounding table structure:
    the newsletter is react-email output with no semantic markup at all
    (titles are plain <a> tags styled bold via inline CSS — there is not
    a single <b>/<strong>/<h*> in the document), so the link is the only
    reliable handle on a listing.
    """
    title = _norm(anchor.get_text(" ", strip=True))
    if len(title) < 5:
        return None

    container = anchor.find_parent("table")
    if container is None:
        return None

    # Paragraphs inside the item block, in document order:
    # [0] title, [1] metadata line, [2:] description.
    paragraphs = [
        _norm(p.get_text(" ", strip=True)) for p in container.find_all("p")
    ]
    paragraphs = [p for p in paragraphs if p]

    meta_text = paragraphs[1] if len(paragraphs) > 1 else ""
    desc_text = " ".join(paragraphs[2:]) if len(paragraphs) > 2 else ""

    meta = _parse_meta_line(meta_text)
    full_text = "\n".join(part for part in (title, meta_text, desc_text) if part)
    if len(full_text) < MIN_BLURB_LENGTH:
        return None

    return {
        "title": title,
        "summary": desc_text or meta_text,
        "source_url": fetch_url,
        # Not the same string as source_url on purpose — see module docstring.
        "dedup_url": dedup_url,
        "source_portal": portal,
        "deadline_hint": meta["deadline_hint"],
        # Used if the listing page fetch fails, and prepended to it when
        # it succeeds, so donor/country/deadline always reach the analyzer.
        "fallback_text": full_text,
    }


def _parse_newsletter(html: str, received_date: datetime) -> list[dict]:
    """Extract every opportunity listing from one newsletter email."""
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    listings: list[dict] = []
    seen_dedup_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].replace("&amp;", "&")

        resolved = _assortis_urls(href)
        portal = ASSORTIS_PORTAL
        if resolved is None:
            resolved = _ica_project_urls(href)
            portal = ICA_PORTAL
        if resolved is None:
            continue

        fetch_url, dedup_url = resolved
        # Each listing appears twice — once as the title link, once as the
        # "+ Add to My projects" / "+ Express Interest" call to action.
        # The title link comes first, so first-wins is the right rule.
        if dedup_url in seen_dedup_urls:
            continue

        item = _listing_from_anchor(anchor, fetch_url, dedup_url, portal)
        if item is None:
            continue

        seen_dedup_urls.add(dedup_url)
        item["published"] = received_date.strftime("%Y-%m-%d")
        listings.append(item)

    if not listings:
        logger.warning(
            f"No listings parsed from the {received_date:%Y-%m-%d} newsletter — "
            "either a quiet day or the email layout changed"
        )
    return listings


def _sender_matches(msg) -> bool:
    """
    Match on the envelope address OR the display name, case-insensitively.
    The IMAP FROM search key is not used for this: the newsletter's From
    header has varied between 'Info ICA World' and a bare
    'info@icaworld.net' over time, and a server-side FROM search on the
    display name silently misses most of the archive.
    """
    needle = (IMAP_NEWSLETTER_SENDER or "").lower().strip()
    if not needle:
        return True
    candidates = [(msg.from_ or "").lower()]
    from_values = getattr(msg, "from_values", None)
    if from_values is not None:
        candidates.append((from_values.name or "").lower())
    return any(needle in candidate for candidate in candidates)


def check_assortis_newsletter() -> list[dict]:
    """
    Connects to IMAP, reads the last IMAP_LOOKBACK_DAYS of ICA Daily
    Newsletters, extracts their listings, and runs them through the same
    free filters every other source uses.

    Deliberately does NOT use (or set) the \\Seen flag. This is a shared
    human inbox: people read the newsletter themselves, which used to
    make every listing invisible to the agent, and the agent marking
    mail as read hid it from them in return. Dedup is Supabase's job —
    it already keys on URL — so re-reading the same email is free.

    Returns opportunity dicts in the SAME shape rss_monitor.py and
    scraper.py produce, plus `dedup_url` and `fallback_text`, both
    handled by process_opportunity() in main.py.
    """
    if not all([IMAP_HOST, IMAP_USERNAME, IMAP_PASSWORD]):
        logger.warning("IMAP credentials not configured — skipping Assortis newsletter check")
        return []

    since = (datetime.now() - timedelta(days=IMAP_LOOKBACK_DAYS)).date()
    new_opportunities: list[dict] = []
    seen_dedup_urls: set[str] = set()
    # find_similar_opportunity() can only see what is already stored, so it
    # cannot catch repeats WITHIN this batch. Multi-day tenders are re-listed
    # verbatim under a fresh Assortis id on each day of the lookback window,
    # which would otherwise mean one full drafted proposal per day.
    seen_titles: set[str] = set()

    try:
        with MailBox(IMAP_HOST, IMAP_PORT).login(
            IMAP_USERNAME, IMAP_PASSWORD, initial_folder=IMAP_FOLDER
        ) as mailbox:
            criteria = AND(date_gte=since, subject=IMAP_NEWSLETTER_SUBJECT)
            messages = [
                msg for msg in mailbox.fetch(criteria, mark_seen=False)
                if _sender_matches(msg)
            ]
            logger.info(
                f"  {len(messages)} newsletter(s) since {since} "
                f"from '{IMAP_NEWSLETTER_SENDER}'"
            )
            if not messages:
                logger.warning(
                    f"  No '{IMAP_NEWSLETTER_SUBJECT}' email found in "
                    f"{IMAP_FOLDER} since {since} — check IMAP_NEWSLETTER_SENDER "
                    "/ IMAP_NEWSLETTER_SUBJECT / IMAP_LOOKBACK_DAYS in .env"
                )

            for msg in messages:
                listings = _parse_newsletter(msg.html, msg.date)
                logger.info(
                    f"  Parsed {len(listings)} listing(s) from newsletter "
                    f"dated {msg.date:%Y-%m-%d}"
                )

                for item in listings:
                    if item["dedup_url"] in seen_dedup_urls:
                        continue  # same listing repeated across the lookback window
                    seen_dedup_urls.add(item["dedup_url"])

                    title_key = item["title"].casefold()
                    if title_key in seen_titles:
                        logger.debug(
                            f"  FILTERED (re-listed under a new id in this "
                            f"batch): {item['title'][:60]}"
                        )
                        continue
                    seen_titles.add(title_key)

                    if check_opportunity_exists(item["dedup_url"]):
                        continue
                    if not quick_relevance_check(item["title"], item["summary"]):
                        continue
                    if find_similar_opportunity(item["title"]):
                        logger.debug(
                            f"  FILTERED (semantic duplicate, likely also on "
                            f"another portal): {item['title'][:60]}"
                        )
                        continue
                    logger.success(f"  NEW: {item['title'][:60]}...")
                    new_opportunities.append(item)

    except Exception as e:
        logger.error(f"Assortis newsletter check failed: {e}")
        return []

    logger.info(
        f"  Newsletter check complete — {len(new_opportunities)} new opportunity(ies)"
    )
    return new_opportunities
