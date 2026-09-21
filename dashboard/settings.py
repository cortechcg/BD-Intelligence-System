"""Dashboard configuration.

Reads the same ``.env`` as ``config.py`` so a local run and a Render run differ
only in where the variables come from. No credential is ever defaulted to a
usable value — a missing secret disables the feature loudly rather than
silently falling back to something permissive.
"""
from __future__ import annotations

import os

from config import _ENV_PATH

# ── Auth ────────────────────────────────────────────────────────────────────

#: The domain the brief mandates. Overridable only so a rename doesn't require
#: a code change; it is not a way to open the tool up.
AUTH_ALLOWED_DOMAIN = (
    os.getenv("AUTH_ALLOWED_DOMAIN") or "cortechconsultinggroup.com"
).strip().lower().lstrip("@")


def _allowed_emails() -> frozenset[str]:
    """Explicit off-domain exceptions. Empty by default.

    The brief flags that a team member whose email is not on the Cortech domain
    must be raised rather than silently accommodated. This list is that
    accommodation, made explicit: it lives in the environment (and therefore in
    render.yaml, visible in review), defaults to empty, and each entry is a
    single full address — never a second domain.
    """
    raw = os.getenv("AUTH_ALLOWED_EMAILS") or ""
    return frozenset(
        part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()
    )


GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()

#: Public origin of the deployed service, e.g. https://cortech-bd.onrender.com
#: Used to build the OAuth redirect URI. Must match the Google console exactly.
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")

#: Signs the session cookie. No default — an unset value disables login rather
#: than falling back to a guessable key.
SESSION_SECRET = (os.getenv("DASHBOARD_SESSION_SECRET") or "").strip()

SESSION_MAX_AGE_SECONDS = int(os.getenv("DASHBOARD_SESSION_MAX_AGE") or 43200)  # 12h
SESSION_COOKIE_NAME = "cortech_bd_session"

#: Off only for local development against http://127.0.0.1.
COOKIE_SECURE = (os.getenv("DASHBOARD_COOKIE_SECURE") or "true").lower() != "false"

# ── Behaviour ───────────────────────────────────────────────────────────────

POLL_MS = int(os.getenv("DASHBOARD_POLL_MS") or 15000)
WORKER_POLL_SECONDS = float(os.getenv("DASHBOARD_WORKER_POLL_SECONDS") or 5.0)
WORKER_LEASE_SECONDS = int(os.getenv("DASHBOARD_WORKER_LEASE_SECONDS") or 3600)

#: Hard off-switch for triggering. The read-only views keep working.
TRIGGERS_ENABLED = (os.getenv("DASHBOARD_TRIGGERS_ENABLED") or "true").lower() != "false"

#: Aggregate cap across ALL dashboard-triggered runs in a trailing window.
#:
#: This is a control the pipeline did not have: the Phase 8 cap
#: (MAX_RUN_COST_USD) is per run, and every `--submit-url` — hence every
#: dashboard trigger — is its own run with its own fresh cap. Forty clicks on
#: "Draft selected" would therefore be forty caps. This gate is enforced by
#: the WORKER before it starts a run (dashboard/worker.py), against the spend
#: actually recorded on finished/cancelled/running trigger rows; the UI only
#: mirrors it. Default 100 USD / 24 h. Zero blocks every dashboard run.
AGGREGATE_CAP_USD = float(os.getenv("DASHBOARD_AGGREGATE_CAP_USD") or 100.0)
AGGREGATE_WINDOW_HOURS = int(os.getenv("DASHBOARD_AGGREGATE_WINDOW_HOURS") or 24)

#: How often a running worker reports live spend + checks for a cancel.
HEARTBEAT_SECONDS = float(os.getenv("DASHBOARD_HEARTBEAT_SECONDS") or 10.0)

#: Upper bound on one bulk "Draft selected" request. Each is a separate job;
#: the aggregate cap above is what actually bounds the money.
BULK_MAX = int(os.getenv("DASHBOARD_BULK_MAX") or 40)


def auth_configured() -> tuple[bool, str]:
    """Is Google OAuth usable? Returns ``(ok, reason_if_not)``."""
    missing = [
        name
        for name, val in (
            ("GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID),
            ("GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET),
            ("DASHBOARD_SESSION_SECRET", SESSION_SECRET),
            ("PUBLIC_BASE_URL", PUBLIC_BASE_URL),
        )
        if not val
    ]
    if missing:
        return False, "missing " + ", ".join(missing)
    return True, ""


def redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/auth/callback"


def email_allowed(email: str | None, email_verified: bool = True) -> tuple[bool, str]:
    """The access decision. Returns ``(allowed, reason)``.

    Pure function of its arguments so it can be tested without a browser, a
    Google app, or a network. Rejection reasons are specific because a login
    that fails for an unexplained reason gets worked around.
    """
    if not email or "@" not in email:
        return False, "no email address in the Google profile"
    addr = email.strip().lower()
    if not email_verified:
        return False, f"Google has not verified {addr}"
    domain = addr.rsplit("@", 1)[1]
    if domain == AUTH_ALLOWED_DOMAIN:
        return True, f"@{AUTH_ALLOWED_DOMAIN} account"
    if addr in _allowed_emails():
        return True, "explicit AUTH_ALLOWED_EMAILS entry"
    return False, (
        f"{addr} is not on @{AUTH_ALLOWED_DOMAIN} and is not in AUTH_ALLOWED_EMAILS"
    )
