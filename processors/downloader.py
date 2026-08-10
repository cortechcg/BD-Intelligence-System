# processors/downloader.py
import asyncio
import httpx
import pdfplumber
from docx import Document as DocxDocument
from io import BytesIO
from loguru import logger
from database.supabase_client import store_document
from urllib.parse import urljoin
import re

MIN_USEFUL_CHARS = 200  # matches main.py's own "insufficient text" threshold

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _is_ssl_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "certificate" in msg or "ssl" in msg


def _looks_like_pdf_url(url: str) -> bool:
    return url.lower().split("?")[0].split("#")[0].endswith(".pdf")


def download_document(url: str) -> bytes:
    """Download a document from URL, retrying without SSL verify on bad certs."""
    last_error: Exception | None = None
    for verify in (True, False):
        try:
            response = httpx.get(
                url,
                headers=HTTP_HEADERS,
                follow_redirects=True,
                timeout=60,
                verify=verify,
            )
            response.raise_for_status()
            if not verify:
                logger.warning(
                    f"  Downloaded with SSL verification disabled: {url[:80]}"
                )
            return response.content
        except Exception as e:
            last_error = e
            if verify and _is_ssl_error(e):
                logger.warning(
                    f"  SSL verify failed for {url[:60]} — retrying without verification"
                )
                continue
            logger.error(f"Download failed for {url}: {e}")
            raise
    assert last_error is not None
    logger.error(f"Download failed for {url}: {last_error}")
    raise last_error


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


def _find_pdf_links(html: str, base_url: str) -> list[str]:
    """Collect PDF hrefs from a rendered page — common on WordPress tender posts."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if ".pdf" not in href.lower():
            continue
        absolute = urljoin(base_url, href)
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
    return links


def _extract_from_pdf_url(pdf_url: str) -> tuple[str, bytes]:
    """Download and extract text from a direct PDF link."""
    content = download_document(pdf_url)
    return extract_text_from_pdf(content), content


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
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(5000)
                return await page.content()
            except PlaywrightTimeout:
                logger.warning(f"  Playwright timeout loading: {url}")
                return None
            finally:
                await page.close()
                await context.close()
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
    is_pdf_url = _looks_like_pdf_url(url)

    try:
        content = download_document(url)
        if is_pdf_url or content[:4] == b"%PDF":
            text = extract_text_from_pdf(content)
            file_type = "pdf"
        elif url.lower().split("?")[0].endswith(".docx"):
            text = extract_text_from_docx(content)
            file_type = "docx"
        else:
            text = extract_text_from_html(content.decode("utf-8", errors="ignore"))
            file_type = "html"
    except Exception as e:
        if is_pdf_url:
            logger.error(f"  PDF download failed for {url}: {e}")
        else:
            logger.warning(f"  httpx fetch/extract failed for {url}: {e} — will try Playwright")

    # ── PLAYWRIGHT FALLBACK (HTML pages only — never for direct PDF links) ─
    rendered_html: str | None = None
    if file_type == "html" and not is_pdf_url and len(text) < MIN_USEFUL_CHARS:
        logger.info(
            f"  httpx extraction thin ({len(text)} chars) — trying Playwright: {url[:60]}"
        )
        rendered_html = _fetch_rendered_html_sync(url)
        if rendered_html:
            browser_text = extract_text_from_html(rendered_html)
            if len(browser_text) > len(text):
                text = browser_text
                logger.success(
                    f"  Playwright fallback recovered {len(text)} chars: {url[:60]}"
                )

    # ── PDF LINK FOLLOW — WordPress posts often only link to the ToR PDF ───
    html_source = rendered_html or (
        content.decode("utf-8", errors="ignore") if content else ""
    )
    if file_type == "html" and len(text) < MIN_USEFUL_CHARS and html_source:
        for pdf_url in _find_pdf_links(html_source, url):
            logger.info(f"  Following PDF link from page: {pdf_url[:70]}")
            try:
                pdf_text, pdf_content = _extract_from_pdf_url(pdf_url)
            except Exception as e:
                logger.warning(f"  PDF link fetch failed ({pdf_url[:60]}): {e}")
                continue
            if len(pdf_text) >= MIN_USEFUL_CHARS:
                text = pdf_text
                content = pdf_content
                file_type = "pdf"
                url = pdf_url  # store under the PDF filename
                logger.success(
                    f"  Recovered {len(text)} chars from linked PDF: {pdf_url[:60]}"
                )
                break

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
