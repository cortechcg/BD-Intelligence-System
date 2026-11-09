#!/usr/bin/env python3
"""Capture local dashboard pages with synthetic data and a local-only session.

The fixture app is provided by dashboard/design/ui_harness.py. It replaces the
dashboard data functions in memory and does not connect to Supabase.

    .venv/bin/python scripts/visual_check.py
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The UI harness overwrites these with non-production values before importing
# the dashboard. The session below is signed with that same local-only secret.
from dashboard.design import ui_harness

os.environ["DASHBOARD_SESSION_SECRET"] = "ui-harness-secret-not-real"
os.environ["DASHBOARD_COOKIE_SECURE"] = "false"

PAGES = [
    ("login", "/auth/login", False),
    ("pipeline", "/", True),
    ("live-tenders", "/live-tenders", True),
    ("sources", "/sources", True),
    ("opportunity-detail", "/opportunity?url=" + quote(ui_harness.URL_SCORED, safe=""), True),
    ("review", "/opportunity?url=" + quote(ui_harness.URL_DRAFTED, safe="") + "#stage-5-review", True),
    ("portfolio", "/portfolio", True),
    ("library", "/library", True),
    ("track-record", "/track-record", True),
    ("access-denied", "/auth/denied-preview", True),
]
VIEWPORTS = ((1440, 900), (390, 844))


def wait_for_server(base_url: str, proc: subprocess.Popen) -> None:
    for _ in range(60):
        if proc.poll() is not None:
            raise RuntimeError(f"UI fixture server exited with status {proc.returncode}")
        try:
            with urllib.request.urlopen(base_url + "/healthz", timeout=1):
                return
        except Exception:
            time.sleep(0.25)
    raise TimeoutError("UI fixture server did not become ready")


def check_live_page(page) -> None:
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    page.route(
        "**/api/live-tenders*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "items": [],
                "next_cursor": None,
                "checked_at": checked_at,
            }),
        ),
    )
    rows = page.locator("#live-list > li")
    assert rows.count() >= 4, "expected synthetic procurement rows plus a manual link"
    first = rows.first
    assert "SAMPLE: County water services brief" in first.inner_text(), "initial feed is not newest-first"
    assert first.locator(".live-manual-badge").inner_text() == "Manually added"
    refreshed = page.locator("#live-last-refresh")
    with page.expect_response(lambda item: "/api/live-tenders" in item.url, timeout=12000) as response_info:
        # Force a visible refresh now so both viewport screenshots exercise the
        # successful zero-new-rows polling path without waiting for its timer.
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    response = response_info.value
    assert response.ok, f"live feed poll failed with HTTP {response.status}"
    page.wait_for_function(
        "expected => { const n = document.querySelector('#live-last-refresh'); "
        "return n && n.dateTime === new Date(expected).toISOString(); }",
        arg=checked_at,
        timeout=3000,
    )
    local_label = page.evaluate("value => new Date(value).toLocaleString(undefined, "
                                "{year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})", checked_at)
    assert refreshed.inner_text() == "Last refresh " + local_label, "refresh timestamp was not localized after polling"
    assert "UTC" not in refreshed.inner_text(), "refresh timestamp is still labelled in UTC"


def check_mobile_layout(page, page_name: str) -> None:
    if page_name in ("login", "access-denied"):
        return
    dimensions = page.evaluate("""() => ({
        viewport: document.documentElement.clientWidth,
        document: document.documentElement.scrollWidth,
        body: document.body.scrollWidth,
        navDisplay: (() => { const e = document.querySelector('.sidenav .nav'); return e ? getComputedStyle(e).display : 'missing'; })(),
        hiddenRail: (() => { const e = document.querySelector('.sidenav__section'); return e ? getComputedStyle(e).display : 'none'; })(),
        hiddenFoot: (() => { const e = document.querySelector('.sidenav__foot'); return e ? getComputedStyle(e).display : 'none'; })()
    })""")
    assert dimensions["document"] <= dimensions["viewport"], f"{page_name} has horizontal document overflow: {dimensions}"
    assert dimensions["body"] <= dimensions["viewport"], f"{page_name} body overflows horizontally: {dimensions}"
    assert dimensions["navDisplay"] == "flex", f"{page_name} mobile navigation did not collapse to the compact row: {dimensions}"
    assert dimensions["hiddenRail"] == "none" and dimensions["hiddenFoot"] == "none", f"{page_name} mobile sidebar details remained expanded: {dimensions}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "screenshots")
    parser.add_argument("--only", help="comma-separated page names to capture")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    selected = set(args.only.split(",")) if args.only else None

    from dashboard import auth, settings
    settings.SESSION_SECRET = os.environ["DASHBOARD_SESSION_SECRET"]
    token = auth.issue_session("staff@cortechconsultinggroup.com")

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "dashboard/design/ui_harness.py"), "serve", str(args.port)],
        cwd=ROOT,
        env={**os.environ, "DASHBOARD_SESSION_SECRET": "ui-harness-secret-not-real", "DASHBOARD_COOKIE_SECURE": "false"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{args.port}"
    try:
        wait_for_server(base_url, proc)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            # Playwright's bundled Chromium installer does not recognize the
            # current Ubuntu 26.04 platform; use the installed Google Chrome,
            # which exposes the same Chromium engine through Playwright.
            browser = playwright.chromium.launch(headless=True, channel="chrome")
            try:
                for width, height in VIEWPORTS:
                    context = browser.new_context(
                        viewport={"width": width, "height": height},
                        device_scale_factor=1,
                        timezone_id="Africa/Nairobi",
                        reduced_motion="reduce",
                    )
                    context.add_cookies([{
                        "name": settings.SESSION_COOKIE_NAME,
                        "value": token,
                        "url": base_url,
                    }])
                    try:
                        for name, path, signed_in in PAGES:
                            if selected and name not in selected:
                                continue
                            page_context = context if signed_in else browser.new_context(
                                viewport={"width": width, "height": height},
                                device_scale_factor=1,
                                timezone_id="Africa/Nairobi",
                                reduced_motion="reduce",
                            )
                            page = page_context.new_page()
                            try:
                                page.goto(base_url + path, wait_until="networkidle", timeout=30000)
                                page.evaluate("document.fonts.ready")
                                page.add_style_tag(content="*{scroll-behavior:auto!important}")
                                ui_harness._settle(page)
                                if name == "live-tenders":
                                    check_live_page(page)
                                if name == "review":
                                    review = page.locator("#stage-5-review")
                                    review.scroll_into_view_if_needed()
                                    assert review.is_visible(), "review gate did not render"
                                else:
                                    page.evaluate("window.scrollTo(0, 0)")
                                page.evaluate("if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur()")
                                if width == 390:
                                    check_mobile_layout(page, name)
                                target = args.output / f"{name}-{width}x{height}.png"
                                page.screenshot(path=str(target), full_page=False, animations="disabled")
                                print(f"{target.relative_to(ROOT)}")
                                if name == "portfolio":
                                    chart = page.locator(".section .panel__chart").first
                                    chart.scroll_into_view_if_needed()
                                    fills = page.locator(".bar__fill--agent")
                                    assert fills.count() > 0, "portfolio stage chart has no sage bars"
                                    assert fills.first.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(127, 207, 160)"
                                    chart_target = args.output / f"portfolio-chart-{width}x{height}.png"
                                    page.screenshot(path=str(chart_target), full_page=False, animations="disabled")
                                    print(f"{chart_target.relative_to(ROOT)}")
                                if name == "track-record":
                                    chart = page.locator("#years")
                                    chart.scroll_into_view_if_needed()
                                    dots = page.locator(".tr-tl__sq i")
                                    assert dots.count() > 0, "track record timeline has no assignment marks"
                                    assert dots.first.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(79, 143, 122)"
                                    chart_target = args.output / f"track-record-chart-{width}x{height}.png"
                                    page.screenshot(path=str(chart_target), full_page=False, animations="disabled")
                                    print(f"{chart_target.relative_to(ROOT)}")
                            finally:
                                page.close()
                                if not signed_in:
                                    page_context.close()
                    finally:
                        context.close()
                browser.close()
            except Exception:
                browser.close()
                raise
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
