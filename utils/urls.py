"""URL canonicalization and fetch-time SSRF guards."""

from __future__ import annotations

import ipaddress
import re
import socket
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

_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.google.com",
    "instance-data",
    "instance-data.ec2.internal",
}
_BLOCKED_IPS = {
    ipaddress.ip_address("168.63.129.16"),  # Azure platform / WireServer address
}
_BLOCKED_IPV6_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),   # Well-known NAT64 prefix
    ipaddress.ip_network("64:ff9b:1::/48"),  # Local-use NAT64 prefix
    ipaddress.ip_network("2002::/16"),      # 6to4 embeds an IPv4 destination
)


class UnsafeURLError(ValueError):
    error_type = ErrorType.SSRF_ERROR


def canonicalize_url(
    url: str,
    *,
    preserve_query_keys: set[str] | frozenset[str] = frozenset(),
) -> str:
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

    # urlparse.hostname omits the brackets required around IPv6 literals in
    # an authority. Preserve a valid authority when canonicalizing an allowed
    # public IPv6 URL.
    try:
        is_ipv6 = ipaddress.ip_address(host).version == 6
    except ValueError:
        is_ipv6 = False
    authority_host = f"[{host}]" if is_ipv6 else host

    port = parsed.port
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{authority_host}:{port}"
    else:
        netloc = authority_host

    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = ""

    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lk = key.lower()
        if lk in TRACKING_QUERY_KEYS and lk not in preserve_query_keys:
            continue
        if any(lk.startswith(p) for p in TRACKING_QUERY_PREFIXES) and lk not in preserve_query_keys:
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


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    """Return whether an address is globally routable enough to fetch.

    ``is_private`` alone is not sufficient: it misses multicast, unspecified,
    documentation, carrier-grade NAT, and several IPv4-mapped IPv6 forms on
    different Python versions. A fetcher has no business connecting to any
    address that the standard library does not classify as global.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _ip_is_public(mapped)
    if ip in _BLOCKED_IPS:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and any(
        ip in network for network in _BLOCKED_IPV6_NETWORKS
    ):
        return False
    return bool(ip.is_global)


def resolve_host_addresses(host: str) -> set[ipaddress._BaseAddress]:
    """Resolve a hostname for fetch-time SSRF validation.

    This is intentionally a small, injectable boundary: tests can provide a
    deterministic resolver and all network fetchers use the same policy. DNS
    answers are checked immediately before each request/navigation, reducing
    (but not fully eliminating) DNS-rebinding TOCTOU risk.
    """
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"could not resolve host: {host}") from exc

    addresses: set[ipaddress._BaseAddress] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in results:
        try:
            addresses.add(ipaddress.ip_address(sockaddr[0]))
        except ValueError as exc:
            raise UnsafeURLError(f"resolver returned invalid address for {host}") from exc
    if not addresses:
        raise UnsafeURLError(f"host resolved to no addresses: {host}")
    return addresses


def _host_is_blocked(host: str, *, resolve: bool = False) -> bool:
    if not host:
        return True
    if host in _BLOCKED_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if not resolve:
            return False
        return any(not _ip_is_public(ip) for ip in resolve_host_addresses(host))
    return not _ip_is_public(ip)


def assert_public_http_url(url: str, *, resolve: bool = False) -> str:
    """Raise UnsafeURLError if this URL must not be fetched by the downloader.

    With ``resolve=True`` the current DNS answers are also checked before a
    fetch/navigation. Fetchers must use that mode for every hop. This cannot
    fully pin a later HTTP-client/browser lookup against DNS rebinding, but it
    blocks literal, numeric-alias, and currently-resolved private destinations.
    """
    raw = (url or "").strip()
    if not raw:
        raise UnsafeURLError("empty URL")

    try:
        parsed = urlparse(raw)
        # Accessing .port validates malformed/non-numeric port syntax.
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("malformed URL port") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise UnsafeURLError(f"blocked scheme: {scheme or 'none'}")

    host = (parsed.hostname or "").lower()
    if _host_is_blocked(host, resolve=resolve):
        raise UnsafeURLError(f"blocked host: {host}")

    # Procurement feeds and public listings need only the standard web
    # ports. Blocking alternate ports also prevents use of URL fetches as a
    # probe against public services listening on administration ports.
    expected_port = 80 if scheme == "http" else 443
    if port is not None and port != expected_port:
        raise UnsafeURLError("blocked non-standard port")

    # Userinfo in a tender URL is unusual and is a credential-smuggling vector.
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UnsafeURLError("URL must not contain userinfo")

    return raw


def assert_safe_redirect(
    source_url: str,
    target_url: str,
    *,
    resolve: bool = True,
) -> str:
    """Validate a redirect target and reject HTTPS-to-HTTP downgrade hops."""
    try:
        source_scheme = (urlparse((source_url or "").strip()).scheme or "").lower()
        target_scheme = (urlparse((target_url or "").strip()).scheme or "").lower()
    except ValueError as exc:
        raise UnsafeURLError("malformed redirect URL") from exc
    if source_scheme == "https" and target_scheme == "http":
        raise UnsafeURLError("blocked HTTPS-to-HTTP redirect downgrade")
    return assert_public_http_url(target_url, resolve=resolve)


def safe_url_log_label(url: str) -> str:
    """Return a host-only URL label for logs; omit paths, queries, and secrets."""
    try:
        parsed = urlparse((url or "").strip())
        host = parsed.hostname or "unknown-host"
        try:
            ip = ipaddress.ip_address(host)
            if ip.version == 6:
                host = f"[{host}]"
        except ValueError:
            pass
        port = parsed.port
        authority = host if port is None else f"{host}:{port}"
        scheme = (parsed.scheme or "https").lower()
        return f"{scheme}://{authority}"
    except (TypeError, ValueError):
        return "unknown-host"


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, default: str = "document.bin") -> str:
    """Basename only — blocks path traversal into storage keys."""
    base = (name or "").replace("\\", "/").split("/")[-1]
    if not base or base in {".", ".."}:
        return default
    cleaned = _UNSAFE_FILENAME.sub("_", base).strip("._")
    return cleaned or default
