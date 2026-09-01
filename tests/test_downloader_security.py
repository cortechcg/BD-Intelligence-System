import pytest

from processors import downloader
from utils.urls import UnsafeURLError


class _Response:
    def __init__(self, status_code, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self):
        yield from self._chunks


class _Client:
    responses = []
    requested_urls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url, headers):
        self.requested_urls.append(url)
        return self.responses.pop(0)


def _install_client(monkeypatch, responses):
    _Client.responses = list(responses)
    _Client.requested_urls = []
    monkeypatch.setattr(downloader.httpx, "Client", _Client)


def test_download_checks_each_redirect_before_requesting_it(monkeypatch):
    _install_client(
        monkeypatch,
        [_Response(302, {"location": "http://127.0.0.1/latest/meta-data"})],
    )

    with pytest.raises(UnsafeURLError):
        downloader.download_document("https://procurement.example/tender")

    assert _Client.requested_urls == ["https://procurement.example/tender"]


def test_download_allows_checked_public_redirect(monkeypatch):
    _install_client(
        monkeypatch,
        [
            _Response(302, {"location": "/files/tor.pdf"}),
            _Response(200, {"content-length": "5"}, [b"hello"]),
        ],
    )

    assert downloader.download_document("https://procurement.example/tender") == b"hello"
    assert _Client.requested_urls == [
        "https://procurement.example/tender",
        "https://procurement.example/files/tor.pdf",
    ]


def test_download_stops_when_stream_exceeds_configured_limit(monkeypatch):
    _install_client(monkeypatch, [_Response(200, {}, [b"abc", b"def"])])
    monkeypatch.setattr(downloader, "MAX_DOCUMENT_BYTES", 5)

    with pytest.raises(ValueError, match="exceeds 5 byte limit"):
        downloader.download_document("https://procurement.example/tender")


def test_download_retries_transient_network_failure_with_bounded_backoff(monkeypatch):
    attempts = []
    sleeps = []

    def once(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise downloader.httpx.ConnectError("connection reset")
        return b"document"

    monkeypatch.setattr(downloader, "_download_document_once", once)
    monkeypatch.setattr(downloader, "DOCUMENT_DOWNLOAD_MAX_RETRIES", 2)
    monkeypatch.setattr(downloader.time, "sleep", sleeps.append)

    assert downloader.download_document("https://procurement.example/tender") == b"document"
    assert len(attempts) == 2
    assert sleeps == [1]


def test_download_does_not_retry_invalid_or_oversized_input(monkeypatch):
    attempts = []
    monkeypatch.setattr(
        downloader,
        "_download_document_once",
        lambda url: attempts.append(url) or (_ for _ in ()).throw(ValueError("too large")),
    )
    monkeypatch.setattr(downloader, "DOCUMENT_DOWNLOAD_MAX_RETRIES", 2)
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: pytest.fail("must not sleep"))

    with pytest.raises(ValueError, match="too large"):
        downloader.download_document("https://procurement.example/tender")
    assert len(attempts) == 1
