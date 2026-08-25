"""
Extracts real prose from the submitted proposals in data/proposals/ so new
drafts sound like Cortech's own winning submissions rather than like a
generic consulting template.

extract_style_guide.py already captures the SKELETON of the corpus (which
headings appear, in what order). That told the writer what to call a
section but nothing about how Cortech actually writes one. This pulls the
prose itself: real paragraphs, grouped by section, written to
intelligence/style_guides/voice_exemplars.md, which proposal_writer.py
loads per section at draft time.

Purely mechanical — no Claude calls, so it is cheap to re-run whenever the
corpus changes:
  python extract_voice_exemplars.py
  python extract_voice_exemplars.py --docs 12   # widen the sampled corpus

Every excerpt is passed through utils.money_scrub first. Exemplars carrying
figures would teach the model to write figures back in, which is exactly
what the technical proposal must never contain.
"""
import argparse
import re
from pathlib import Path

from docx import Document
from loguru import logger

from utils.money_scrub import strip_monetary_amounts

OUT_PATH = Path("intelligence/style_guides/voice_exemplars.md")

# Checked in order — first match wins, so specific patterns precede the
# generic ones. "understanding" is last because "Introduction" and
# "Background" are catch-alls that would otherwise swallow real
# methodology and profile headings.
SECTION_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("cover_letter", (
        "cover letter", "letter of interest", "letter of transmittal",
        "letter of expression", "expression of interest letter",
    )),
    ("executive_summary", ("executive summary", "abstract",)),
    ("org_profile", (
        "organisational profile", "organizational profile", "about cortech",
        "consultant's organisation", "consultants organisation",
        "consultant's organization", "presentation of cortech",
        "our expertise", "firm profile", "company profile", "who we are",
    )),
    ("experience", (
        "previous assignments", "relevant experience", "our experience",
        "relevant project experience", "similar assignments", "track record",
        "past performance", "relevant assignments",
    )),
    ("risk", ("risk",)),
    ("work_plan", (
        "work plan", "workplan", "timeline", "gantt", "schedule",
        "implementation plan", "phasing",
    )),
    ("team", (
        "core personnel", "core experts", "team composition", "proposed team",
        "key experts", "resources in staff", "personnel", "staffing",
        "team structure", "proposed experts",
    )),
    ("qa_ethics", (
        "quality assurance", "quality control", "ethical", "ethics",
        "safeguarding", "do no harm", "data protection", "confidentiality",
    )),
    ("analysis", (
        "sampling", "sample size", "data analysis", "analysis plan",
        "data management", "analytical approach",
    )),
    ("methodology", (
        "methodology", "technical approach", "approach and method",
        "proposed approach", "evaluation design", "study design",
        "survey design", "research design", "data collection",
        "inception", "field work", "fieldwork", "reporting phase",
        "conceptual framework", "theory of change",
    )),
    ("understanding", (
        "understanding of the terms of reference",
        "understanding the terms of reference",
        "understanding of the assignment", "interpretation",
        "scope of work", "introduction", "background", "context",
        "purpose", "objectives",
    )),
]

# Budget/financial headings are skipped outright — no exemplar should ever
# demonstrate how to write about price in a technical proposal.
EXCLUDED_HEADING_TERMS = (
    "budget", "financial", "cost", "price", "fee", "rate card", "annex",
    "table of contents", "abbreviation", "acronym", "declaration",
    "registration", "reference",
)

MIN_PARA_CHARS = 260
MIN_PARA_WORDS = 40
WORDS_PER_DOC = 230
WORDS_PER_SECTION = 430
DOCS_PER_SECTION = 2

_TOC_RE = re.compile(r"\.{4,}|\t")


def _section_for(heading: str) -> str | None:
    h = heading.lower().strip()
    if not h or len(h) > 140:
        return None
    if any(term in h for term in EXCLUDED_HEADING_TERMS):
        return None
    for key, patterns in SECTION_PATTERNS:
        if any(p in h for p in patterns):
            return key
    return None


def _is_prose(text: str) -> bool:
    """Body prose, not a heading, caption, table remnant, or TOC line."""
    t = text.strip()
    if len(t) < MIN_PARA_CHARS or len(t.split()) < MIN_PARA_WORDS:
        return False
    if _TOC_RE.search(t):
        return False
    if t.startswith(("Table ", "Figure ", "Annex ", "Source:", "Note:")):
        return False
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.4:
        return False
    # A paragraph that is mostly digits is a table row flattened into text.
    return sum(c.isdigit() for c in t) / len(t) <= 0.12


def harvest(docx_path: Path) -> dict[str, list[str]]:
    """Section key → prose excerpts found under matching headings."""
    try:
        doc = Document(str(docx_path))
    except Exception as e:
        logger.warning(f"Could not open {docx_path.name}: {e}")
        return {}

    found: dict[str, list[str]] = {}
    current: str | None = None
    words_taken: dict[str, int] = {}

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if para.style.name.startswith("Heading"):
            current = _section_for(text)
            continue
        if current is None or words_taken.get(current, 0) >= WORDS_PER_DOC:
            continue
        if not _is_prose(text):
            continue
        cleaned, _ = strip_monetary_amounts(text)
        if not _is_prose(cleaned):
            continue
        found.setdefault(current, []).append(cleaned)
        words_taken[current] = words_taken.get(current, 0) + len(cleaned.split())

    return found


def build(corpus: list[Path]) -> dict[str, list[tuple[str, str]]]:
    """Section key → [(source filename, excerpt)], capped per section."""
    by_section: dict[str, list[tuple[str, str]]] = {}
    for path in corpus:
        harvested = harvest(path)
        if not harvested:
            continue
        logger.info(f"  {path.name}: {', '.join(sorted(harvested))}")
        for key, paragraphs in harvested.items():
            bucket = by_section.setdefault(key, [])
            if len({src for src, _ in bucket}) >= DOCS_PER_SECTION:
                continue
            used = sum(len(x.split()) for _, x in bucket)
            if used >= WORDS_PER_SECTION:
                continue
            excerpt = "\n\n".join(paragraphs).strip()
            words = excerpt.split()
            allowance = min(WORDS_PER_DOC, WORDS_PER_SECTION - used)
            if len(words) > allowance:
                excerpt = " ".join(words[:allowance]).rstrip(",;:") + " …"
            bucket.append((path.name, excerpt))
    return by_section


def write_file(by_section: dict[str, list[tuple[str, str]]], corpus_size: int) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Cortech house voice — real excerpts from submitted proposals",
        "",
        f"*Extracted mechanically from {corpus_size} documents in data/proposals/ "
        "by extract_voice_exemplars.py. Monetary figures have been stripped. "
        "Re-run the script when the corpus changes.*",
        "",
    ]
    for key in sorted(by_section):
        lines.append(f"<!-- section: {key} -->")
        for source, excerpt in by_section[key]:
            lines.append(f"### {key} — from {source}")
            lines.append("")
            lines.append(excerpt)
            lines.append("")
    OUT_PATH.write_text("\n".join(lines))

    total = sum(len(v) for v in by_section.values())
    words = sum(len(x.split()) for v in by_section.values() for _, x in v)
    logger.success(
        f"Wrote {OUT_PATH} — {len(by_section)} sections, {total} excerpts, "
        f"~{words:,} words"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Cortech house-voice exemplars from data/proposals/"
    )
    parser.add_argument(
        "--docs",
        type=int,
        default=18,
        help="How many of the most recent .docx proposals to sample (default 18)",
    )
    args = parser.parse_args()

    proposals_dir = Path("data/proposals")
    if not proposals_dir.is_dir():
        logger.error(f"Missing directory: {proposals_dir}")
        return

    # Most recent first — recent submissions reflect the current house voice,
    # and are the ones the team is actually happy to be judged on.
    candidates = sorted(
        (
            f for f in proposals_dir.glob("*.docx")
            if f.is_file() and not f.name.startswith("~$")
        ),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[: args.docs]

    if not candidates:
        logger.error("No .docx proposals found in data/proposals/")
        return

    logger.info(f"Harvesting prose from {len(candidates)} proposals")
    by_section = build(candidates)
    if not by_section:
        logger.error("No usable excerpts found — check heading styles in the corpus")
        return
    write_file(by_section, len(candidates))


if __name__ == "__main__":
    main()
