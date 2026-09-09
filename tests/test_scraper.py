import asyncio
import os

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
