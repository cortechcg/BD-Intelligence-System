"""Google OAuth 2.0 sign-in, restricted to the Cortech domain.

Built on ``httpx`` + ``PyJWT`` + ``itsdangerous``, all of which are either
already in ``requirements.txt`` or are a few kilobytes — this repo has stayed
dependency-lean, and an OAuth framework would be more code than the flow.

The ID token is verified properly: signature against Google's published JWKS,
plus issuer, audience and expiry, plus the nonce we minted. An unverified
``email`` claim is rejected — without that check, anyone able to mint a Google
account with an unverified ``@cortechconsultinggroup.com`` address would pass
the domain test.

The access decision itself lives in ``settings.email_allowed`` so it is a pure
function and can be tested without a browser or a network.
"""
from __future__ import annotations

import hmac
import secrets
import time
from hashlib import sha256

import httpx
import jwt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from loguru import logger

from dashboard import settings

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

STATE_COOKIE = "cortech_bd_oauth"
_SALT_SESSION = "cortech-bd-session"
_SALT_STATE = "cortech-bd-oauth-state"

_jwk_client: jwt.PyJWKClient | None = None


class AuthError(Exception):
    """Login could not be completed. The message is shown to the user."""


def _serializer(salt: str) -> URLSafeTimedSerializer:
    if not settings.SESSION_SECRET:
        raise AuthError(
            "DASHBOARD_SESSION_SECRET is not set — refusing to sign sessions "
            "with a default key."
        )
    return URLSafeTimedSerializer(settings.SESSION_SECRET, salt=salt)


# ── session cookie ──────────────────────────────────────────────────────────

def issue_session(email: str, name: str = "", picture: str = "") -> str:
    return _serializer(_SALT_SESSION).dumps(
        {"email": email, "name": name, "iat": int(time.time())}
    )


def read_session(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        data = _serializer(_SALT_SESSION).loads(
            token, max_age=settings.SESSION_MAX_AGE_SECONDS
        )
    except SignatureExpired:
        return None
    except (BadSignature, AuthError):
        return None
    if not isinstance(data, dict) or not data.get("email"):
        return None
    # Re-check the allowlist on every request, not only at login: removing
    # someone from AUTH_ALLOWED_EMAILS must lock them out immediately rather
    # than when their 12-hour cookie happens to expire.
    allowed, _ = settings.email_allowed(data.get("email"), email_verified=True)
    if not allowed:
        return None
    return data


def csrf_token(session: dict | None) -> str:
    """Stateless per-session CSRF token.

    SameSite=Lax already blocks a cross-site form POST; this is the second
    layer, because the actions behind it spend money on LLM calls.
    """
    if not session or not settings.SESSION_SECRET:
        return ""
    msg = f"{session.get('email','')}|{session.get('iat','')}".encode()
    return hmac.new(settings.SESSION_SECRET.encode(), msg, sha256).hexdigest()


def csrf_ok(session: dict | None, supplied: str | None) -> bool:
    expected = csrf_token(session)
    return bool(expected) and hmac.compare_digest(expected, supplied or "")


# ── the OAuth dance ─────────────────────────────────────────────────────────

def begin_login() -> tuple[str, str]:
    """Return ``(authorize_url, state_cookie_value)``."""
    ok, reason = settings.auth_configured()
    if not ok:
        raise AuthError(f"Google sign-in is not configured: {reason}")

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    packed = _serializer(_SALT_STATE).dumps({"state": state, "nonce": nonce})

    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
        # A hint only — Google does not enforce it, so the real check is
        # settings.email_allowed() after the ID token is verified.
        "hd": settings.AUTH_ALLOWED_DOMAIN,
    }
    return f"{GOOGLE_AUTH_ENDPOINT}?{httpx.QueryParams(params)}", packed


def _jwks() -> jwt.PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = jwt.PyJWKClient(GOOGLE_JWKS_URI, cache_keys=True)
    return _jwk_client


def _verify_id_token(id_token: str, nonce: str) -> dict:
    try:
        key = _jwks().get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=settings.GOOGLE_CLIENT_ID,
            issuer=list(GOOGLE_ISSUERS),
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"Google ID token failed verification: {exc}") from exc

    if claims.get("nonce") != nonce:
        raise AuthError("Google ID token nonce did not match this login attempt")
    return claims


def complete_login(code: str, state: str, state_cookie: str | None) -> dict:
    """Exchange the code, verify the ID token, apply the allowlist.

    Returns the verified claims. Raises ``AuthError`` with a specific reason.
    """
    ok, reason = settings.auth_configured()
    if not ok:
        raise AuthError(f"Google sign-in is not configured: {reason}")
    if not state_cookie:
        raise AuthError("login state cookie missing — start again from /auth/login")

    try:
        packed = _serializer(_SALT_STATE).loads(state_cookie, max_age=600)
    except SignatureExpired as exc:
        raise AuthError("login attempt expired — start again") from exc
    except BadSignature as exc:
        raise AuthError("login state failed its signature check") from exc

    if not hmac.compare_digest(str(packed.get("state") or ""), state or ""):
        raise AuthError("login state mismatch — possible cross-site request")

    try:
        response = httpx.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"could not reach Google to exchange the code: {exc}") from exc

    if response.status_code >= 400:
        # Never echo the body — it can carry the client secret back on some
        # misconfigurations.
        raise AuthError(
            f"Google rejected the authorization code (HTTP {response.status_code})"
        )

    payload = response.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise AuthError("Google did not return an ID token")

    claims = _verify_id_token(id_token, str(packed.get("nonce") or ""))

    allowed, why = settings.email_allowed(
        claims.get("email"), bool(claims.get("email_verified"))
    )
    if not allowed:
        logger.warning(f"dashboard login refused: {why}")
        raise AuthError(f"Access denied — {why}.")

    logger.info(f"dashboard login accepted ({why}): {claims.get('email')}")
    return claims
