# diagnose_scrapers.py
"""
Run this to diagnose what each scraper source actually returns.
Output tells you: did the page load, how many links found, 
what the top 10 links look like.
Use this to write accurate selectors before running the full pipeline.

Usage: python diagnose_scrapers.py
"""
import asyncio
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from bs4 import BeautifulSoup
from loguru import logger
from utils.browser_security import install_browser_request_guard, launch_chromium
from utils.urls import UnsafeURLError, assert_public_http_url

SOURCES_TO_CHECK = [
    {
        "name": "Somalia Jobs Tenders",
        "url":  "https://www.somalijobs.com/tenders",
    },
]


async def diagnose_source(browser, source: dict) -> None:
    name = source["name"]
    url  = source["url"]
    page = await browser.new_page()

    print(f"\n{'='*60}")
    print(f"SOURCE: {name}")
    print(f"URL:    {url}")
    print(f"{'='*60}")

    try:
        # This diagnostic loads URLs configured by a developer, but its page
        # still follows arbitrary portal redirects and subresources. Use the
        # production browser policy rather than creating a privileged bypass.
        assert_public_http_url(url, resolve=True)
        await install_browser_request_guard(page)
        await page.set_extra_http_headers({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        })
        await page.goto(url, wait_until="commit", timeout=45000)
        try:
            await page.wait_for_selector('a[href*="/tenders/"]', timeout=20000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(8000)

        html  = await page.content()
        soup  = BeautifulSoup(html, "lxml")
        title = soup.title.string if soup.title else "No title"

        print(f"Page title: {title}")
        print(f"HTML length: {len(html):,} chars")

        # Show top-level structural tags to understand page layout
        structural = {}
        for tag in ["article", "div", "tr", "li", "section"]:
            count = len(soup.find_all(tag))
            if count > 0:
                structural[tag] = count
        print(f"Structural tags: {structural}")

        # Find all anchors and show the most relevant ones
        anchors = soup.select("a[href]")
        print(f"Total anchor tags: {len(anchors)}")

        # Filter to anchors that look like tender/contract listings
        tender_keywords = [
            "tender", "consultanc", "procurement", "rfp", "tor",
            "contract", "notice", "bid", "eoi", "proposal", "evaluation",
            "assessment", "research", "survey",
        ]
        relevant = []
        for a in anchors:
            text = a.get_text(strip=True)
            href = a.get("href", "")
            if len(text) > 15 and any(
                kw in text.lower() or kw in href.lower()
                for kw in tender_keywords
            ):
                relevant.append((text[:80], href[:100]))

        if relevant:
            print(f"\nRelevant anchors found ({len(relevant)}):")
            for text, href in relevant[:15]:
                print(f"  TITLE: {text}")
                print(f"  HREF:  {href}")
                print()
        else:
            print("\nNo obviously tender-related anchors found.")
            print("Top 10 anchors by text length:")
            sorted_anchors = sorted(
                anchors,
                key=lambda a: len(a.get_text(strip=True)),
                reverse=True
            )
            for a in sorted_anchors[:10]:
                text = a.get_text(strip=True)
                href = a.get("href", "")
                if text:
                    print(f"  TITLE: {text[:80]}")
                    print(f"  HREF:  {href[:100]}")
                    print()

        # Show first 500 chars of body text to understand content
        body_text = soup.get_text(separator=" ", strip=True)
        print(f"\nBody text preview:")
        print(f"  {body_text[:500]}")

        # Show CSS classes on likely container elements
        print(f"\nCSS classes on article/div/tr elements (first 10):")
        for tag in soup.find_all(["article", "tr"], limit=10):
            classes = tag.get("class", [])
            if classes:
                print(f"  <{tag.name} class='{' '.join(classes)}'>")

    except (PWTimeout, UnsafeURLError):
        print(f"TIMEOUT — page took too long to load")
    except Exception as e:
        print(f"ERROR — {e}")
    finally:
        await page.close()


async def main():
    async with async_playwright() as p:
        browser = await launch_chromium(p)
        for source in SOURCES_TO_CHECK:
            await diagnose_source(browser, source)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
