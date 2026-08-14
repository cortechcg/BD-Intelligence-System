"""
╔══════════════════════════════════════════════════════════════════╗
║          CORTECH AIRTABLE AUTO-POPULATION SCRIPT                 ║
║                                                                  ║
║  This script reads your CVs and past proposals from folders      ║
║  and automatically fills all your Airtable tables.               ║
║                                                                  ║
║  HOW TO USE:                                                     ║
║  1. Put all CV files (PDF or DOCX) in:  ./data/cvs/             ║
║  2. Put all proposal files in:          ./data/proposals/        ║
║  3. Make sure your .env file has API keys                        ║
║  4. Run: python populate_airtable.py                             ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import json
import uuid
import time
from pathlib import Path
from datetime import datetime
from io import BytesIO

# ── DEPENDENCY CHECK ──────────────────────────────────────────────────────────
# Check all required packages are installed before importing
REQUIRED = [
    "anthropic", "pyairtable", "supabase", "pdfplumber",
    "docx", "rich", "loguru", "dotenv"
]

missing = []
for pkg in REQUIRED:
    try:
        __import__(pkg if pkg != "dotenv" else "dotenv")
    except ImportError:
        missing.append(pkg if pkg != "dotenv" else "python-dotenv")

if missing:
    print(f"\n❌ Missing packages: {', '.join(missing)}")
    print(f"   Run: pip install {' '.join(missing)}\n")
    sys.exit(1)

# ── IMPORTS ───────────────────────────────────────────────────────────────────
import anthropic
from config import CLAUDE_MODEL, get_anthropic_api_key, get_anthropic_client
import pdfplumber
from docx import Document as DocxDocument
from pyairtable import Api
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress, SpinnerColumn, TextColumn,
    BarColumn, TaskProgressColumn
)
from rich.table import Table
from rich.prompt import Confirm, Prompt
from rich import print as rprint
from loguru import logger

# ── SETUP ─────────────────────────────────────────────────────────────────────
load_dotenv(override=True)
console = Console()

# Remove default loguru handler, add clean one
logger.remove()
logger.add("populate_airtable.log", rotation="10 MB", level="DEBUG")

# ── CONSTANTS ─────────────────────────────────────────────────────────────────
DATA_DIR       = Path("./data")
CVS_DIR        = DATA_DIR / "cvs"
PROPOSALS_DIR  = DATA_DIR / "proposals"

SUPPORTED_EXTS = {".pdf", ".docx", ".doc", ".txt"}

THEMATIC_OPTIONS = [
    "MEL", "Research", "Evaluation", "Survey Design",
    "Capacity Building", "Community Engagement",
    "Climate Resilience", "Urban Development",
    "Refugee/Displacement", "Protection", "Livelihoods",
    "WASH", "Health Systems", "Education", "TVET",
    "Nutrition", "Food Security", "Policy Analysis",
    "Advocacy", "Data Science", "GIS/Mapping",
    "Knowledge Management", "Documentation"
]

GEOGRAPHY_OPTIONS = [
    "Kenya", "Somalia", "Ethiopia", "Sudan", "South Sudan",
    "Uganda", "Tanzania", "Rwanda", "DRC", "Zimbabwe",
    "Zambia", "Djibouti", "Eritrea", "East Africa",
    "Horn of Africa", "Sub-Saharan Africa", "Global"
]

TOOLS_OPTIONS = [
    "KoboToolbox", "SPSS", "NVivo", "Python", "R",
    "QGIS", "ArcGIS", "Excel", "Stata", "Power BI",
    "Tableau", "ODK", "SurveyCTO", "MAXQDA", "Atlas.ti",
    "Airtable", "Supabase", "SQL", "JavaScript"
]

LANGUAGES_OPTIONS = [
    "English", "Somali", "Swahili", "Arabic", "French",
    "Amharic", "Oromo", "Tigrinya", "Dinka", "Nuer",
    "Kinyarwanda", "Portuguese", "Spanish"
]

SENIORITY_OPTIONS = ["Junior", "Mid", "Senior", "Principal"]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — AIRTABLE & CLAUDE CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

def get_airtable_clients():
    """Initialize Airtable API clients for all tables."""
    api_key  = os.getenv("AIRTABLE_API_KEY")
    base_id  = os.getenv("AIRTABLE_BASE_ID")

    if not api_key or not base_id:
        console.print("[red]❌ Missing AIRTABLE_API_KEY or AIRTABLE_BASE_ID in .env[/red]")
        sys.exit(1)

    api  = Api(api_key)
    base = api.base(base_id)

    return {
        "consultants":    base.table("CONSULTANTS"),
        "past_proposals": base.table("PAST_PROPOSALS"),
        "rate_cards":     base.table("RATE_CARDS"),
        "opportunities":  base.table("OPPORTUNITIES"),
        "logs":           base.table("AGENT_LOGS"),
    }


def get_claude_client():
    """Initialize Anthropic Claude client."""
    try:
        return get_anthropic_client()
    except ValueError:
        console.print("[red]❌ Missing ANTHROPIC_API_KEY in .env[/red]")
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — FILE READING
# ═══════════════════════════════════════════════════════════════════════════════

def read_pdf(file_path: Path) -> str:
    """Extract all text from a PDF file."""
    try:
        with pdfplumber.open(file_path) as pdf:
            pages = []
            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                if text and text.strip():
                    pages.append(f"[PAGE {i+1}]\n{text.strip()}")

                # Also try to extract tables
                tables = page.extract_tables()
                for table in tables:
                    if table:
                        for row in table:
                            row_text = " | ".join(
                                str(cell).strip() for cell in row if cell
                            )
                            if row_text.strip():
                                pages.append(row_text)

            full_text = "\n\n".join(pages)
            logger.debug(f"PDF: {file_path.name} → {len(full_text)} chars")
            return full_text
    except Exception as e:
        logger.error(f"PDF read failed for {file_path}: {e}")
        return ""


def read_docx(file_path: Path) -> str:
    """Extract all text from a DOCX file."""
    try:
        doc = DocxDocument(file_path)

        sections = []

        # Paragraphs
        for para in doc.paragraphs:
            if para.text.strip():
                sections.append(para.text.strip())

        # Tables
        for table in doc.tables:
            for row in table.rows:
                cells = [
                    cell.text.strip()
                    for cell in row.cells
                    if cell.text.strip()
                ]
                if cells:
                    sections.append(" | ".join(cells))

        full_text = "\n".join(sections)
        logger.debug(f"DOCX: {file_path.name} → {len(full_text)} chars")
        return full_text
    except Exception as e:
        logger.error(f"DOCX read failed for {file_path}: {e}")
        return ""


def read_txt(file_path: Path) -> str:
    """Read plain text file."""
    try:
        return file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.error(f"TXT read failed for {file_path}: {e}")
        return ""


def read_file(file_path: Path) -> str:
    """Route to correct reader based on file extension."""
    ext = file_path.suffix.lower()

    if ext == ".pdf":
        return read_pdf(file_path)
    elif ext in (".docx", ".doc"):
        return read_docx(file_path)
    elif ext == ".txt":
        return read_txt(file_path)
    else:
        logger.warning(f"Unsupported file type: {ext}")
        return ""


def get_files_in_folder(folder: Path) -> list[Path]:
    """Get all supported files in a folder (including subfolders)."""
    files = []
    if not folder.exists():
        return files

    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
            # Skip hidden files and system files
            if not path.name.startswith(".") and not path.name.startswith("~"):
                files.append(path)

    return sorted(files)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — CLAUDE EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

CV_EXTRACTION_SCHEMA = """
{
  "full_name": "string — person's full name",
  "role_title": "string — current job title or most senior title",
  "years_experience": "number — total years of professional experience",
  "education": "string — highest degree, institution, year e.g. MSc Development Studies, University of Nairobi, 2018",
  "seniority_level": "one of: Junior (0-3 yrs), Mid (4-7 yrs), Senior (8-14 yrs), Principal (15+ yrs)",
  "based_in": "string — primary city they are based in",
  "day_rate_usd": "number — estimated day rate in USD based on seniority and location, or 0 if unknown",
  "thematic_expertise": ["list of areas from CV — MEL, Research, Evaluation, Survey Design, Capacity Building, etc."],
  "geographic_experience": ["list of countries or regions they have worked in"],
  "languages": ["list of languages they speak"],
  "tools": ["list of tools/software they use — KoboToolbox, SPSS, Python, NVivo, QGIS, etc."],
  "key_skills": "string — comma-separated list of 8-12 key skills",
  "key_assignments": "string — summary of 3-5 most important assignments, one per line as: Project Name — Client — Year — 1 sentence description",
  "availability_status": "Available",
  "cv_summary": "string — 3-4 sentence professional summary of this person's profile for semantic matching"
}
"""

PROPOSAL_EXTRACTION_SCHEMA = """
{
  "project_title": "string — name of the project/assignment",
  "client": "string — organization that hired the consultant",
  "donor": "string — funding organization or null",
  "year": "number — year the proposal was submitted",
  "won": "boolean — true if this was a winning proposal (check for award notices, contract mentions)",
  "contract_value_usd": "number — contract value in USD or 0 if unknown",
  "thematic_areas": ["list of thematic areas — MEL, Research, Capacity Building, etc."],
  "location": ["list of countries/regions covered"],
  "methodology_approach": "string — 2-3 sentences summarizing the methodology used",
  "proposal_summary": "string — 4-5 sentence summary of this proposal's approach, key sections, and strengths. Focus on what makes it distinctive.",
  "key_sections_found": ["list of major sections found in this document"],
  "writing_style_notes": "string — 2-3 sentences describing the writing style, tone, and structure for Claude to learn from"
}
"""


def extract_cv_info(claude: anthropic.Anthropic, cv_text: str, file_name: str) -> dict:
    """Use Claude to extract structured information from a CV."""

    # Truncate if too long
    max_chars = 15000
    if len(cv_text) > max_chars:
        cv_text = cv_text[:max_chars] + "\n\n[CV TRUNCATED]"

    prompt = f"""You are analyzing a CV/resume for the Cortech Consulting Group HR database.

Extract structured information from this CV document.

IMPORTANT RULES:
- Extract ONLY what is actually in the document
- For day_rate_usd: estimate based on seniority level and location:
  * Junior in Nairobi: ~500 USD/day
  * Mid in Nairobi: ~1150 USD/day
  * Senior in Nairobi: ~1900 USD/day
  * Principal in Nairobi: ~2700 USD/day
  * Add 20% for London, add 15% for Mogadishu
- For thematic_expertise, geographic_experience, languages, tools: only include what's actually mentioned
- The cv_summary should be written for semantic search matching — include key skills, locations, experience level

Return ONLY a valid JSON object matching this schema. No other text.

SCHEMA:
{CV_EXTRACTION_SCHEMA}

CV DOCUMENT (filename: {file_name}):
{cv_text}"""

    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,  # Fast + cheap for extraction
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )

        text = response.content[0].text.strip()

        # Clean markdown if present
        if "```" in text:
            text = text.split("```json")[-1].split("```")[0].strip()
            if not text:
                text = text.split("```")[-2].strip() if "```" in text else text

        result = json.loads(text)
        logger.debug(f"Extracted CV: {result.get('full_name', 'Unknown')}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error for CV {file_name}: {e}")
        # Return basic structure with file name as fallback
        return {
            "full_name": file_name.replace(".pdf", "").replace(".docx", ""),
            "role_title": "Consultant",
            "years_experience": 0,
            "seniority_level": "Mid",
            "thematic_expertise": [],
            "geographic_experience": [],
            "languages": ["English"],
            "tools": [],
            "key_skills": "",
            "key_assignments": "",
            "cv_summary": cv_text[:500],
            "availability_status": "Available",
            "based_in": "Nairobi",
            "day_rate_usd": 0,
            "education": "",
            "extraction_error": str(e),
        }
    except Exception as e:
        logger.error(f"Claude extraction failed for {file_name}: {e}")
        raise


def extract_proposal_info(
    claude: anthropic.Anthropic,
    proposal_text: str,
    file_name: str
) -> dict:
    """Use Claude to extract structured information from a proposal document."""

    # Truncate if too long — keep beginning and end (most important parts)
    max_chars = 20000
    if len(proposal_text) > max_chars:
        half = max_chars // 2
        proposal_text = (
            proposal_text[:half]
            + "\n\n[... MIDDLE SECTION TRUNCATED ...]\n\n"
            + proposal_text[-half:]
        )

    prompt = f"""You are analyzing a technical proposal document for the Cortech Consulting Group knowledge base.

Extract structured information from this proposal to help the AI agent learn Cortech's proposal writing style.

IMPORTANT RULES:
- won: set to true if you see words like "awarded", "contract signed", "selected", or if the document is clearly a final deliverable
- For contract_value_usd: look for budget sections, currency amounts
- methodology_approach: focus on the research methods, not the background
- writing_style_notes: be specific — e.g. "Uses numbered sections, third person, evidence-based with data citations, avoids jargon"

Return ONLY a valid JSON object matching this schema. No other text.

SCHEMA:
{PROPOSAL_EXTRACTION_SCHEMA}

PROPOSAL DOCUMENT (filename: {file_name}):
{proposal_text}"""

    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )

        text = response.content[0].text.strip()

        if "```" in text:
            text = text.split("```json")[-1].split("```")[0].strip()

        result = json.loads(text)
        logger.debug(f"Extracted proposal: {result.get('project_title', 'Unknown')}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error for proposal {file_name}: {e}")
        return {
            "project_title": file_name.replace(".pdf", "").replace(".docx", ""),
            "client": "Unknown",
            "year": datetime.now().year,
            "won": False,
            "contract_value_usd": 0,
            "thematic_areas": [],
            "location": [],
            "methodology_approach": "",
            "proposal_summary": proposal_text[:500],
            "extraction_error": str(e),
        }
    except Exception as e:
        logger.error(f"Proposal extraction failed for {file_name}: {e}")
        raise


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — AIRTABLE POPULATION
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_multiselect(values: list, valid_options: list) -> list:
    """
    Match extracted values to valid Airtable multi-select options.
    Uses fuzzy matching to handle slight variations.
    """
    if not values:
        return []

    normalized = []
    values_lower = [str(v).lower().strip() for v in values]

    for option in valid_options:
        option_lower = option.lower()
        # Direct match or close match
        for val in values_lower:
            if (option_lower in val or val in option_lower
                    or option_lower.replace(" ", "") == val.replace(" ", "")):
                if option not in normalized:
                    normalized.append(option)
                break

    # Also add values that are clearly valid even if not in our list
    # (Airtable will reject invalid multi-select values, so we only use valid ones)
    return normalized


def add_consultant_to_airtable(
    tables: dict,
    cv_info: dict,
    raw_cv_text: str,
    file_name: str
) -> str | None:
    """Add a single consultant record to Airtable."""

    # Normalize multi-select fields to valid Airtable options
    thematic = normalize_multiselect(
        cv_info.get("thematic_expertise", []), THEMATIC_OPTIONS
    )
    geography = normalize_multiselect(
        cv_info.get("geographic_experience", []), GEOGRAPHY_OPTIONS
    )
    tools = normalize_multiselect(
        cv_info.get("tools", []), TOOLS_OPTIONS
    )
    languages = normalize_multiselect(
        cv_info.get("languages", []), LANGUAGES_OPTIONS
    )

    # Validate seniority
    seniority = cv_info.get("seniority_level", "Mid")
    if seniority not in SENIORITY_OPTIONS:
        seniority = "Mid"

    # Build the cv_text field — combines extracted summary with raw text
    # This is what gets embedded for semantic search
    cv_text_for_embedding = f"""
PROFESSIONAL SUMMARY:
{cv_info.get('cv_summary', '')}

FULL CV CONTENT:
{raw_cv_text[:8000]}
""".strip()

    record = {
        "consultant_id": str(uuid.uuid4()),
        "full_name": cv_info.get("full_name", file_name),
        "role_title": cv_info.get("role_title", "Consultant"),
        "years_experience": cv_info.get("years_experience", 0),
        "education": cv_info.get("education", ""),
        "seniority_level": seniority,
        "based_in": cv_info.get("based_in", "Nairobi"),
        "day_rate_usd": cv_info.get("day_rate_usd", 0),
        "thematic_expertise": thematic,
        "geographic_experience": geography,
        "languages": languages,
        "tools": tools,
        "key_skills": cv_info.get("key_skills", ""),
        "key_assignments": cv_info.get("key_assignments", ""),
        "availability_status": "Available",
        "cv_text": cv_text_for_embedding,
        "cv_last_updated": datetime.now().strftime("%Y-%m-%d"),
    }

    try:
        result = tables["consultants"].create(record, typecast=True)
        return result["id"]
    except Exception as e:
        console.print(f"  [red]Airtable rejected this record: {e}[/red]")
        logger.error(f"Failed to add consultant {cv_info.get('full_name')}: {e}")
        return None


def add_proposal_to_airtable(
    tables: dict,
    proposal_info: dict,
    raw_proposal_text: str,
    file_name: str
) -> str | None:
    """Add a single proposal record to Airtable."""

    thematic = normalize_multiselect(
        proposal_info.get("thematic_areas", []), THEMATIC_OPTIONS
    )
    location = normalize_multiselect(
        proposal_info.get("location", []), GEOGRAPHY_OPTIONS
    )

    # The full proposal text for style reference
    full_text = f"""
PROPOSAL SUMMARY:
{proposal_info.get('proposal_summary', '')}

METHODOLOGY:
{proposal_info.get('methodology_approach', '')}

WRITING STYLE:
{proposal_info.get('writing_style_notes', '')}

KEY SECTIONS:
{', '.join(proposal_info.get('key_sections_found', []))}

FULL PROPOSAL TEXT:
{raw_proposal_text[:15000]}
""".strip()

    record = {
        "proposal_id": str(uuid.uuid4()),
        "project_title": proposal_info.get("project_title", file_name),
        "client": proposal_info.get("client", "Unknown"),
        "donor": proposal_info.get("donor", ""),
        "year": int(proposal_info.get("year", datetime.now().year)),
        "won": bool(proposal_info.get("won", False)),
        "contract_value_usd": proposal_info.get("contract_value_usd", 0),
        "thematic_areas": thematic,
        "location": location,
        "methodology_approach": proposal_info.get("methodology_approach", ""),
        "proposal_text": full_text,
    }

    try:
        result = tables["past_proposals"].create(record, typecast=True)
        return result["id"]
    except Exception as e:
        console.print(f"  [red]Airtable rejected this record: {e}[/red]")
        logger.error(f"Failed to add proposal {proposal_info.get('project_title')}: {e}")
        return None

def populate_rate_cards(tables: dict) -> int:
    """Populate the rate cards table with standard Cortech rates."""

    rates = [
        # Team Leader
        {"role_level": "Team Leader", "location": "Nairobi",     "day_rate_usd": 2700, "per_diem_usd": 150},
        {"role_level": "Team Leader", "location": "Addis Ababa", "day_rate_usd": 2500, "per_diem_usd": 180},
        {"role_level": "Team Leader", "location": "Mogadishu",   "day_rate_usd": 3000, "per_diem_usd": 350},
        {"role_level": "Team Leader", "location": "London",      "day_rate_usd": 3500, "per_diem_usd": 300},
        {"role_level": "Team Leader", "location": "Remote",      "day_rate_usd": 2200, "per_diem_usd": 0},
        # Senior Specialist
        {"role_level": "Senior Specialist", "location": "Nairobi",     "day_rate_usd": 1900, "per_diem_usd": 150},
        {"role_level": "Senior Specialist", "location": "Addis Ababa", "day_rate_usd": 1800, "per_diem_usd": 180},
        {"role_level": "Senior Specialist", "location": "Mogadishu",   "day_rate_usd": 2200, "per_diem_usd": 350},
        {"role_level": "Senior Specialist", "location": "London",      "day_rate_usd": 2500, "per_diem_usd": 300},
        {"role_level": "Senior Specialist", "location": "Remote",      "day_rate_usd": 1600, "per_diem_usd": 0},
        # Mid Specialist
        {"role_level": "Mid Specialist", "location": "Nairobi",     "day_rate_usd": 1150, "per_diem_usd": 150},
        {"role_level": "Mid Specialist", "location": "Addis Ababa", "day_rate_usd": 1100, "per_diem_usd": 180},
        {"role_level": "Mid Specialist", "location": "Mogadishu",   "day_rate_usd": 1400, "per_diem_usd": 350},
        {"role_level": "Mid Specialist", "location": "London",      "day_rate_usd": 1600, "per_diem_usd": 300},
        {"role_level": "Mid Specialist", "location": "Remote",      "day_rate_usd": 950,  "per_diem_usd": 0},
        # Junior Researcher
        {"role_level": "Junior Researcher", "location": "Nairobi",     "day_rate_usd": 500, "per_diem_usd": 100},
        {"role_level": "Junior Researcher", "location": "Addis Ababa", "day_rate_usd": 450, "per_diem_usd": 120},
        {"role_level": "Junior Researcher", "location": "Mogadishu",   "day_rate_usd": 650, "per_diem_usd": 200},
        {"role_level": "Junior Researcher", "location": "Remote",      "day_rate_usd": 400, "per_diem_usd": 0},
        # Field Researcher
        {"role_level": "Field Researcher", "location": "Nairobi",     "day_rate_usd": 385, "per_diem_usd": 80},
        {"role_level": "Field Researcher", "location": "Addis Ababa", "day_rate_usd": 350, "per_diem_usd": 80},
        {"role_level": "Field Researcher", "location": "Mogadishu",   "day_rate_usd": 500, "per_diem_usd": 150},
        # Translator
        {"role_level": "Translator", "location": "Nairobi",     "day_rate_usd": 155, "per_diem_usd": 60},
        {"role_level": "Translator", "location": "Addis Ababa", "day_rate_usd": 140, "per_diem_usd": 60},
        {"role_level": "Translator", "location": "Mogadishu",   "day_rate_usd": 200, "per_diem_usd": 100},
    ]

    added = 0
    for rate in rates:
        try:
            rate["rate_id"] = str(uuid.uuid4())
            rate["last_updated"] = datetime.now().strftime("%Y-%m-%d")
            tables["rate_cards"].create(rate)
            added += 1
            time.sleep(0.1)  # Airtable rate limit
        except Exception as e:
            logger.error(f"Failed to add rate: {e}")

    return added


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def validate_environment() -> bool:
    """Check all required environment variables and connections."""
    errors = []
    warnings = []

    console.print("\n[bold]🔍 Validating environment...[/bold]\n")

    # Check .env keys
    required_keys = [
        "ANTHROPIC_API_KEY",
        "AIRTABLE_API_KEY",
        "AIRTABLE_BASE_ID",
    ]
    optional_keys = [
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "EMAIL_SENDER",
        "EMAIL_RECIPIENTS",
    ]

    for key in required_keys:
        val = os.getenv(key) if key != "ANTHROPIC_API_KEY" else get_anthropic_api_key()
        if not val:
            errors.append(f"Missing: {key}")
        elif key == "ANTHROPIC_API_KEY" and not val.startswith("sk-ant-"):
            errors.append(
                f"{key} should start with sk-ant- (check for a pasted OpenAI or wrong key)"
            )
        elif len(val) < 10:
            errors.append(f"Looks invalid: {key}")
        else:
            console.print(f"  ✅ {key}: {'*' * 8}{val[-4:]}")

    # Warn if shell env would have overridden .env before override=True fix
    raw_env = os.environ.get("ANTHROPIC_API_KEY")
    cleaned = get_anthropic_api_key()
    if raw_env and cleaned and raw_env.strip() != cleaned:
        warnings.append(
            "ANTHROPIC_API_KEY in the shell differed from .env — config now uses .env (override=True)"
        )

    for key in optional_keys:
        val = os.getenv(key)
        if not val:
            warnings.append(f"Optional missing: {key}")
        else:
            console.print(f"  ✅ {key}: {'*' * 8}{val[-4:]}")

    # Check folders exist
    console.print()
    if CVS_DIR.exists():
        cv_count = len(get_files_in_folder(CVS_DIR))
        console.print(f"  ✅ CVs folder exists: {cv_count} files found")
    else:
        warnings.append(f"CVs folder not found: {CVS_DIR}")
        console.print(f"  ⚠️  CVs folder not found: {CVS_DIR}")

    if PROPOSALS_DIR.exists():
        prop_count = len(get_files_in_folder(PROPOSALS_DIR))
        console.print(f"  ✅ Proposals folder exists: {prop_count} files found")
    else:
        warnings.append(f"Proposals folder not found: {PROPOSALS_DIR}")
        console.print(f"  ⚠️  Proposals folder not found: {PROPOSALS_DIR}")

    # Test Airtable connection
    console.print()
    try:
        tables = get_airtable_clients()
        # Try to read from consultants table
        tables["consultants"].all(max_records=1)
        console.print("  ✅ Airtable connection: OK")
    except Exception as e:
        errors.append(f"Airtable connection failed: {e}")
        console.print(f"  ❌ Airtable connection failed: {e}")

    # Test Anthropic connection
    try:
        claude = get_claude_client()
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "Hi"}]
        )
        console.print("  ✅ Anthropic API: OK")
    except Exception as e:
        err = str(e)
        if "401" in err or "authentication_error" in err:
            errors.append(
                "Anthropic API rejected the key (401 invalid). "
                "Create a fresh key at console.anthropic.com → API Keys, "
                "paste it in .env as ANTHROPIC_API_KEY=sk-ant-... (no quotes), "
                "then open a new terminal and run again."
            )
        else:
            errors.append(f"Anthropic API failed: {e}")
        console.print(f"  ❌ Anthropic API failed: {e}")

    # Show results
    console.print()
    if warnings:
        for w in warnings:
            console.print(f"  [yellow]⚠️  {w}[/yellow]")

    if errors:
        console.print()
        for err in errors:
            console.print(f"  [red]❌ {err}[/red]")
        console.print()
        return False

    console.print("  [green bold]✅ All checks passed![/green bold]")
    return True


def check_for_duplicates(tables: dict, name: str, table_key: str, field: str) -> bool:
    """Check if a record already exists in Airtable."""
    try:
        from pyairtable.formulas import match
        records = tables[table_key].all(
            formula=f"FIND('{name}', {{{field}}})"
        )
        return len(records) > 0
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — INTERACTIVE REVIEW
# ═══════════════════════════════════════════════════════════════════════════════

def show_cv_preview(cv_info: dict, file_name: str) -> None:
    """Show a preview of extracted CV information before saving."""

    table = Table(title=f"📄 CV Extracted: {file_name}", show_header=True)
    table.add_column("Field", style="bold cyan", width=25)
    table.add_column("Extracted Value", style="white")

    table.add_row("Name", cv_info.get("full_name", "—"))
    table.add_row("Title", cv_info.get("role_title", "—"))
    table.add_row("Seniority", cv_info.get("seniority_level", "—"))
    table.add_row("Years Experience", str(cv_info.get("years_experience", 0)))
    table.add_row("Based In", cv_info.get("based_in", "—"))
    table.add_row("Day Rate (USD)", f"${cv_info.get('day_rate_usd', 0):,}")
    table.add_row("Thematic Areas", ", ".join(cv_info.get("thematic_expertise", [])))
    table.add_row("Geographic Exp.", ", ".join(cv_info.get("geographic_experience", [])))
    table.add_row("Languages", ", ".join(cv_info.get("languages", [])))
    table.add_row("Tools", ", ".join(cv_info.get("tools", [])))
    table.add_row("Education", cv_info.get("education", "—")[:80])

    console.print(table)


def show_proposal_preview(proposal_info: dict, file_name: str) -> None:
    """Show a preview of extracted proposal information before saving."""

    table = Table(title=f"📋 Proposal Extracted: {file_name}", show_header=True)
    table.add_column("Field", style="bold cyan", width=25)
    table.add_column("Extracted Value", style="white")

    table.add_row("Title", proposal_info.get("project_title", "—")[:60])
    table.add_row("Client", proposal_info.get("client", "—"))
    table.add_row("Donor", proposal_info.get("donor", "—"))
    table.add_row("Year", str(proposal_info.get("year", "—")))
    table.add_row("Won?", "✅ YES" if proposal_info.get("won") else "❌ No")
    table.add_row("Value (USD)", f"${proposal_info.get('contract_value_usd', 0):,}")
    table.add_row("Thematic Areas", ", ".join(proposal_info.get("thematic_areas", [])))
    table.add_row("Location", ", ".join(proposal_info.get("location", [])))
    table.add_row("Methodology", proposal_info.get("methodology_approach", "—")[:80])

    console.print(table)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — MAIN EXECUTION
# ═══════════════════════════════════════════════════════════════════════════════

def setup_folders() -> None:
    """Create required folders if they don't exist."""
    CVS_DIR.mkdir(parents=True, exist_ok=True)
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)

    # Create README files so user knows where to put files
    cv_readme = CVS_DIR / "PUT_CV_FILES_HERE.txt"
    if not cv_readme.exists():
        cv_readme.write_text(
            "Put all consultant CV files here.\n"
            "Supported formats: PDF, DOCX, TXT\n"
            "You can put them in subfolders too.\n"
        )

    prop_readme = PROPOSALS_DIR / "PUT_PROPOSAL_FILES_HERE.txt"
    if not prop_readme.exists():
        prop_readme.write_text(
            "Put all past proposal files here.\n"
            "Supported formats: PDF, DOCX, TXT\n"
            "Include winning AND losing proposals.\n"
        )


def run_cv_population(
    tables: dict,
    claude: anthropic.Anthropic,
    interactive: bool = True
) -> dict:
    """Process all CV files and add to Airtable."""

    cv_files = get_files_in_folder(CVS_DIR)

    # Filter out README files
    cv_files = [f for f in cv_files if "PUT_CV" not in f.name.upper()]

    if not cv_files:
        console.print(f"[yellow]⚠️  No CV files found in {CVS_DIR}[/yellow]")
        console.print(f"   Add PDF or DOCX files to: [cyan]{CVS_DIR.absolute()}[/cyan]\n")
        return {"added": 0, "skipped": 0, "errors": 0}

    console.print(f"\n[bold blue]📂 Found {len(cv_files)} CV file(s)[/bold blue]")

    results = {"added": 0, "skipped": 0, "errors": 0, "records": []}

    for i, cv_file in enumerate(cv_files, 1):
        console.print(f"\n[{i}/{len(cv_files)}] Processing: [cyan]{cv_file.name}[/cyan]")

        try:
            # Read file
            with console.status("  Reading file..."):
                raw_text = read_file(cv_file)

            if not raw_text or len(raw_text) < 100:
                console.print(f"  [yellow]⚠️  Could not extract text. Skipping.[/yellow]")
                results["skipped"] += 1
                continue

            console.print(f"  Extracted [green]{len(raw_text):,}[/green] characters")

            # Extract with Claude
            with console.status("  Claude is reading the CV..."):
                cv_info = extract_cv_info(claude, raw_text, cv_file.name)
                time.sleep(0.5)  # Small delay to be nice to API

            # Show preview
            if interactive:
                show_cv_preview(cv_info, cv_file.name)
                if not Confirm.ask("  Save this to Airtable?", default=True):
                    results["skipped"] += 1
                    continue

            # Add to Airtable
            with console.status("  Saving to Airtable..."):
                record_id = add_consultant_to_airtable(
                    tables, cv_info, raw_text, cv_file.stem
                )
                time.sleep(0.3)  # Airtable rate limit

            if record_id:
                console.print(f"  [green]✅ Added: {cv_info.get('full_name', cv_file.name)}[/green]")
                results["added"] += 1
                results["records"].append({
                    "name": cv_info.get("full_name"),
                    "airtable_id": record_id,
                    "file": cv_file.name
                })
            else:
                console.print(f"  [red]❌ Failed to save to Airtable[/red]")
                results["errors"] += 1

        except Exception as e:
            console.print(f"  [red]❌ Error: {e}[/red]")
            logger.exception(f"CV processing error for {cv_file.name}")
            results["errors"] += 1

    return results


def run_proposal_population(
    tables: dict,
    claude: anthropic.Anthropic,
    interactive: bool = True
) -> dict:
    """Process all proposal files and add to Airtable."""

    prop_files = get_files_in_folder(PROPOSALS_DIR)
    prop_files = [f for f in prop_files if "PUT_PROPOSAL" not in f.name.upper()]

    if not prop_files:
        console.print(f"[yellow]⚠️  No proposal files found in {PROPOSALS_DIR}[/yellow]")
        console.print(f"   Add PDF or DOCX files to: [cyan]{PROPOSALS_DIR.absolute()}[/cyan]\n")
        return {"added": 0, "skipped": 0, "errors": 0}

    console.print(f"\n[bold blue]📂 Found {len(prop_files)} proposal file(s)[/bold blue]")

    results = {"added": 0, "skipped": 0, "errors": 0, "records": []}

    for i, prop_file in enumerate(prop_files, 1):
        console.print(f"\n[{i}/{len(prop_files)}] Processing: [cyan]{prop_file.name}[/cyan]")

        try:
            with console.status("  Reading file..."):
                raw_text = read_file(prop_file)

            if not raw_text or len(raw_text) < 200:
                console.print(f"  [yellow]⚠️  Could not extract text. Skipping.[/yellow]")
                results["skipped"] += 1
                continue

            console.print(f"  Extracted [green]{len(raw_text):,}[/green] characters")

            with console.status("  Claude is analyzing the proposal..."):
                proposal_info = extract_proposal_info(claude, raw_text, prop_file.name)
                time.sleep(0.5)

            if interactive:
                show_proposal_preview(proposal_info, prop_file.name)

                # Allow manual corrections
                won_manual = Confirm.ask(
                    f"  Was this proposal [bold]WON[/bold]?",
                    default=bool(proposal_info.get("won", False))
                )
                proposal_info["won"] = won_manual

                if not Confirm.ask("  Save this to Airtable?", default=True):
                    results["skipped"] += 1
                    continue

            with console.status("  Saving to Airtable..."):
                record_id = add_proposal_to_airtable(
                    tables, proposal_info, raw_text, prop_file.stem
                )
                time.sleep(0.3)

            if record_id:
                title = proposal_info.get("project_title", prop_file.name)
                won_str = "✅ WON" if proposal_info.get("won") else "📋 Not won"
                console.print(f"  [green]✅ Added: {title[:50]} ({won_str})[/green]")
                results["added"] += 1
                results["records"].append({
                    "title": title,
                    "won": proposal_info.get("won"),
                    "airtable_id": record_id
                })
            else:
                console.print(f"  [red]❌ Failed to save to Airtable[/red]")
                results["errors"] += 1

        except Exception as e:
            console.print(f"  [red]❌ Error: {e}[/red]")
            logger.exception(f"Proposal processing error for {prop_file.name}")
            results["errors"] += 1

    return results


def show_final_summary(
    cv_results: dict,
    proposal_results: dict,
    rate_cards_added: int
) -> None:
    """Show final summary table of what was populated."""

    console.print()
    console.print(Panel.fit(
        "[bold green]🎉 Population Complete![/bold green]",
        border_style="green"
    ))

    table = Table(title="Population Summary", show_header=True)
    table.add_column("Table", style="bold cyan")
    table.add_column("Added", style="green", justify="right")
    table.add_column("Skipped", style="yellow", justify="right")
    table.add_column("Errors", style="red", justify="right")

    table.add_row(
        "CONSULTANTS (CVs)",
        str(cv_results.get("added", 0)),
        str(cv_results.get("skipped", 0)),
        str(cv_results.get("errors", 0))
    )
    table.add_row(
        "PAST PROPOSALS",
        str(proposal_results.get("added", 0)),
        str(proposal_results.get("skipped", 0)),
        str(proposal_results.get("errors", 0))
    )
    table.add_row("RATE CARDS", str(rate_cards_added), "—", "—")

    console.print(table)

    console.print()
    console.print("[bold]📋 Next Steps:[/bold]")
    console.print("  1. Open Airtable and review the records")
    console.print("  2. Fix any incorrect extractions manually")
    console.print("  3. Run the CV embedder to enable semantic search:")
    console.print("     [cyan]python scripts/embed_cvs.py[/cyan]")
    console.print("  4. Run your first pipeline test:")
    console.print("     [cyan]python main.py --once[/cyan]")
    console.print()
    console.print("[dim]Log file: populate_airtable.log[/dim]")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    console.print(Panel.fit(
        "[bold blue]🤖 Cortech Airtable Auto-Population Script[/bold blue]\n"
        "[dim]Reads your files and fills Airtable automatically[/dim]",
        border_style="blue"
    ))

    # 0. Setup folders
    setup_folders()

    # 1. Validate environment
    if not validate_environment():
        console.print("\n[red]Fix the errors above and run again.[/red]")
        sys.exit(1)

    # 2. Ask what to populate
    console.print()
    console.print("[bold]What would you like to populate?[/bold]")
    console.print("  1. CVs only")
    console.print("  2. Past Proposals only")
    console.print("  3. Rate Cards only")
    console.print("  4. Everything (CVs + Proposals + Rate Cards)")
    console.print("  5. Exit")

    choice = Prompt.ask("\nChoice", choices=["1", "2", "3", "4", "5"], default="4")

    if choice == "5":
        console.print("Goodbye!")
        sys.exit(0)

    # 3. Interactive mode?
    interactive = Confirm.ask(
        "\nReview each record before saving? (Recommended for first run)",
        default=True
    )

    # 4. Initialize clients
    tables = get_airtable_clients()
    claude = get_claude_client()

    cv_results     = {"added": 0, "skipped": 0, "errors": 0}
    prop_results   = {"added": 0, "skipped": 0, "errors": 0}
    rate_cards_added = 0

    # 5. Run selected populations
    if choice in ("3", "4"):
        console.print("\n[bold blue]💰 Populating Rate Cards...[/bold blue]")

        # Check if rate cards already exist
        existing = tables["rate_cards"].all(max_records=1)
        if existing:
            if Confirm.ask("Rate cards already exist. Overwrite?", default=False):
                rate_cards_added = populate_rate_cards(tables)
                console.print(f"  [green]✅ Added {rate_cards_added} rate card entries[/green]")
        else:
            rate_cards_added = populate_rate_cards(tables)
            console.print(f"  [green]✅ Added {rate_cards_added} rate card entries[/green]")

    if choice in ("1", "4"):
        console.print("\n[bold blue]👥 Processing CVs...[/bold blue]")
        cv_results = run_cv_population(tables, claude, interactive)

    if choice in ("2", "4"):
        console.print("\n[bold blue]📋 Processing Past Proposals...[/bold blue]")
        prop_results = run_proposal_population(tables, claude, interactive)

    # 6. Final summary
    show_final_summary(cv_results, prop_results, rate_cards_added)


if __name__ == "__main__":
    main()