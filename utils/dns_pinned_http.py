"""DNS-pinned HTTP GET for the untrusted download path.

Resolve the hostname once, reject any non-public address, connect to that
IP, and present the original hostname for Host / SNI. The HTTP client must
not perform a second DNS lookup between the safety check and connect
(DNS rebinding TOCTOU).
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

from utils.urls import (
    UnsafeURLError,
    _ip_is_public,
    assert_public_http_url,
    resolve_host_addresses,
)


@dataclass(frozen=True)
class PinnedTarget:
    url: str
    scheme: str
    hostname: str
    ip: str
    port: int
    request_path: str
    host_header: str


@dataclass
class PinnedResponse:
    status_code: int
    headers: dict
    body: bytes
    pinned_ip: str
    url: str


def pin_public_http_target(url: str, *, resolver=None) -> PinnedTarget:
    """Resolve once and pick a single public IP. Reject mixed/private answers."""
    raw = assert_public_http_url(url, resolve=False)
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise UnsafeURLError("empty host")

    try:
        literal = ipaddress.ip_address(hostname)
        addresses = {literal}
    except ValueError:
        resolve = resolver or resolve_host_addresses
        addresses = set(resolve(hostname))

    public = [addr for addr in addresses if _ip_is_public(addr)]
    if not public or len(public) != len(addresses):
        raise UnsafeURLError(f"blocked host: {hostname}")

    ipv4 = [addr for addr in public if addr.version == 4]
    chosen = sorted(ipv4 or public, key=lambda addr: int(addr))[0]
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    host_header = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
    return PinnedTarget(
        url=raw,
        scheme=scheme,
        hostname=hostname,
        ip=str(chosen),
        port=int(port),
        request_path=path,
        host_header=host_header,
    )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, port: int, pinned_ip: str, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        port: int,
        pinned_ip: str,
        timeout: float,
        context: ssl.SSLContext,
    ):
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def exchange(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = 60.0,
    max_bytes: int | None = None,
    resolver=None,
) -> PinnedResponse:
    """One GET against the pinned IP. Redirects are not followed."""
    pin = pin_public_http_target(url, resolver=resolver)
    request_headers = dict(headers or {})
    request_headers.setdefault("Host", pin.host_header)
    request_headers.setdefault("Connection", "close")

    if pin.scheme == "https":
        context = ssl.create_default_context()
        conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
            pin.hostname, pin.port, pin.ip, timeout, context
        )
    elif pin.scheme == "http":
        conn = _PinnedHTTPConnection(pin.hostname, pin.port, pin.ip, timeout)
    else:
        raise UnsafeURLError(f"blocked scheme: {pin.scheme}")

    try:
        conn.request("GET", pin.request_path, headers=request_headers)
        response = conn.getresponse()
        header_map = {k.lower(): v for k, v in response.getheaders()}
        declared = header_map.get("content-length")
        if declared and max_bytes is not None:
            try:
                if int(declared) > max_bytes:
                    raise ValueError(
                        f"Document exceeds {max_bytes} byte limit "
                        f"(declared {declared} bytes)"
                    )
            except ValueError as exc:
                if "exceeds" in str(exc):
                    raise
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError(
                    f"Document exceeds {max_bytes} byte limit while streaming"
                )
            chunks.append(chunk)
        return PinnedResponse(
            status_code=int(response.status),
            headers=header_map,
            body=b"".join(chunks),
            pinned_ip=pin.ip,
            url=pin.url,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
