"""Playwright resource limits are flags/timeouts, not an OS sandbox."""
import asyncio

from utils.browser_security import _browser_launch_args, prepare_browser_page


def test_chromium_launch_args_include_heap_and_process_caps():
    args = _browser_launch_args()
    assert "--no-sandbox" in args
    assert "--disable-dev-shm-usage" in args
    assert "--renderer-process-limit=1" in args
    assert any(a.startswith("--js-flags=--max-old-space-size=") for a in args)


def test_prepare_browser_page_sets_timeouts():
    recorded = {}

    class _Page:
        async def route(self, pattern, handler):
            recorded["routed"] = pattern

        def set_default_timeout(self, ms):
            recorded["timeout"] = ms

        def set_default_navigation_timeout(self, ms):
            recorded["nav_timeout"] = ms

    asyncio.run(prepare_browser_page(_Page()))
    assert recorded["routed"] == "**/*"
    assert recorded["timeout"] >= 1000
    assert recorded["nav_timeout"] == recorded["timeout"]
