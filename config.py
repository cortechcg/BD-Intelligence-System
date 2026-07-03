# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# ── API KEYS ──────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# ── AIRTABLE TABLE NAMES ──────────────────────────────────────
TABLES = {
    "opportunities": "OPPORTUNITIES",
    "consultants": "CONSULTANTS",
    "past_proposals": "PAST_PROPOSALS",
    "rate_cards": "RATE_CARDS",
    "pipeline": "PIPELINE_TRACKER",
    "logs": "AGENT_LOGS",
}

# ── CLAUDE SETTINGS ───────────────────────────────────────────
CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_MODEL_PROPOSAL = "claude-opus-4-8"
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
Welthungerhilfe, DANIDA, Arche Nova, Welthungerhilfe,
UNHCR, UN Women, UNICEF, C40 Cities

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

# ── TENDER SOURCES ────────────────────────────────────────────
RSS_FEEDS = [
    {
        "name": "ReliefWeb Jobs & Tenders",
        "url": "https://reliefweb.int/jobs/rss.xml?source=reliefweb",
        "filter_keywords": ["consultant", "evaluation", "assessment", "research", "MEL"],
    },
    {
        "name": "World Bank Procurement",
        "url": "https://www.worldbank.org/en/projects-operations/products-and-services/brief/consulting-services-rss",
        "filter_keywords": ["east africa", "somalia", "kenya", "ethiopia"],
    },
    {
        "name": "UNGM Notices",
        "url": "https://www.ungm.org/Public/Notice/rss",
        "filter_keywords": ["consultant", "research", "evaluation"],
    },
]

SCRAPE_SOURCES = [
    {
        "name": "ReliefWeb Consultancies",
        "base_url": "https://reliefweb.int/jobs?type=consultancy&source=reliefweb",
        "type": "reliefweb",
    },
    {
        "name": "DRC Procurement",
        "base_url": "https://pro.drc.ngo/suppliers/open-procurements/",
        "type": "drc",
    },
    {
        "name": "SomaliJobs Tenders",
        "base_url": "https://somalijobs.com/tenders",
        "type": "somalijobs",
    },
]

# ── SCORING THRESHOLDS ────────────────────────────────────────
SCORE_THRESHOLDS = {
    "bid": 40,        # Score >= 40 → Recommend BID
    "watch": 30,      # Score 30-40 → WATCH
    "no_bid": 10,      # Score < 10 → NO-BID
}

URGENT_DEADLINE_DAYS = 3    # Flag as urgent if deadline in N days
SOON_DEADLINE_DAYS = 7      # Flag as soon if deadline in N days
CHECK_INTERVAL_HOURS = 6  # polling interval for continuous (non --once) mode