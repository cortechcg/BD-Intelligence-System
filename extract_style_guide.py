"""
Reads every real past proposal/EOI in data/proposals/, extracts each
document's ACTUAL heading structure (not a content summary — the
structural skeleton), classifies each as EOI or full-proposal, and asks
Claude to synthesize a written structure/style guide per type from the
aggregated real examples.

Run manually when the corpus changes:
  python extract_style_guide.py           # extract + synthesize guides
  python extract_style_guide.py --compare # diff corpus vs PROPOSAL_STRUCTURE
"""
import argparse
import os
import re
from pathlib import Path

import anthropic
from docx import Document
from loguru import logger

from config import CLAUDE_MODEL_PROPOSAL
from intelligence.proposal_writer import PROPOSAL_STRUCTURE
from utils.claude_helpers import get_text

client = anthropic.Anthropic()


def extract_structure(docx_path: Path) -> dict:
    doc = Document(str(docx_path))
    headings = []
    word_count = 0
    for para in doc.paragraphs:
        word_count += len(para.text.split())
        if para.style.name.startswith("Heading"):
            headings.append({"level": para.style.name, "text": para.text.strip()})
    return {"file": docx_path.name, "headings": headings, "word_count": word_count}


def classify(structure: dict) -> str:
    """
    Filename is the primary, more reliable signal — real EOIs in this
    corpus are already named clearly. Word count is a fallback only,
    for anything not obviously named either way.
    """
    name_lower = structure["file"].lower()

    if "technical proposal" in name_lower or "tehnical proposal" in name_lower:
        return "full_proposal"
    if "funding proposal" in name_lower:
        return "full_proposal"

    if (
        "expression of interest" in name_lower
        or "expresion of interest" in name_lower
        or " eoi" in name_lower
        or name_lower.startswith("eoi")
        or "reoi" in name_lower
        or "concept note" in name_lower
    ):
        return "eoi"

    # Word-count fallback — only when doc has enough text to be meaningful
    if 300 <= structure["word_count"] < 1500:
        return "eoi"

    return "full_proposal"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _proposal_structure_sections() -> list[str]:
    sections = []
    for line in PROPOSAL_STRUCTURE.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("PROPOSAL SECTIONS"):
            continue
        # "1. COVER LETTER (...)" or "   5.1 Context ..."
        cleaned = re.sub(r"^\d+(\.\d+)?\.\s*", "", line)
        cleaned = cleaned.split("(")[0].strip()
        if cleaned and not cleaned[0].isdigit():
            sections.append(cleaned)
    return sections


def _heading_matches_section(heading: str, section: str) -> bool:
    h = _normalize(heading)
    s = _normalize(section)
    if not h or not s:
        return False
    # Substring either way — headings are shorter/messier than template labels
    return s in h or h in s or any(
        token in h for token in s.split() if len(token) > 4
    )


def compare_structure(full_docs: list[dict]) -> None:
    """Surface diff between real full-proposal headings and PROPOSAL_STRUCTURE."""
    template_sections = _proposal_structure_sections()
    corpus_headings: list[str] = []
    for doc in full_docs:
        for h in doc["headings"]:
            if h["text"]:
                corpus_headings.append(h["text"])

    unique_headings = sorted(set(corpus_headings), key=str.lower)

    print("\n" + "=" * 60)
    print("  PROPOSAL_STRUCTURE vs real corpus (full proposals only)")
    print("=" * 60)

    print("\nTemplate sections with NO similar heading in corpus:")
    unmatched_template = []
    for section in template_sections:
        if not any(_heading_matches_section(h, section) for h in unique_headings):
            unmatched_template.append(section)
            print(f"  - {section}")
    if not unmatched_template:
        print("  (none — every template section has at least one corpus match)")

    print("\nCorpus headings NOT represented in PROPOSAL_STRUCTURE:")
    unmatched_headings = []
    for heading in unique_headings:
        if not any(_heading_matches_section(heading, section) for section in template_sections):
            unmatched_headings.append(heading)
            print(f"  - {heading}")
    if not unmatched_headings:
        print("  (none — every corpus heading maps to a template section)")

    print(
        f"\nSummary: {len(template_sections)} template sections, "
        f"{len(unique_headings)} unique corpus headings, "
        f"{len(unmatched_template)} template gaps, "
        f"{len(unmatched_headings)} corpus-only headings"
    )


def collect_structures(proposals_dir: Path) -> tuple[list[dict], list[Path], list[Path]]:
    all_files = [f for f in proposals_dir.glob("*") if f.is_file()]

    pdfs = [f for f in all_files if f.suffix.lower() == ".pdf"]
    for pdf in pdfs:
        logger.warning(f"Skipping PDF (no reliable heading structure): {pdf.name}")

    skipped_temp = []
    docx_files = []
    for f in all_files:
        if f.suffix.lower() != ".docx":
            continue
        if f.name.startswith("~$"):
            skipped_temp.append(f)
            logger.warning(f"Skipping Word temp/lock file: {f.name}")
            continue
        docx_files.append(f)

    structures = [extract_structure(f) for f in docx_files]
    return structures, pdfs, skipped_temp


def synthesize_guides(eoi_docs: list[dict], full_docs: list[dict]) -> None:
    os.makedirs("intelligence/style_guides", exist_ok=True)

    for label, docs in [("full_proposal", full_docs), ("eoi", eoi_docs)]:
        if not docs:
            logger.warning(f"No documents classified as {label} — skipping this guide")
            continue

        headings_summary = "\n\n".join(
            f"[{d['file']}] ({d['word_count']} words)\n"
            + "\n".join(f"  {h['level']}: {h['text']}" for h in d["headings"])
            for d in docs
        )

        prompt = f"""Below are the actual heading structures from {len(docs)} real {label.replace('_', ' ')} documents Cortech Consulting Group has submitted.

{headings_summary}

Synthesize a written structure and style guide grounded in these REAL examples:
1. The common section order/pattern actually used across these documents — not a generic proposal template, what Cortech actually does
2. Where structure varies between documents, describe the variation honestly rather than picking one and presenting it as universal
3. Any formatting conventions visible from the heading text itself (numbering style, capitalization, whether tables appear to be used based on heading names like "Team" or "Budget")

Write as clear guidance for someone drafting a new {label.replace('_', ' ')} for Cortech, citing which real document(s) a pattern comes from where useful."""

        response = client.messages.create(
            model=CLAUDE_MODEL_PROPOSAL,
            max_tokens=1800,
            messages=[{"role": "user", "content": prompt}],
        )
        guide = get_text(response)

        out_path = f"intelligence/style_guides/{label}_style.md"
        with open(out_path, "w") as f:
            f.write(guide)
        logger.success(f"Wrote {out_path} from {len(docs)} real documents")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Cortech proposal style guides from data/proposals/")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare full-proposal corpus headings against PROPOSAL_STRUCTURE (no Claude calls)",
    )
    args = parser.parse_args()

    proposals_dir = Path("data/proposals")
    if not proposals_dir.is_dir():
        logger.error(f"Missing directory: {proposals_dir}")
        return

    structures, pdfs, _ = collect_structures(proposals_dir)
    eoi_docs = [s for s in structures if classify(s) == "eoi"]
    full_docs = [s for s in structures if classify(s) == "full_proposal"]

    logger.info(
        f"Classified: {len(eoi_docs)} EOI, {len(full_docs)} full proposal, "
        f"{len(pdfs)} skipped (PDF)"
    )

    if args.compare:
        compare_structure(full_docs)
        return

    compare_structure(full_docs)
    synthesize_guides(eoi_docs, full_docs)


if __name__ == "__main__":
    main()
