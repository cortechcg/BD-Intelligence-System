"""Bounded, fail-safe Healthchecks-style completion pings."""

from __future__ import annotations

import httpx
from loguru import logger

from config import HEALTHCHECK_URL
from utils.urls import UnsafeURLError, assert_public_http_url

_TIMEOUT_SECONDS = 10


def _endpoint(failed: bool) -> str | None:
    base = (HEALTHCHECK_URL or "").strip().rstrip("/")
    if not base:
        return None
    endpoint = f"{base}/fail" if failed else base
    try:
        # Monitoring endpoints are configuration, but validating them prevents
        # an accidental local URL from turning a liveness hook into SSRF.
        return assert_public_http_url(endpoint, resolve=True)
    except UnsafeURLError as exc:
        logger.warning(f"Healthcheck URL ignored: {exc}")
        return None


def ping_healthcheck(*, failed: bool = False) -> bool:
    """Send one bounded completion/failure ping; never crash the business run."""
    endpoint = _endpoint(failed)
    if not endpoint:
        return False
    try:
        response = httpx.get(endpoint, timeout=_TIMEOUT_SECONDS, follow_redirects=False)
        if not 200 <= getattr(response, "status_code", 200) < 300:
            raise RuntimeError("healthcheck returned a non-success response")
        response.raise_for_status()
        return True
    except Exception as exc:
        # Do not log the endpoint: many Healthchecks URLs embed a secret UUID.
        logger.warning(f"Healthcheck ping failed ({type(exc).__name__})")
        return False
