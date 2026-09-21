"""Access control for the dashboard.

The brief requires Google OAuth restricted to ``@cortechconsultinggroup.com``
and a test that a non-domain email is rejected. The decision itself lives in
``dashboard.settings.email_allowed`` as a pure function precisely so it can be
tested exhaustively here — without a browser, a Google app, or a network.
"""
from __future__ import annotations

import importlib

import pytest

from dashboard import auth, settings

DOMAIN = "cortechconsultinggroup.com"


@pytest.fixture(autouse=True)
def _clean_allowlist(monkeypatch):
    """Default state: domain-only, no explicit exceptions."""
    monkeypatch.delenv("AUTH_ALLOWED_EMAILS", raising=False)
    monkeypatch.setattr(settings, "AUTH_ALLOWED_DOMAIN", DOMAIN)


# ── the requirement: non-domain emails are rejected ─────────────────────────

@pytest.mark.parametrize(
    "email",
    [
        "someone@gmail.com",
        "sharahbiilabdi2@gmail.com",
        "attacker@evil.com",
        "staff@cortechconsultinggroup.com.evil.com",   # suffix-append attack
        "staff@notcortechconsultinggroup.com",         # prefix-append attack
        "staff@cortechconsultinggroup.co",             # truncated TLD
        "staff@sub.cortechconsultinggroup.com",        # subdomain is not the domain
        "staff@cortechconsultinggroup.com@gmail.com",  # double-@ confusion
    ],
)
def test_non_domain_email_is_rejected(email):
    allowed, reason = settings.email_allowed(email, email_verified=True)
    assert allowed is False, f"{email} must not be granted access"
    assert DOMAIN in reason


@pytest.mark.parametrize(
    "email",
    [
        "staff@cortechconsultinggroup.com",
        "Staff@CortechConsultingGroup.com",   # case-insensitive
        "  staff@cortechconsultinggroup.com ",  # surrounding whitespace
    ],
)
def test_domain_email_is_accepted(email):
    allowed, reason = settings.email_allowed(email, email_verified=True)
    assert allowed is True
    assert reason == f"@{DOMAIN} account"


def test_unverified_google_email_is_rejected_even_on_the_domain():
    """Without this, an unverified @cortech… address would pass the domain test."""
    allowed, reason = settings.email_allowed(
        "staff@cortechconsultinggroup.com", email_verified=False
    )
    assert allowed is False
    assert "not verified" in reason


@pytest.mark.parametrize("email", [None, "", "   ", "not-an-email", "@", "no-at-sign"])
def test_missing_or_malformed_email_is_rejected(email):
    allowed, _ = settings.email_allowed(email, email_verified=True)
    assert allowed is False


# ── the explicit exception list ─────────────────────────────────────────────

def test_allowlist_is_empty_by_default(monkeypatch):
    """Behaviour with no configuration is domain-only. No implicit openings."""
    monkeypatch.delenv("AUTH_ALLOWED_EMAILS", raising=False)
    assert settings._allowed_emails() == frozenset()
    allowed, _ = settings.email_allowed("someone@gmail.com", True)
    assert allowed is False


def test_explicit_allowlist_entry_is_admitted(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "sharahbiilabdi2@gmail.com")
    allowed, reason = settings.email_allowed("sharahbiilabdi2@gmail.com", True)
    assert allowed is True
    assert "AUTH_ALLOWED_EMAILS" in reason


def test_allowlist_admits_only_the_exact_address(monkeypatch):
    """An entry is one address — never a second domain."""
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "one@gmail.com")
    assert settings.email_allowed("one@gmail.com", True)[0] is True
    assert settings.email_allowed("two@gmail.com", True)[0] is False
    assert settings.email_allowed("one@gmail.com.evil.com", True)[0] is False


def test_allowlist_entry_still_requires_a_verified_email(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "one@gmail.com")
    assert settings.email_allowed("one@gmail.com", False)[0] is False


def test_allowlist_parses_commas_semicolons_and_case(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", " A@x.com; b@y.com ,C@z.com ")
    assert settings._allowed_emails() == frozenset({"a@x.com", "b@y.com", "c@z.com"})
    assert settings.email_allowed("B@Y.com", True)[0] is True


# ── sessions ────────────────────────────────────────────────────────────────

@pytest.fixture
def signed(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "unit-test-secret")
    monkeypatch.setattr(settings, "SESSION_MAX_AGE_SECONDS", 3600)
    return True


def test_session_round_trip(signed):
    token = auth.issue_session("staff@cortechconsultinggroup.com", "Staff")
    data = auth.read_session(token)
    assert data["email"] == "staff@cortechconsultinggroup.com"


def test_session_signed_with_another_secret_is_rejected(signed, monkeypatch):
    token = auth.issue_session("staff@cortechconsultinggroup.com")
    monkeypatch.setattr(settings, "SESSION_SECRET", "a-different-secret")
    assert auth.read_session(token) is None


def test_tampered_session_is_rejected(signed):
    token = auth.issue_session("staff@cortechconsultinggroup.com")
    assert auth.read_session(token[:-3] + "AAA") is None


def test_expired_session_is_rejected(signed, monkeypatch):
    token = auth.issue_session("staff@cortechconsultinggroup.com")
    monkeypatch.setattr(settings, "SESSION_MAX_AGE_SECONDS", -1)
    assert auth.read_session(token) is None


def test_session_for_a_now_disallowed_email_is_rejected(signed, monkeypatch):
    """Revocation must take effect immediately, not when the cookie expires.

    A validly-signed cookie for an address that has since been removed from
    AUTH_ALLOWED_EMAILS must stop working on the next request.
    """
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "contractor@gmail.com")
    token = auth.issue_session("contractor@gmail.com")
    assert auth.read_session(token) is not None

    monkeypatch.delenv("AUTH_ALLOWED_EMAILS")
    assert auth.read_session(token) is None


def test_no_session_secret_means_no_session(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "")
    assert auth.read_session("anything") is None
    with pytest.raises(auth.AuthError):
        auth.issue_session("staff@cortechconsultinggroup.com")


# ── CSRF ────────────────────────────────────────────────────────────────────

def test_csrf_token_is_bound_to_the_session(signed):
    a = {"email": "a@cortechconsultinggroup.com", "iat": 1}
    b = {"email": "b@cortechconsultinggroup.com", "iat": 1}
    assert auth.csrf_token(a) != auth.csrf_token(b)
    assert auth.csrf_ok(a, auth.csrf_token(a)) is True
    assert auth.csrf_ok(a, auth.csrf_token(b)) is False
    assert auth.csrf_ok(a, "") is False
    assert auth.csrf_ok(None, "anything") is False


# ── OAuth configuration ─────────────────────────────────────────────────────

def test_auth_reports_exactly_which_settings_are_missing(monkeypatch):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SESSION_SECRET"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    ok, reason = settings.auth_configured()
    assert ok is False
    for expected in (
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        "DASHBOARD_SESSION_SECRET", "PUBLIC_BASE_URL",
    ):
        assert expected in reason


def test_login_refuses_when_oauth_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "")
    with pytest.raises(auth.AuthError):
        auth.begin_login()


def test_authorize_url_targets_google_with_the_domain_hint(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(settings, "SESSION_SECRET", "unit-test-secret")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://bd.example.com")

    url, packed = auth.begin_login()
    assert url.startswith(auth.GOOGLE_AUTH_ENDPOINT)
    assert "response_type=code" in url
    assert "scope=openid+email+profile" in url or "scope=openid%20email%20profile" in url
    assert f"hd={DOMAIN}" in url
    assert "redirect_uri=https%3A%2F%2Fbd.example.com%2Fauth%2Fcallback" in url
    assert "nonce=" in url and "state=" in url
    assert packed


def test_callback_rejects_a_mismatched_state(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(settings, "SESSION_SECRET", "unit-test-secret")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://bd.example.com")

    _url, packed = auth.begin_login()
    with pytest.raises(auth.AuthError, match="state mismatch"):
        auth.complete_login("code", "not-the-state", packed)


def test_callback_without_a_state_cookie_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(settings, "SESSION_SECRET", "unit-test-secret")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://bd.example.com")
    with pytest.raises(auth.AuthError, match="state cookie missing"):
        auth.complete_login("code", "state", None)


def test_redirect_uri_is_built_from_the_public_base_url(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://bd.example.com")
    assert settings.redirect_uri() == "https://bd.example.com/auth/callback"


def test_default_domain_is_the_cortech_domain():
    """Guard the default so an env typo cannot silently widen access."""
    module = importlib.reload(importlib.import_module("dashboard.settings"))
    assert module.AUTH_ALLOWED_DOMAIN == DOMAIN
