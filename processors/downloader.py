# processors/downloader.py
import httpx
import pdfplumber
from docx import Document as DocxDocument
from io import BytesIO
from loguru import logger
from database.supabase_client import store_document
import re


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

        # Also extract tables
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

    # Remove scripts, styles, nav
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    # Clean up whitespace
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def fetch_and_extract(url: str, opportunity_id: str = None) -> str:
    """
    Main function: fetch URL, detect type, extract text.
    Returns clean extracted text.
    """
    try:
        content = download_document(url)
        content_type = ""

        # Detect type from content or URL
        if url.lower().endswith(".pdf") or content[:4] == b"%PDF":
            text = extract_text_from_pdf(content)
            file_type = "pdf"
        elif url.lower().endswith(".docx"):
            text = extract_text_from_docx(content)
            file_type = "docx"
        else:
            # Assume HTML
            text = extract_text_from_html(content.decode("utf-8", errors="ignore"))
            file_type = "html"

        # Store document in Supabase
        if opportunity_id and file_type in ["pdf", "docx"]:
            file_name = url.split("/")[-1] or f"document.{file_type}"
            store_document(opportunity_id, file_name, content, file_type)

        logger.success(f"Extracted {len(text)} chars from {file_type}: {url[:60]}...")
        return text

    except Exception as e:
        logger.error(f"Failed to fetch/extract {url}: {e}")
        return ""