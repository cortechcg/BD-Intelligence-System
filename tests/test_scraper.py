import asyncio

from monitors.scraper import parse_tenders_from_html, scrape_one_source, SCRAPE_SOURCES
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


def test_scrape_one_source_swallows_failures():
    async def boom(_browser, _source):
        return await scrape_one_source(_browser, {"name": "Broken"})

    class ExplodingBrowser:
        pass

    result = asyncio.run(boom(ExplodingBrowser(), None))
    assert result == []


def test_launch_chromium_falls_back_to_system_chrome():
    class _Chromium:
        def __init__(self):
            self.attempts = []

        async def launch(self, **kwargs):
            self.attempts.append(kwargs.get("channel", "bundled"))
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
    assert attempts[0] == "bundled"
    assert "chrome" in attempts
