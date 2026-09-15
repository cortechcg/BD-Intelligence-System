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
