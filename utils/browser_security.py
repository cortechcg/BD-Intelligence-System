"""Playwright request routing backed by the shared fetch URL policy."""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from utils.errors import ErrorType
from utils.urls import UnsafeURLError, assert_public_http_url, assert_safe_redirect


async def guard_browser_request(route, request) -> bool:
    """Allow only public HTTP(S) browser requests.

    Playwright follows redirects internally, so checking only ``page.goto`` is
    insufficient. Routing every request covers navigations, redirect hops, and
    subresources which could otherwise be used as an SSRF primitive.
    """
    try:
        previous = getattr(request, "redirected_from", None)
        if previous is not None:
            assert_safe_redirect(previous.url, request.url)
        else:
            assert_public_http_url(request.url, resolve=True)
    except UnsafeURLError as exc:
        logger.warning(
            f"Browser request blocked ({ErrorType.SSRF_ERROR}): {exc}"
        )
        await route.abort()
        return False
    await route.continue_()
    return True


async def install_browser_request_guard(page) -> None:
    """Install the guard before navigation so the initial request is covered."""
    await page.route("**/*", guard_browser_request)


_BROWSER_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]

_SYSTEM_CHROME_CANDIDATES = (
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
)


_BUNDLED_BROWSER_NAMES = frozenset({
    "chrome",
    "chromium",
    "chromium-browser",
    "chrome-headless-shell",
    "headless_shell",
})


def _dir_contains_chromium_binary(root: Path) -> bool:
    """True only if a real Chromium executable exists, not just a version folder."""
    try:
        for child in root.iterdir():
            if not child.is_dir() or "chrom" not in child.name.lower():
                continue
            for candidate in child.rglob("*"):
                name = candidate.name.lower()
                if (
                    candidate.is_file()
                    and name in _BUNDLED_BROWSER_NAMES
                    and os.access(candidate, os.X_OK)
                ):
                    return True
    except OSError:
        return False
    return False


def _playwright_browsers_path_is_usable() -> bool:
    """Cursor often sets PLAYWRIGHT_BROWSERS_PATH to an empty sandbox cache."""
    path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if path is None:
        return True
    if not str(path).strip():
        return False
    root = Path(path)
    if not root.is_dir():
        return False
    return _dir_contains_chromium_binary(root)


def _bundled_chromium_available(playwright) -> bool:
    """Skip the bundled launch when Playwright has no downloaded Chromium.

    A failed bundled launch prints Playwright's 'run playwright install' banner
    to stderr even though Ubuntu 26.04 cannot install that browser. Probe first.
    """
    chromium = getattr(playwright, "chromium", None)
    path = getattr(chromium, "executable_path", None)
    if callable(path):
        try:
            path = path()
        except Exception:
            return False
    path = str(path or "").strip()
    return bool(path) and os.path.isfile(path)


def _sanitize_playwright_browsers_path() -> None:
    """Unset a broken PLAYWRIGHT_BROWSERS_PATH so system Chrome can be found."""
    if _playwright_browsers_path_is_usable():
        return
    logger.warning(
        "PLAYWRIGHT_BROWSERS_PATH is empty or has no Chromium; unsetting it "
        "so Playwright can fall back to system Chrome"
    )
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


def _system_chrome_launch_attempts() -> list[dict]:
    attempts: list[dict] = []
    seen: set[str] = set()
    for exe in _SYSTEM_CHROME_CANDIDATES:
        if exe in seen:
            continue
        if os.path.isfile(exe) and os.access(exe, os.X_OK):
            seen.add(exe)
            attempts.append({"executable_path": exe})
    return attempts


async def launch_chromium(playwright):
    """Launch Chromium, falling back to a system Chrome/Chromium install.

    Playwright's bundled browser is not published for every Linux distro
    (Ubuntu 26.04 currently rejects ``playwright install chromium``). Cursor
    also sets PLAYWRIGHT_BROWSERS_PATH to an empty cache. Unset that, skip
    bundled Chromium when the binary is missing (so Playwright never prints
    the install banner), then use the Chrome/Chromium channels and known
    system binaries so systemd and Cursor both work.
    """
    _sanitize_playwright_browsers_path()
    last_error = None
    attempts: list[dict] = []
    if _bundled_chromium_available(playwright):
        attempts.append({})
    else:
        logger.debug(
            "Playwright bundled Chromium is not installed; using system Chrome"
        )
    attempts.extend((
        {"channel": "chrome"},
        {"channel": "chromium"},
        *_system_chrome_launch_attempts(),
    ))
    for extra in attempts:
        kwargs = {"headless": True, "args": _BROWSER_LAUNCH_ARGS, **extra}
        channel = extra.get("executable_path") or extra.get("channel", "bundled")
        try:
            browser = await playwright.chromium.launch(**kwargs)
            logger.debug(f"Playwright Chromium launched via {channel}")
            return browser
        except Exception as e:
            last_error = e
            # Playwright's missing-browser error includes a multi-line install
            # banner. Keep one short line; the next attempt is system Chrome.
            first_line = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            logger.warning(f"Playwright launch via {channel} failed: {first_line}")
    raise last_error
