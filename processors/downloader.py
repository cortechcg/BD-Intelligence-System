# processors/downloader.py
import asyncio
import httpx
import pdfplumber
from docx import Document as DocxDocument
from io import BytesIO
from loguru import logger
from database.supabase_client import store_document
import re

MIN_USEFUL_CHARS = 200  # matches main.py's own "insufficient text" threshold


def download_document(url: str) -> bytes:
    """Download a document from URL."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    try:
        response = httpx.get(url, headers=headers, follow_redirects=True, timeout=30)
        response.raise_for_status()
        return response.content
    except Exception as e:
        logger.error(f"Download failed for {url}: {e}")
        raise


def extract_text_from_pdf(content: bytes) -> str:
    """Extract text from PDF bytes."""
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n\n".join(pages)
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return ""


def extract_text_from_docx(content: bytes) -> str:
    """Extract text from DOCX bytes."""
    try:
        doc = DocxDocument(BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]

        tables_text = []
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(
                    cell.text.strip() for cell in row.cells if cell.text.strip()
                )
                if row_text:
                    tables_text.append(row_text)

        return "\n\n".join(paragraphs) + "\n\n" + "\n".join(tables_text)
    except Exception as e:
        logger.error(f"DOCX extraction failed: {e}")
        return ""


def extract_text_from_html(html: str) -> str:
    """Extract clean text from HTML."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


async def _fetch_rendered_html(url: str) -> str | None:
    """
    Playwright fallback for individual ToR/opportunity pages that render
    via JS. Same problem monitors/scraper.py already solves for listing
    pages, one layer deeper — the linked document page itself.
    """
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="commit", timeout=45000)
                await page.wait_for_timeout(3000)
                return await page.content()
            except PlaywrightTimeout:
                logger.warning(f"  Playwright timeout loading: {url}")
                return None
            finally:
                await page.close()
                await browser.close()
    except Exception as e:
        logger.error(f"  Playwright fallback failed for {url}: {e}")
        return None


def _fetch_rendered_html_sync(url: str) -> str | None:
    """Sync wrapper — same pattern as scrape_non_rss_sources() in scraper.py."""
    try:
        return asyncio.run(_fetch_rendered_html(url))
    except Exception as e:
        logger.error(f"  Browser fallback crashed for {url}: {e}")
        return None


def fetch_and_extract(url: str, opportunity_id: str = None) -> str:
    """
    Main function: fetch URL, detect type, extract text.
    Falls back to a Playwright-rendered page when the plain httpx result
    is too thin to be a real ToR — the storage side-effect is isolated
    from the extraction result so a Supabase hiccup can never discard a
    successful extraction.
    """
    text = ""
    file_type = "html"
    content = b""

    try:
        content = download_document(url)
        if url.lower().endswith(".pdf") or content[:4] == b"%PDF":
            text = extract_text_from_pdf(content)
            file_type = "pdf"
        elif url.lower().endswith(".docx"):
            text = extract_text_from_docx(content)
            file_type = "docx"
        else:
            text = extract_text_from_html(content.decode("utf-8", errors="ignore"))
            file_type = "html"
    except Exception as e:
        logger.warning(f"  httpx fetch/extract failed for {url}: {e} — will try Playwright")

    # ── PLAYWRIGHT FALLBACK ────────────────────────────────────────────
    # Only for HTML — a genuine PDF/DOCX either extracted or truly failed;
    # a browser won't fix a bad PDF.
    if file_type == "html" and len(text) < MIN_USEFUL_CHARS:
        logger.info(f"  httpx extraction thin ({len(text)} chars) — trying Playwright: {url[:60]}")
        rendered_html = _fetch_rendered_html_sync(url)
        if rendered_html:
            browser_text = extract_text_from_html(rendered_html)
            if len(browser_text) > len(text):
                text = browser_text
                logger.success(f"  Playwright fallback recovered {len(text)} chars: {url[:60]}")

    # ── STORE IN SUPABASE — isolated, never discards a good extraction ──
    if opportunity_id and file_type in ["pdf", "docx"] and text and content:
        try:
            file_name = url.split("/")[-1] or f"document.{file_type}"
            store_document(opportunity_id, file_name, content, file_type)
        except Exception as e:
            logger.warning(f"  Supabase document store failed (non-fatal): {e}")

    if text:
        logger.success(f"Extracted {len(text)} chars from {file_type}: {url[:60]}...")
    else:
        logger.warning(f"No text extracted for {url[:60]}...")
    return text
