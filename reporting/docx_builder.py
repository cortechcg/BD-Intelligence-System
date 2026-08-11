"""
Renders a generate_proposal() sections dict into a formatted .docx file
for email attachment. Must handle PARTIAL section dicts too — the
lightweight WATCH-tier path only produces cover_letter and
executive_summary, not all 10 keys.
"""
import os
from datetime import datetime
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from loguru import logger

# Order matters — this is the real proposal structure, not dict
# insertion order. The 4 grouped generator functions map onto these
# slots (org_profile_and_track_record covers what would otherwise be
# two separate PROPOSAL_STRUCTURE entries, etc.) — see proposal_writer.py.
SECTION_ORDER = [
    ("cover_letter", "Cover Letter"),
    ("firm_profile", "Firm Profile"),
    ("relevant_experience", "Relevant Experience"),
    ("key_experts", "Proposed Key Experts"),
    ("executive_summary", "Executive Summary"),
    ("org_profile_and_track_record", "Organisational Profile & Track Record"),
    ("introduction_and_framework", "Introduction, Background & Conceptual Framework"),
    ("methodology", "Technical Methodology"),
    ("analysis_plan", "Sampling Strategy & Data Analysis Plan"),
    ("qa_and_ethics", "Quality Assurance & Ethical Considerations"),
    ("risk_register", "Risk Management"),
    ("team_section", "Proposed Team & Core Experts"),
    ("work_plan", "Work Plan & Timeline"),
]


def _insert_toc_field(doc: Document) -> None:
    """
    Inserts a real, clickable, page-numbered Word TOC field. python-docx
    has no built-in TOC helper — this is the standard raw-OXML field-code
    approach. Word computes the actual page numbers when the file is
    opened; some Word versions show "Right-click → Update Field" on
    first open instead of populating instantly — that's normal Word
    behavior, not a bug in this code.

    If this proves fragile in testing, an acceptable fallback is a
    static list of section names with no page numbers, added as plain
    paragraphs instead — flag it rather than spending excess time
    fighting Word's field-code quirks.
    """
    paragraph = doc.add_paragraph()
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = 'TOC \\o "1-1" \\h \\z \\u'
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    for el in (fld_begin, instr, fld_sep, fld_end):
        run._r.append(el)


def build_proposal_docx(
    sections: dict,
    opportunity: dict,
    output_dir: str = "output/proposals",
) -> str:
    """
    Builds a formatted .docx from a proposal sections dict. Returns the
    file path. Sections missing from `sections` are skipped silently —
    this must not crash on a partial (lightweight/WATCH-tier) dict.
    """
    os.makedirs(output_dir, exist_ok=True)
    doc = Document()

    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    # Title page
    header = doc.add_paragraph()
    header.alignment = WD_ALIGN_PARAGRAPH.CENTER
    header_run = header.add_run("CORTECH CONSULTING GROUP")
    header_run.bold = True
    header_run.font.size = Pt(16)
    header_run.font.color.rgb = RGBColor(0x14, 0x21, 0x3D)

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc_type = (
        "Expression of Interest"
        if sections.get("submission_type") == "EOI"
        else "Technical Proposal"
    )
    subtitle.add_run(doc_type).italic = True

    doc.add_paragraph()

    opp_title = doc.add_paragraph()
    opp_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = opp_title.add_run(opportunity.get("title", "Untitled Assignment"))
    title_run.bold = True
    title_run.font.size = Pt(14)

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta.add_run(f"Client: {opportunity.get('client', 'N/A')}\n")
    meta.add_run("Submitted by: Cortech Consulting Group\n")
    meta.add_run(f"Date: {datetime.now().strftime('%d %B %Y')}")

    doc.add_page_break()
    _insert_toc_field(doc)
    doc.add_page_break()

    sections_included = 0
    for key, heading in SECTION_ORDER:
        content = sections.get(key)
        if not content or not isinstance(content, str):
            continue
        doc.add_heading(heading, level=1)
        for para in content.split("\n\n"):
            para = para.strip()
            if para:
                doc.add_paragraph(para)
        doc.add_page_break()
        sections_included += 1

    if sections_included == 0:
        logger.warning("build_proposal_docx: no sections had content — file will be nearly empty")

    safe_title = "".join(
        c for c in opportunity.get("title", "proposal") if c.isalnum() or c in " -_"
    )[:60].strip() or "proposal"
    file_path = os.path.join(output_dir, f"{safe_title}.docx")
    doc.save(file_path)
    logger.success(f"Built proposal docx ({sections_included} sections): {file_path}")
    return file_path
