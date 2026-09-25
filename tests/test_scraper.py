import asyncio
import os

from monitors.scraper import (
    listings_from_json,
    parse_tenders_from_html,
    scrape_one_source,
    SCRAPE_SOURCES,
)


def _source(name: str) -> dict:
    return next(source for source in SCRAPE_SOURCES if source["name"] == name)
from utils.browser_security import launch_chromium


def test_parse_tenders_empty_html_does_not_crash():
    items = parse_tenders_from_html("", SCRAPE_SOURCES[0])
    assert items == []


def test_parse_tenders_extracts_somalijobs_links():
    html = """
    <html><body>
      <a href="/tenders/mogadishu/123/endline-evaluation-somalia">
        Endline Evaluation of Livelihoods Programme Somalia
      </a>
      <a href="/about">About us</a>
      <a href="/tenders/x/1/ab">short</a>
    </body></html>
    """
    items = parse_tenders_from_html(html, SCRAPE_SOURCES[0])
    assert len(items) == 1
    assert "Endline Evaluation" in items[0]["title"]
    assert "/tenders/" in items[0]["source_url"]


def test_parse_save_the_children_uses_the_card_title():
    html = """
    <div class="three_col-listing-card">
      <h3 class="three_col-listing-card__h3">Endline evaluation of the Somalia education programme</h3>
      <a href="/tenders/somalia-endline-evaluation">Read More</a>
    </div>
    """
    items = parse_tenders_from_html(html, _source("Save the Children tenders"))
    assert len(items) == 1
    assert items[0]["title"].startswith("Endline evaluation")
    assert items[0]["source_url"].endswith("/tenders/somalia-endline-evaluation")


def test_parse_afdb_document_links():
    html = """
    <div class="views-field-title">
      <a href="/en/documents/eoi-kenya-wash-evaluation">EOI - Kenya - WASH evaluation consultancy</a>
    </div>
    <a href="/en/documents/board-documents">Board documents</a>
    """
    items = parse_tenders_from_html(html, _source("African Development Bank procurement"))
    assert len(items) == 1
    assert "Kenya" in items[0]["title"]
    assert "/en/documents/eoi-kenya-wash-evaluation" in items[0]["source_url"]


def test_parse_drc_skips_archived_tenders():
    html = """
    <article class="tenderList__item" data-status="archived">
      <span class="tenderList__item__title">Old fuel supply Ukraine</span>
      <a class="casper" href="/en/tenders/old-fuel/"></a>
    </article>
    <article class="tenderList__item" data-status="current">
      <span class="tenderList__item__title">Final evaluation of the Somalia livelihoods project</span>
      <a class="casper" href="/en/tenders/somalia-evaluation/"></a>
    </article>
    """
    items = parse_tenders_from_html(html, _source("Danish Refugee Council tenders"))
    assert len(items) == 1
    assert "Somalia" in items[0]["title"]
    assert items[0]["source_url"].endswith("/en/tenders/somalia-evaluation")


def test_parse_undp_notice_rows():
    html = """
    <a class="vacanciesTableLink" href="view_notice.cfm?notice_id=42">
      <div class="vacanciesTable__cell__label">Title</div>
      <span>Consultancy for a Kenya health assessment</span>
    </a>
    """
    items = parse_tenders_from_html(html, _source("UNDP procurement notices"))
    assert len(items) == 1
    assert items[0]["title"] == "Consultancy for a Kenya health assessment"
    assert "view_notice.cfm?notice_id=42" in items[0]["source_url"]


def test_parse_ungm_notice_links():
    html = """
    <a href="/Public/Notice">Procurement opportunities</a>
    <a href="/Public/Notice/314510">Evaluation of refugee livelihoods in Kenya</a>
    """
    items = parse_tenders_from_html(html, _source("UNGM procurement notices"))
    assert len(items) == 1
    assert "Kenya" in items[0]["title"]
    assert items[0]["source_url"].endswith("/Public/Notice/314510")


def test_worldbank_and_rfx_json_keep_title_and_link_only():
    bank = listings_from_json(
        {
            "procnotices": [{
                "id": "OP00400152",
                "bid_description": "Feasibility study for electricity interconnection",
                "project_ctry_name": "Kenya",
                "notice_type": "Request for Expression of Interest",
                "contact_email": "hidden@example.com",
            }]
        },
        _source("World Bank procurement notices"),
    )
    assert bank[0]["source_url"].endswith("/procurement-detail/OP00400152")
    assert "Kenya" in bank[0]["summary"]
    assert "hidden@example.com" not in str(bank)

    rfx = listings_from_json(
        {
            "advertisementList": [{
                "id": 7348,
                "procurementTitle": "Climate risk training for financial institutions in Kenya",
                "createdBy": {"userProfile": {"email": "staff@worldbank.org"}},
            }]
        },
        _source("World Bank consultancy EOIs"),
    )
    assert rfx[0]["source_url"].endswith("/advertisement/7348/view.html")
    assert "staff@worldbank.org" not in str(rfx)


def test_scrape_one_source_swallows_failures():
    async def boom(_browser, _source):
        return await scrape_one_source(_browser, {"name": "Broken"})

    class ExplodingBrowser:
        pass

    result = asyncio.run(boom(ExplodingBrowser(), None))
    assert result == []


def test_launch_chromium_skips_missing_bundled_browser():
    class _Chromium:
        executable_path = "/nonexistent/playwright-chrome"

        def __init__(self):
            self.attempts = []

        async def launch(self, **kwargs):
            self.attempts.append(
                kwargs.get("executable_path") or kwargs.get("channel") or "bundled"
            )
            if kwargs.get("channel") != "chrome":
                raise RuntimeError("bundled chromium missing")
            return "ok-browser"

    class _Playwright:
        def __init__(self):
            self.chromium = _Chromium()

    async def run():
        pw = _Playwright()
        browser = await launch_chromium(pw)
        return browser, pw.chromium.attempts

    browser, attempts = asyncio.run(run())
    assert browser == "ok-browser"
    assert attempts == ["chrome"]


def test_launch_chromium_uses_bundled_when_executable_exists(tmp_path):
    bundled = tmp_path / "chrome"
    bundled.write_text("")
    bundled.chmod(0o755)

    class _Chromium:
        executable_path = str(bundled)

        def __init__(self):
            self.attempts = []

        async def launch(self, **kwargs):
            self.attempts.append(
                kwargs.get("executable_path") or kwargs.get("channel") or "bundled"
            )
            if kwargs.get("channel") or kwargs.get("executable_path"):
                raise RuntimeError("should have used bundled Chromium")
            return "bundled-browser"

    class _Playwright:
        def __init__(self):
            self.chromium = _Chromium()

    async def run():
        pw = _Playwright()
        browser = await launch_chromium(pw)
        return browser, pw.chromium.attempts

    browser, attempts = asyncio.run(run())
    assert browser == "bundled-browser"
    assert attempts == ["bundled"]


def test_launch_chromium_unsets_broken_playwright_cache(monkeypatch, tmp_path):
    empty = tmp_path / "empty-playwright-cache"
    empty.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(empty))

    class _Chromium:
        executable_path = "/nonexistent/playwright-chrome"

        async def launch(self, **kwargs):
            if kwargs.get("channel") == "chrome":
                return "ok-browser"
            raise RuntimeError("bundled chromium missing")

    class _Playwright:
        def __init__(self):
            self.chromium = _Chromium()

    browser = asyncio.run(launch_chromium(_Playwright()))
    assert browser == "ok-browser"
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ


def test_launch_chromium_unsets_cache_with_empty_chromium_folder(monkeypatch, tmp_path):
    cache = tmp_path / "playwright-cache"
    (cache / "chromium-1223").mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))

    class _Chromium:
        executable_path = str(cache / "chromium-1223" / "chrome")

        async def launch(self, **kwargs):
            if kwargs.get("channel") == "chrome":
                return "ok-browser"
            raise RuntimeError("bundled chromium missing")

    class _Playwright:
        def __init__(self):
            self.chromium = _Chromium()

    browser = asyncio.run(launch_chromium(_Playwright()))
    assert browser == "ok-browser"
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ
