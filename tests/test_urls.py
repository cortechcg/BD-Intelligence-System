"""Unit tests — no live Airtable/Anthropic/OpenAI/Supabase calls."""

from utils.urls import (
    UnsafeURLError,
    assert_public_http_url,
    canonicalize_url,
    safe_filename,
    url_identity_keys,
)


def test_strips_tracking_and_slash():
    a = "https://WWW.Example.com/tenders/abc/?utm_source=x&id=9"
    b = "https://example.com/tenders/abc?id=9"
    assert canonicalize_url(a) == canonicalize_url(b)
    assert canonicalize_url(a) == "https://example.com/tenders/abc?id=9"


def test_fragment_and_default_port():
    assert canonicalize_url("https://example.com:443/a/#section") == "https://example.com/a"


def test_identity_keys_include_raw_and_canonical():
    raw = "HTTP://Example.com/foo/"
    keys = url_identity_keys(raw)
    assert raw in keys or raw.strip() in keys
    assert canonicalize_url(raw) in keys


def test_same_listing_different_utm_is_one_key():
    keys_a = set(url_identity_keys("https://portal.test/rfp?id=1&utm_campaign=x"))
    keys_b = set(url_identity_keys("https://portal.test/rfp?id=1"))
    assert keys_a & keys_b


def test_blocks_localhost_and_metadata():
    for url in (
        "http://127.0.0.1/secret",
        "http://localhost/x",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "https://user:pass@example.com/t",
    ):
        try:
            assert_public_http_url(url)
            raise AssertionError(f"should block {url}")
        except UnsafeURLError:
            pass


def test_allows_public_https():
    assert assert_public_http_url("https://www.worldbank.org/en/rfp")


def test_safe_filename_blocks_traversal():
    assert ".." not in safe_filename("../../etc/passwd")
    assert "/" not in safe_filename("a/b/c.pdf")
    assert safe_filename("ToR-final.pdf") == "ToR-final.pdf"
