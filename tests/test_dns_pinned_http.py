"""DNS-pinned download transport. Network-free."""
from __future__ import annotations

import ipaddress
import socket

import pytest

from processors import downloader
from utils import dns_pinned_http
from utils.dns_pinned_http import PinnedResponse, pin_public_http_target
from utils.urls import UnsafeURLError


def test_pin_rejects_private_or_mixed_answers(monkeypatch):
    monkeypatch.setattr(
        dns_pinned_http,
        "resolve_host_addresses",
        lambda host: {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("93.184.216.34")},
    )
    with pytest.raises(UnsafeURLError):
        pin_public_http_target("https://procurement.example/tender")


def test_pin_picks_one_public_ip_and_exchange_connects_there(monkeypatch):
    resolves = []

    def resolver(host):
        resolves.append(host)
        if len(resolves) > 1:
            return {ipaddress.ip_address("127.0.0.1")}
        return {ipaddress.ip_address("93.184.216.34")}

    connected = []

    def fake_connect(address, timeout=None):
        connected.append((address, timeout))
        raise OSError("stop-after-pin")

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    with pytest.raises(OSError, match="stop-after-pin"):
        dns_pinned_http.exchange(
            "https://procurement.example/tender",
            timeout=5,
            resolver=resolver,
        )
    assert connected == [(("93.184.216.34", 443), 5)]
    assert resolves == ["procurement.example"]


def test_public_ipv6_pin_formats_the_http_authority_with_brackets():
    pin = pin_public_http_target("https://[2606:4700:4700::1111]/notices")
    assert pin.ip == "2606:4700:4700::1111"
    assert pin.host_header == "[2606:4700:4700::1111]"


def test_download_uses_pinned_exchange_per_hop(monkeypatch):
    monkeypatch.setattr(
        "utils.urls.resolve_host_addresses",
        lambda host: {ipaddress.ip_address("93.184.216.34")},
    )
    hops = []

    def fake_exchange(url, **kwargs):
        hops.append(url)
        if url.endswith("/tender"):
            return PinnedResponse(
                status_code=302,
                headers={"location": "/files/tor.pdf"},
                body=b"",
                pinned_ip="93.184.216.34",
                url=url,
            )
        return PinnedResponse(
            status_code=200,
            headers={"content-length": "5"},
            body=b"hello",
            pinned_ip="93.184.216.34",
            url=url,
        )

    monkeypatch.setattr(downloader, "dns_pinned_exchange", fake_exchange)
    assert downloader.download_document("https://procurement.example/tender") == b"hello"
    assert hops == [
        "https://procurement.example/tender",
        "https://procurement.example/files/tor.pdf",
    ]


def test_download_blocks_private_redirect_before_second_exchange(monkeypatch):
    monkeypatch.setattr(
        "utils.urls.resolve_host_addresses",
        lambda host: {ipaddress.ip_address("93.184.216.34")},
    )
    hops = []

    def fake_exchange(url, **kwargs):
        hops.append(url)
        return PinnedResponse(
            status_code=302,
            headers={"location": "http://127.0.0.1/latest/meta-data"},
            body=b"",
            pinned_ip="93.184.216.34",
            url=url,
        )

    monkeypatch.setattr(downloader, "dns_pinned_exchange", fake_exchange)
    with pytest.raises(UnsafeURLError):
        downloader.download_document("https://procurement.example/tender")
    assert hops == ["https://procurement.example/tender"]


def test_download_rejects_private_dns_rebinding_on_a_redirect(monkeypatch):
    target_resolutions = 0

    def resolver(host):
        nonlocal target_resolutions
        if host == "files.example":
            target_resolutions += 1
            address = "93.184.216.34" if target_resolutions == 1 else "127.0.0.1"
        else:
            address = "93.184.216.34"
        return {ipaddress.ip_address(address)}

    monkeypatch.setattr("utils.urls.resolve_host_addresses", resolver)
    monkeypatch.setattr(dns_pinned_http, "resolve_host_addresses", resolver)
    hops = []

    def fake_exchange(url, **kwargs):
        hops.append(url)
        # Exercise the same DNS pin check that precedes a real socket connect.
        dns_pinned_http.pin_public_http_target(url)
        if url.endswith("/tender"):
            return PinnedResponse(
                status_code=302,
                headers={"location": "https://files.example/tor.pdf"},
                body=b"",
                pinned_ip="93.184.216.34",
                url=url,
            )
        return PinnedResponse(
            status_code=200, headers={}, body=b"pdf", pinned_ip="93.184.216.34", url=url
        )

    monkeypatch.setattr(downloader, "dns_pinned_exchange", fake_exchange)
    with pytest.raises(UnsafeURLError):
        downloader.download_document("https://procurement.example/tender")
    assert hops == [
        "https://procurement.example/tender",
        "https://files.example/tor.pdf",
    ]


@pytest.mark.parametrize(("allow_browser", "expected_calls"), [(False, 0), (True, 1)])
def test_browser_fallback_is_disabled_only_for_configured_sources(
    monkeypatch, allow_browser, expected_calls
):
    monkeypatch.setattr(
        "utils.urls.resolve_host_addresses",
        lambda _host: {ipaddress.ip_address("93.184.216.34")},
    )
    monkeypatch.setattr(
        downloader,
        "download_document",
        lambda _url: b"<html><body>Public tender notice</body></html>",
    )
    monkeypatch.setattr(downloader, "MIN_USEFUL_CHARS", 1000)
    monkeypatch.setattr(downloader, "assess_extraction", lambda *_a, **_kw: {"ok": True})
    browser_calls = []

    def rendered(_url):
        browser_calls.append(True)
        return "<html><body>Expanded tender notice content</body></html>"

    monkeypatch.setattr(downloader, "_fetch_rendered_html_sync", rendered)
    text = downloader.fetch_and_extract(
        "https://procurement.example/notice",
        allow_browser_fallback=allow_browser,
    )
    assert text
    assert len(browser_calls) == expected_calls
