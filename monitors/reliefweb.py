# monitors/reliefweb.py
from playwright.async_api import async_playwright
import asyncio
from loguru import logger
from database.supabase_client import check_opportunity_exists


async def scrape_reliefweb_consultancies() -> list[dict]:
    """
    Scrape ReliefWeb consultancy listings.
    More comprehensive than RSS feed.
    """
    opportunities = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # ReliefWeb consultancies with East Africa filter
        url = "https://reliefweb.int/jobs?type=consultancy&region=africa"
        await page.goto(url, wait_until="networkidle")

        # Wait for job listings to load
        await page.wait_for_selector("article.article")

        jobs = await page.query_selector_all("article.article")
        logger.info(f"ReliefWeb: Found {len(jobs)} listings")

        for job in jobs[:30]:  # Process max 30 per run
            try:
                title_el = await job.query_selector("h3 a")
                title = await title_el.inner_text() if title_el else ""
                link = await title_el.get_attribute("href") if title_el else ""
                if link and not link.startswith("http"):
                    link = "https://reliefweb.int" + link

                if not link or check_opportunity_exists(link):
                    continue

                # Get metadata
                country_el = await job.query_selector(".location")
                country = await country_el.inner_text() if country_el else ""

                deadline_el = await job.query_selector(".closing-date")
                deadline_text = await deadline_el.inner_text() if deadline_el else ""

                opportunities.append({
                    "title": title.strip(),
                    "source_url": link,
                    "location_text": country.strip(),
                    "deadline_text": deadline_text.strip(),
                    "source_portal": "ReliefWeb",
                })

            except Exception as e:
                logger.error(f"Error parsing job listing: {e}")
                continue

        await browser.close()

    return opportunities


def scrape_reliefweb() -> list[dict]:
    """Sync wrapper for the async scraper."""
    return asyncio.run(scrape_reliefweb_consultancies())