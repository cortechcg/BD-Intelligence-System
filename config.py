# config.py
import os
from dotenv import load_dotenv

# override=True — a stale ANTHROPIC_API_KEY exported in the shell must not
# silently beat an updated .env after the user rotates keys.
# interpolate=False — $ in API keys must not be treated as variable expansion.
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH, override=True, interpolate=False)


def get_anthropic_api_key() -> str | None:
    """Return a cleaned Anthropic API key from the environment."""
    load_dotenv(_ENV_PATH, override=True, interpolate=False)
    raw = os.getenv("ANTHROPIC_API_KEY") or ""
    key = raw.strip().strip('"').strip("'").removeprefix("Bearer ").strip()
    return key or None


# Hard wall-clock limit on any single Claude call. Without this the SDK
# default (600s per attempt) applies, so one stalled connection parks the
# whole pipeline at "Analyzing with Claude..." for up to half an hour with
# no output — the run looks hung rather than slow. 180s is well above a
# normal ToR analysis (10-25s) and still fails fast.
ANTHROPIC_TIMEOUT_SECONDS = float(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "180"))
ANTHROPIC_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "2"))

_CLIENT_CACHE: dict[str, object] = {}


def get_anthropic_client():
    """Shared Anthropic client — always uses the sanitized key from .env.

    Cached per API key so repeated calls reuse one HTTP connection pool
    instead of opening a new one per opportunity; a rotated key in .env
    still produces a fresh client because the key is the cache key.
    """
    import anthropic

    api_key = get_anthropic_api_key()
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY is not set. Add it to .env and restart the terminal."
        )
    client = _CLIENT_CACHE.get(api_key)
    if client is None:
        client = anthropic.Anthropic(
            api_key=api_key,
            timeout=ANTHROPIC_TIMEOUT_SECONDS,
            max_retries=ANTHROPIC_MAX_RETRIES,
        )
        _CLIENT_CACHE[api_key] = client
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
CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_MODEL_PROPOSAL = "claude-sonnet-5"
CLAUDE_MAX_TOKENS = 8192

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

# Monitored sources are deliberately limited to three portals:
# Assortis (via the ICA newsletter IMAP monitor), Somali Jobs (scraper),
# and World Bank Procurement (RSS below).
RSS_FEEDS = [
    {
        "name": "World Bank Procurement",
        "url": "https://www.worldbank.org/en/projects-operations/products-and-services/brief/consulting-services-rss",
        "filter_keywords": [],  # three-gate filter handles this
    },
]

URGENT_DEADLINE_DAYS = 3    # Flag as urgent if deadline in N days
SOON_DEADLINE_DAYS = 7     # Flag as soon if deadline in N days
CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", "6"))  # polling interval for continuous (non --once) mode

# Hard ceiling on how many opportunities one run will draft for. Each one
# costs up to ~10 Claude calls, so an unbounded run over a big discovery
# batch takes hours and a lot of credit. Nothing is lost by capping: an
# opportunity is only written to Supabase once it has been processed, so
# whatever is deferred here is rediscovered on the next run.
MAX_OPPORTUNITIES_PER_RUN = int(os.getenv("MAX_OPPORTUNITIES_PER_RUN", "15"))