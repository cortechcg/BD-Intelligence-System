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

RSS_FEEDS = [
    {
        "name": "ReliefWeb Consultancies",
        "url": "https://reliefweb.int/jobs/rss.xml?type=consultancy",
        "filter_keywords": [],  # three-gate filter handles this
    },
    {
        "name": "World Bank Procurement",
        "url": "https://www.worldbank.org/en/projects-operations/products-and-services/brief/consulting-services-rss",
        "filter_keywords": [],
    },
    {
        "name": "UNGM Notices",
        "url": "https://www.ungm.org/Public/Notice/rss",
        "filter_keywords": [],
    },
    {
        "name": "UNDP Procurement",
        "url": "https://procurement-notices.undp.org/index.cfm?event=RSS.showRSS",
        "filter_keywords": [],
    },
    {
        "name": "AfDB Procurement",
        "url": "https://www.afdb.org/en/rss/procurement",
        "filter_keywords": [],
    },
    {
        "name": "EU TED Procurement (East Africa)",
        "url": "https://ted.europa.eu/api/v1/notices/search/rss?q=east+africa+consultancy&fields=title,publicationDate,tenderType",
        "filter_keywords": [],
    },
    {
        "name": "IOM Procurement",
        "url": "https://www.iom.int/iom-procurement-rss",
        "filter_keywords": [],
    },
    {
        "name": "DevelopmentAid Tenders",
        "url": "https://www.developmentaid.org/rss/tenders",
        "filter_keywords": [],
    },
]

# Sources with no RSS feed — scraped directly
# Handled in monitors/rss_monitor.py → scrape_non_rss_sources()
SCRAPE_SOURCES = [
    {
        "name": "Somali Jobs Tenders",
        "url": "https://www.somalijobs.com/tenders",
        "type": "somalijobs",
    },
    {
        "name": "DRC Procurement",
        "url": "https://pro.drc.ngo/suppliers/open-procurements/",
        "type": "generic_list",
    },
    {
        "name": "Save the Children Procurement",
        "url": "https://www.savethechildren.net/about-us/jobs/procurement",
        "type": "generic_list",
    },
    {
        "name": "CARE International Tenders",
        "url": "https://www.care.org/about-us/procurement/",
        "type": "generic_list",
    },
    {
        "name": "Welthungerhilfe Tenders",
        "url": "https://www.welthungerhilfe.org/our-work/procurement/",
        "type": "generic_list",
    },
    {
        "name": "NRC Tenders",
        "url": "https://www.nrc.no/about-nrc/procurements/",
        "type": "generic_list",
    },
    {
        "name": "Kenya Government PPIP",
        "url": "https://tenders.go.ke/website/tenders/index",
        "type": "generic_list",
    },
    {
        "name": "Ethiopia PPPA",
        "url": "https://www.pppa.gov.et/procurement-notices",
        "type": "generic_list",
    },
    {
        "name": "USAID Business Forecast",
        "url": "https://www.usaid.gov/rss/business-forecast",
        "type": "generic_list",
    },
    {
        "name": "GIZ Procurement",
        "url": "https://www.giz.de/en/html/tenders.html",
        "type": "generic_list",
    },
]

# ── SCORING THRESHOLDS ────────────────────────────────────────
SCORE_THRESHOLDS = {
    "bid": 40,        # Score >= 40 → Recommend BID
    "watch": 30,      # Score 30-40 → WATCH
    "no_bid": 10,      # Score < 10 → NO-BID
}

URGENT_DEADLINE_DAYS = 3    # Flag as urgent if deadline in N days
SOON_DEADLINE_DAYS = 7     # Flag as soon if deadline in N days
CHECK_INTERVAL_HOURS = 6  # polling interval for continuous (non --once) mode