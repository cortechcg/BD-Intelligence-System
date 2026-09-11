# config.py
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from dotenv import load_dotenv

# override=True — a stale ANTHROPIC_API_KEY exported in the shell must not
# silently beat an updated .env after the user rotates keys.
# interpolate=False — $ in API keys must not be treated as variable expansion.
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH, override=True, interpolate=False)


def env_file_save_hint() -> str:
    """Tell the operator when .env on disk was last saved.

    Editing the key in the IDE and not saving is the usual cause of a 401
    after a key rotation: Python reads the file on disk, not the unsaved tab.
    """
    try:
        ts = datetime.fromtimestamp(os.path.getmtime(_ENV_PATH)).strftime(
            "%Y-%m-%d %H:%M"
        )
    except OSError:
        return "Could not read .env on disk."
    return (
        f".env on disk was last saved {ts}. If you just pasted a new key in "
        "the editor, save the file (Ctrl+S) before rerunning."
    )


def get_anthropic_api_key() -> str | None:
    """Return a cleaned Anthropic API key from the environment."""
    load_dotenv(_ENV_PATH, override=True, interpolate=False)
    raw = os.getenv("ANTHROPIC_API_KEY") or ""
    key = raw.strip().strip('"').strip("'").removeprefix("Bearer ").strip()
    return key or None


def get_openai_api_key() -> str | None:
    """Return a cleaned OpenAI API key from the environment (embeddings only)."""
    load_dotenv(_ENV_PATH, override=True, interpolate=False)
    raw = os.getenv("OPENAI_API_KEY") or ""
    key = raw.strip().strip('"').strip("'").removeprefix("Bearer ").strip()
    return key or None


# Hard wall-clock limit on any single Claude call. Without this the SDK
# default can park the whole pipeline at "Analyzing..." for a long time
# with no output — the run looks hung rather than slow. 180s is well
# above a normal ToR analysis (10-25s) and still fails fast.
ANTHROPIC_TIMEOUT_SECONDS = float(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "180"))
ANTHROPIC_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "2"))


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer setting or fail at startup with a useful error."""
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value}")
    return value


def _nonnegative_int_env(name: str, default: int) -> int:
    """Read a non-negative integer setting (zero is a valid hard stop)."""
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value}")
    return value


# Download limits are deliberately finite: tender URLs are untrusted and a
# malformed or hostile endpoint must not exhaust the agent's memory or disk.
DOCUMENT_DOWNLOAD_TIMEOUT_SECONDS = _positive_int_env(
    "DOCUMENT_DOWNLOAD_TIMEOUT_SECONDS", 60
)
MAX_DOCUMENT_BYTES = _positive_int_env("MAX_DOCUMENT_BYTES", 25 * 1024 * 1024)
MAX_DOWNLOAD_REDIRECTS = _positive_int_env("MAX_DOWNLOAD_REDIRECTS", 5)
DOCUMENT_DOWNLOAD_MAX_RETRIES = _positive_int_env("DOCUMENT_DOWNLOAD_MAX_RETRIES", 2)
# Google Drive folders are untrusted annex containers. Keep one listing from
# expanding an opportunity into an unbounded document/parse workload.
MAX_GDRIVE_FILES = _positive_int_env("MAX_GDRIVE_FILES", 25)
MAX_EXTRACTED_TEXT_CHARS = _positive_int_env("MAX_EXTRACTED_TEXT_CHARS", 120_000)
MAX_DOCUMENT_UNCOMPRESSED_BYTES = _positive_int_env(
    "MAX_DOCUMENT_UNCOMPRESSED_BYTES", 100 * 1024 * 1024
)

_CLIENT_CACHE: dict[str, object] = {}
_ANTHROPIC_CONSTRUCTION_LOCK = threading.RLock()


@contextmanager
def _without_anthropic_api_key_for_client_construction():
    """Hide the env key briefly while constructing an explicitly keyed client.

    The project always passes its sanitized ``.env`` key as ``api_key=``. Newer
    Anthropic SDKs nevertheless inspect ``ANTHROPIC_API_KEY`` a second time
    solely to warn when a developer has an unrelated local SDK profile (for
    example, a Codex/federated profile). Temporarily removing the duplicate
    environment value prevents that misleading warning; it cannot affect
    authentication because the constructor receives the key explicitly.

    ``os.environ`` is process-wide, so serialize the very short construction
    window. The value is restored even if the SDK raises.
    """
    with _ANTHROPIC_CONSTRUCTION_LOCK:
        environment_value = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            yield
        finally:
            if environment_value is not None:
                os.environ["ANTHROPIC_API_KEY"] = environment_value


def get_anthropic_client(*, timeout: float | None = None, max_retries: int | None = None):
    """Shared Anthropic client — always uses the sanitized key from .env.

    Cached per (api_key, timeout, retries) so repeated calls reuse one HTTP
    pool; a rotated key in .env still produces a fresh client.
    """
    import anthropic

    api_key = get_anthropic_api_key()
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY is not set. Add it to .env and restart the terminal."
        )
    to = ANTHROPIC_TIMEOUT_SECONDS if timeout is None else timeout
    retries = ANTHROPIC_MAX_RETRIES if max_retries is None else max_retries
    cache_key = f"{api_key}:{to}:{retries}"
    client = _CLIENT_CACHE.get(cache_key)
    if client is None:
        # Suppress the SDK's profile/federation shadow warning. This program
        # deliberately uses the explicitly passed project API key, never a
        # machine-wide Anthropic profile.
        with _without_anthropic_api_key_for_client_construction():
            client = anthropic.Anthropic(
                api_key=api_key,
                timeout=to,
                max_retries=retries,
            )
        _CLIENT_CACHE[cache_key] = client
    return client


# ── API KEYS ──────────────────────────────────────────────────
def _clean(name: str) -> str | None:
    """Strip whitespace, quotes and a trailing slash off an env value.

    Values are pasted out of web consoles, which is how AIRTABLE_BASE_ID
    once arrived as "app.../ " — the trailing slash produced a double
    slash in the request path and Airtable answered 404 NOT_FOUND, which
    reads like a wrong base ID rather than a formatting problem.
    """
    raw = os.getenv(name) or ""
    return raw.strip().strip('"').strip("'").rstrip("/").strip() or None


ANTHROPIC_API_KEY = get_anthropic_api_key()
OPENAI_API_KEY = get_openai_api_key()
AIRTABLE_API_KEY = _clean("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = _clean("AIRTABLE_BASE_ID")
# Must be the bare project URL. supabase-py appends /rest/v1 itself, so a
# pasted ".../rest/v1/" endpoint would build /rest/v1/rest/v1/... and 404
# every query.
SUPABASE_URL = _clean("SUPABASE_URL")
if SUPABASE_URL and SUPABASE_URL.endswith("/rest/v1"):
    SUPABASE_URL = SUPABASE_URL[: -len("/rest/v1")]
SUPABASE_SERVICE_KEY = _clean("SUPABASE_SERVICE_KEY")

# ── ASSORTIS / ICA DAILY NEWSLETTER (IMAP) ───────────────────
# Read here rather than in monitors/assortis_email.py so they cannot be
# read before load_dotenv() has run — that ordering bug silently turns
# the whole newsletter source off with only a "not configured" warning.
IMAP_HOST = os.getenv("IMAP_HOST")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USERNAME = os.getenv("IMAP_USERNAME")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
# Matched against the sender ADDRESS or display name, case-insensitively —
# not passed to the IMAP FROM search key. See _sender_matches().
IMAP_NEWSLETTER_SENDER = os.getenv("IMAP_NEWSLETTER_SENDER", "icaworld.net")
IMAP_NEWSLETTER_SUBJECT = os.getenv("IMAP_NEWSLETTER_SUBJECT", "ICA Daily Newsletter")
# How many days of newsletters to re-read each run. The agent does NOT
# use the \Seen flag (shared human inbox — see monitors/assortis_email.py),
# so this window is what makes a missed run self-healing. Re-reading is
# free: Supabase dedup blocks anything already processed.
IMAP_LOOKBACK_DAYS = int(os.getenv("IMAP_LOOKBACK_DAYS", "3"))

# Optional dead-man's-switch (e.g. a Healthchecks.io check URL). Pipeline
# pings this on every successful run and pings f"{url}/fail" on a crash.
# Leave unset to disable — every call site checks for this being empty
# first and no-ops, so nothing breaks if you don't set this up.
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL", "")

# ── AIRTABLE TABLE NAMES ──────────────────────────────────────
TABLES = {
    "opportunities": "OPPORTUNITIES",
    "consultants": "CONSULTANTS",
    "past_proposals": "PAST_PROPOSALS",
    "rate_cards": "RATE_CARDS",
    "pipeline": "PIPELINE_TRACKER",
    "logs": "AGENT_LOGS",
    "donor_intelligence": "DONOR_INTELLIGENCE",
}

# ── CLAUDE SETTINGS ───────────────────────────────────────────
# Two constants — never hardcode model IDs at call sites.
CLAUDE_MODEL = "claude-sonnet-5"           # analysis + extraction
CLAUDE_MODEL_PROPOSAL = "claude-fable-5"   # proposal / EOI writing
CLAUDE_MAX_TOKENS = 8192

# ESTIMATED list prices (USD per million tokens). Used only for observability.
# If a model is missing here, estimated_cost_usd is UNKNOWN — never invented.
# Update when Anthropic publishes new rates; these are not invoices.
CLAUDE_PRICING_PER_MTOK = {
    CLAUDE_MODEL: {"input": 2.00, "output": 10.00},
    CLAUDE_MODEL_PROPOSAL: {"input": 10.00, "output": 50.00},
}

# Fail loud at process start. Airtable is intentionally omitted — CRM writes
# are fail-open. IMAP/Gmail are optional source/channel credentials.
# OPENAI_API_KEY remains required for embeddings (text-embedding-3-small).
REQUIRED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
)


def validate_required_env() -> list[str]:
    """Return names of missing required vars. Raises SystemExit if any missing
    when called from main's entry point."""
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not SUPABASE_SERVICE_KEY:
        missing.append("SUPABASE_SERVICE_KEY")
    return missing


def require_env() -> None:
    missing = validate_required_env()
    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in real values."
        )

# ── CORTECH PROFILE (injected into every prompt) ─────────────
CORTECH_PROFILE = """
Company: Cortech Consulting Group
Headquarters: Nairobi, Kenya (also Mogadishu, Somalia & London, UK)
Founded: 2015
Team size: 4-8 core staff + network consultants
Website: https://cortechcgl.com
Tagline: "Independent Oversight, Informed Decision"

CORE THEMATIC AREAS:
- Monitoring, Evaluation and Learning (MEL)
- Research, Assessment and Documentation
- Capacity Building and Training
- Community Engagement and Participatory Research
- Urban Resilience and Climate Adaptation
- Refugee/Displacement and Protection Programming
- Livelihoods and Economic Inclusion
- WASH (Water, Sanitation and Hygiene)
- Health Systems Strengthening & Nutrition
- Policy Analysis and Advocacy

GEOGRAPHIC FOCUS:
Primary: Somalia, Kenya, Ethiopia
Secondary: Sudan, South Sudan, Uganda, Tanzania, Malawi, Djibouti

LANGUAGES: English, Somali, Swahili, Arabic, French

TYPICAL BUDGET RANGE: $1,000 - $300,000 USD

KEY TOOLS & METHODOLOGIES:
- KoboToolbox (survey design and data collection)
- SPSS (quantitative analysis)
- NVivo (qualitative analysis)
- Python/R (data science)
- GIS/QGIS (spatial analysis)
- OECD DAC evaluation criteria
- HVCA methodology
- Participatory Action Research

KEY CLIENTS (past and current):
World Bank, IOM, African Union, IGAD, Save the Children,
Danish Refugee Council (DRC), GIZ, CARE International,
Welthungerhilfe, DANIDA, Arche Nova, UNHCR, UN Women,
UNICEF, C40 Cities, Pastoralist Girls Initiative (PGI),
Government of Kenya, Federal Government of Somalia,
PACIDA, Plan International, Alight Somalia,
Heifer International, Heifer International Kenya,
Action Medeor, Qatar Red Crescent (QRC),
Global Alliance for Improved Nutrition (GAIN),
County Government of Kenya, Kenya Red Cross Society,
Save the Children Somalia Country Office, CEWARN,
Government of Ireland,
Rural Livelihoods Resilience Programme (RLRP),
Kenya Urban Support Program (KUSP),
African Enterprise Challenge Fund (AECF),
Danish Red Cross, Zizi Afrique Foundation,
European Union, Concern Worldwide,
Norwegian Refugee Council,
Ministry of Foreign Affairs of Denmark,
Grundfos Foundation, Dan Church Aid (DCA),
Turkana Pastoralists Development Organization (TUPADO),
Rural Agency for Community Development Assistance (RACIDA),
UNDP

REGISTRATIONS:
- Kenya: Registered company
- Somalia: Licensed to operate
- United Kingdom: Registered company

CERTIFICATIONS:
- ISO certified
- Child safeguarding policy
- PSEA policy compliant

COMPETITIVE STRENGTHS:
1. Deep East Africa presence (offices in Nairobi + Mogadishu)
2. Bilingual team (English + Somali/Swahili)
3. Established relationships with major UN agencies and donors
4. Advanced digital data collection (KoboToolbox expert)
5. Track record in fragile and conflict-affected states (Somalia)
6. Rapid mobilization capacity
"""

# Discovery is limited to two sources:
# Assortis (ICA newsletter via IMAP) and Somali Jobs (Playwright scraper).
# RSS is unused — keep the list empty so monitor_rss_feeds() is a no-op.
RSS_FEEDS = []

URGENT_DEADLINE_DAYS = 3    # Flag as urgent if deadline in N days
SOON_DEADLINE_DAYS = 7     # Flag as soon if deadline in N days
CHECK_INTERVAL_HOURS = _positive_int_env("CHECK_INTERVAL_HOURS", 6)

# Hard ceiling on how many opportunities one run will draft for. Each one
# costs up to ~10 Claude calls, so an unbounded run over a big discovery
# batch takes hours and a lot of credit. Nothing is lost by capping: an
# opportunity is only written to Supabase once it has been processed, so
# whatever is deferred here is rediscovered on the next run.
MAX_OPPORTUNITIES_PER_RUN = _nonnegative_int_env("MAX_OPPORTUNITIES_PER_RUN", 15)

# WATCH-rated opportunities used to get a two-section quick flag (cover
# letter + executive summary) instead of a draft. A WATCH is a mid-scoring
# fit, not a rejection — the Christian Aid energy evaluation scored 65 and
# still warranted a real submission — and a two-section stub is not
# something a reviewer can act on. Full draft is now the default for both
# BID and WATCH. Set FULL_DRAFT_FOR_WATCH=false to restore the quick flag;
# it costs roughly five times fewer Claude calls per WATCH opportunity.
FULL_DRAFT_FOR_WATCH = os.getenv(
    "FULL_DRAFT_FOR_WATCH", "true"
).strip().lower() not in {"false", "0", "no", "off"}
