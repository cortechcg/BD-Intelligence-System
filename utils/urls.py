"""URL canonicalization and fetch-time SSRF guards."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from utils.errors import ErrorType


TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "ref",
    "ref_src",
    "spm",
    "yclid",
    "igshid",
}

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.google.com",
    "instance-data",
}


class UnsafeURLError(ValueError):
    error_type = ErrorType.SSRF_ERROR


def canonicalize_url(url: str) -> str:
    """Deterministic URL key for exact-URL dedup.

    Does not strip tender identifiers (`id`, tokens). Strips tracking
    params, fragments, default ports, trailing slashes, and lowercases
    scheme/host so the same listing is not ingested twice.
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    port = parsed.port
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = ""

    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lk = key.lower()
        if lk in TRACKING_QUERY_KEYS:
            continue
        if any(lk.startswith(p) for p in TRACKING_QUERY_PREFIXES):
            continue
        kept.append((key, value))
    kept.sort(key=lambda kv: (kv[0].lower(), kv[1]))
    query = urlencode(kept, doseq=True)

    return urlunparse((scheme, netloc, path, "", query, ""))


def url_identity_keys(url: str) -> list[str]:
    """Original plus canonical form — both must be checked for dedup."""
    keys: list[str] = []
    raw = (url or "").strip()
    if raw:
        keys.append(raw)
    canon = canonicalize_url(raw)
    if canon and canon not in keys:
        keys.append(canon)
    return keys


def _host_is_blocked(host: str) -> bool:
    if not host:
        return True
    if host in _BLOCKED_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE_NETWORKS)


def assert_public_http_url(url: str) -> str:
    """Raise UnsafeURLError if this URL must not be fetched by the downloader.

    DNS rebinding after this check is not fully solved here (would need
    a pin-the-resolved-IP httpx transport). This blocks the obvious cases:
    non-http schemes, localhost, link-local/metadata, and literal private IPs.
    """
    raw = (url or "").strip()
    if not raw:
        raise UnsafeURLError("empty URL")

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise UnsafeURLError(f"blocked scheme: {scheme or 'none'}")

    host = (parsed.hostname or "").lower()
    if _host_is_blocked(host):
        raise UnsafeURLError(f"blocked host: {host}")

    # Userinfo in a tender URL is unusual and is a credential-smuggling vector.
    if parsed.username or parsed.password:
        raise UnsafeURLError("URL must not contain userinfo")

    return raw


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, default: str = "document.bin") -> str:
    """Basename only — blocks path traversal into storage keys."""
    base = (name or "").replace("\\", "/").split("/")[-1]
    if not base or base in {".", ".."}:
        return default
    cleaned = _UNSAFE_FILENAME.sub("_", base).strip("._")
    return cleaned or default
