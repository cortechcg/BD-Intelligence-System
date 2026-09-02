"""Playwright request routing backed by the shared fetch URL policy."""

from __future__ import annotations

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
