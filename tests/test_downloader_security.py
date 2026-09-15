import pytest
import ipaddress
from io import BytesIO
from zipfile import ZipFile

from processors import downloader
from utils import urls
from utils.dns_pinned_http import PinnedResponse
from utils.urls import UnsafeURLError


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Keep downloader security tests network-free while exercising DNS policy."""
    monkeypatch.setattr(
        urls,
        "resolve_host_addresses",
        lambda host: {ipaddress.ip_address("93.184.216.34")},
    )


class _Exchange:
    responses = []
    requested_urls = []

    @classmethod
    def call(cls, url, **kwargs):
        cls.requested_urls.append(url)
        return cls.responses.pop(0)


def _install_client(monkeypatch, responses):
    _Exchange.responses = list(responses)
    _Exchange.requested_urls = []
    monkeypatch.setattr(downloader, "dns_pinned_exchange", _Exchange.call)


def _resp(status, headers=None, body=b""):
    if isinstance(body, list):
        body = b"".join(body)
    return PinnedResponse(
        status_code=status,
        headers=headers or {},
        body=body,
        pinned_ip="93.184.216.34",
        url="",
    )


def test_download_checks_each_redirect_before_requesting_it(monkeypatch):
    _install_client(
        monkeypatch,
        [_resp(302, {"location": "http://127.0.0.1/latest/meta-data"})],
    )

    with pytest.raises(UnsafeURLError):
        downloader.download_document("https://procurement.example/tender")

    assert _Exchange.requested_urls == ["https://procurement.example/tender"]


def test_download_allows_checked_public_redirect(monkeypatch):
    _install_client(
        monkeypatch,
        [
            _resp(302, {"location": "/files/tor.pdf"}),
            _resp(200, {"content-length": "5"}, b"hello"),
        ],
    )

    assert downloader.download_document("https://procurement.example/tender") == b"hello"
    assert _Exchange.requested_urls == [
        "https://procurement.example/tender",
        "https://procurement.example/files/tor.pdf",
    ]


def test_download_rejects_https_to_http_redirect_downgrade(monkeypatch):
    _install_client(
        monkeypatch,
        [_resp(302, {"location": "http://procurement.example/insecure"})],
    )

    with pytest.raises(UnsafeURLError, match="downgrade"):
        downloader.download_document("https://procurement.example/tender")

    assert _Exchange.requested_urls == ["https://procurement.example/tender"]


def test_download_stops_when_stream_exceeds_configured_limit(monkeypatch):
    _install_client(monkeypatch, [_resp(200, {}, b"abcdef")])
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


def test_drive_folder_and_html_extraction_obey_resource_caps(monkeypatch):
    folder_html = "".join(
        f'<a href="https://drive.google.com/file/d/id{i}/view">Annex {i}</a>'
        for i in range(3)
    ) + ("x" * 250)
    monkeypatch.setattr(downloader, "download_document", lambda url: folder_html.encode())
    monkeypatch.setattr(downloader, "MAX_GDRIVE_FILES", 2)
    assert len(downloader._list_gdrive_folder_files("folder")) == 2

    monkeypatch.setattr(downloader, "MAX_EXTRACTED_TEXT_CHARS", 10)
    assert len(downloader.extract_text_from_html("<p>abcdefghijklmnopqrstuvwxyz</p>")) == 10


def test_docx_zip_expansion_is_bounded_before_python_docx_parses_it(monkeypatch):
    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr("word/document.xml", "x" * 32)
    monkeypatch.setattr(downloader, "MAX_DOCUMENT_UNCOMPRESSED_BYTES", 16)
    assert downloader.extract_text_from_docx(payload.getvalue()) == ""
