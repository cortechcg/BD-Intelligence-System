"""
The style-guide and voice-exemplar extractors must read .pdf as well as
.docx, and "most recent" must mean the document's real date, not mtime.
"""
import os
from datetime import date
from pathlib import Path

import pytest
from docx import Document

import extract_style_guide as esg
import extract_voice_exemplars as eve
from utils import proposal_corpus as pc


def _make_docx(path: Path, heading: str, body: str) -> None:
    doc = Document()
    doc.add_heading(heading, level=1)
    doc.add_paragraph(body)
    doc.save(str(path))


def _make_pdf(path: Path, lines: list[str]) -> None:
    """Minimal single-page text PDF with a real text layer (no extra deps)."""
    content_ops = ["BT", "/F1 12 Tf", "72 760 Td", "14 TL"]
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_ops.append(f"({safe}) Tj T*")
    content_ops.append("ET")
    stream = "\n".join(content_ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))


PROSE = (
    "Cortech will apply a mixed-methods design that sequences a household "
    "survey with key informant interviews and focus group discussions, so "
    "that the quantitative estimates of coverage are explained by the "
    "qualitative account of why services are or are not used. Enumerators "
    "are recruited locally, trained for four days and supervised daily, "
    "and every questionnaire is scripted for tablet-based collection."
)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    d = tmp_path / "proposals"
    d.mkdir()
    _make_docx(d / "20240506_Technical Proposal Alpha.docx", "Methodology", PROSE)
    _make_pdf(
        d / "20250317_Technical Proposal Beta.pdf",
        ["TECHNICAL PROPOSAL", "1. Methodology"] + PROSE.split(". "),
    )
    _make_pdf(d / "EOI Gamma.pdf", ["EXPRESSION OF INTEREST", "2. Our Approach"] + PROSE.split(". "))
    (d / "~$Technical Proposal Alpha.docx").write_text("lock")
    (d / "PUT_PROPOSAL_FILES_HERE.txt").write_text("")
    return d


# ---------------------------------------------------------------- style guide

def test_style_guide_collects_pdf_and_docx(corpus: Path):
    structures, skipped_pdfs, skipped_temp = esg.collect_structures(corpus)
    by_name = {s["file"]: s for s in structures}

    assert "20240506_Technical Proposal Alpha.docx" in by_name
    assert "20250317_Technical Proposal Beta.pdf" in by_name
    assert "EOI Gamma.pdf" in by_name
    assert skipped_pdfs == []
    assert [p.name for p in skipped_temp] == ["~$Technical Proposal Alpha.docx"]

    pdf = by_name["20250317_Technical Proposal Beta.pdf"]
    assert pdf["source"] == "pdf"
    assert pdf["word_count"] > 40
    heading_texts = [h["text"] for h in pdf["headings"]]
    assert "TECHNICAL PROPOSAL" in heading_texts
    assert "1. Methodology" in heading_texts
    assert all(h["level"] == esg.PDF_HEADING_LEVEL for h in pdf["headings"])

    # PDFs flow through the same classifier as .docx
    assert esg.classify(pdf) == "full_proposal"
    assert esg.classify(by_name["EOI Gamma.pdf"]) == "eoi"


def test_style_guide_skips_pdf_without_text_layer_but_keeps_the_rest(corpus: Path):
    # A PDF with no text layer (scanned image) is skipped with a reason;
    # it must not take the rest of the corpus down with it.
    _make_pdf(corpus / "Technical Proposal Scanned.pdf", [])
    (corpus / "Technical Proposal Broken.pdf").write_bytes(b"%PDF-1.4 garbage")

    structures, skipped_pdfs, _ = esg.collect_structures(corpus)
    names = {s["file"] for s in structures}
    assert "20250317_Technical Proposal Beta.pdf" in names
    assert "Technical Proposal Scanned.pdf" not in names
    assert {p.name for p in skipped_pdfs} == {
        "Technical Proposal Scanned.pdf",
        "Technical Proposal Broken.pdf",
    }


# ------------------------------------------------------------ voice exemplars

def test_voice_exemplars_harvest_reads_pdf_prose_under_detected_heading(corpus: Path):
    harvested = eve.harvest(corpus / "20250317_Technical Proposal Beta.pdf")
    assert "methodology" in harvested
    assert "mixed-methods design" in harvested["methodology"][0]

    harvested_docx = eve.harvest(corpus / "20240506_Technical Proposal Alpha.docx")
    assert "methodology" in harvested_docx


def test_voice_exemplars_corpus_includes_pdf_and_excludes_lock_files(corpus: Path):
    names = [p.name for p in eve.corpus_files(corpus)]
    assert "20250317_Technical Proposal Beta.pdf" in names
    assert "EOI Gamma.pdf" in names
    assert "20240506_Technical Proposal Alpha.docx" in names
    assert not any(n.startswith("~$") for n in names)
    assert "PUT_PROPOSAL_FILES_HERE.txt" not in names


# ------------------------------------------------------------ recency ranking

def test_recency_uses_filename_date_not_mtime(tmp_path: Path):
    older = tmp_path / "20240209_Technical Proposal Old.docx"
    newer = tmp_path / "20250317_Technical Proposal New.pdf"
    _make_docx(older, "Methodology", PROSE)
    _make_pdf(newer, ["1. Methodology"] + PROSE.split(". "))

    # Identical mtime for both, and the OLDER file gets the LATER mtime so
    # an mtime sort would put it first.
    same = 1_700_000_000
    os.utime(newer, (same, same))
    os.utime(older, (same + 3600, same + 3600))

    dated, undated = pc.rank_by_date([older, newer])
    assert undated == []
    assert [p.name for p, _, _ in dated] == [newer.name, older.name]
    assert dated[0][1] == date(2025, 3, 17)
    assert dated[0][2] == "filename"

    assert eve.most_recent([older, newer], 1) == [newer]


def test_recency_falls_back_to_cover_date_and_excludes_undated(tmp_path: Path):
    cover_dated = tmp_path / "Technical Proposal Cover.pdf"
    _make_pdf(cover_dated, ["Technical Proposal", "Nairobi, 14 August 2025", "1. Methodology"])
    undated = tmp_path / "Technical Proposal Nodate.docx"
    _make_docx(undated, "Methodology", PROSE)
    prefixed = tmp_path / "20240506_Technical Proposal Prefixed.docx"
    _make_docx(prefixed, "Methodology", PROSE)

    dated, left_out = pc.rank_by_date([undated, prefixed, cover_dated])
    assert [p.name for p, _, _ in dated] == [cover_dated.name, prefixed.name]
    assert dated[0][1] == date(2025, 8, 14)
    assert dated[0][2] == "cover"
    assert left_out == [undated]
    assert eve.most_recent([undated, prefixed, cover_dated], 5) == [cover_dated, prefixed]


def test_pdf_heading_heuristic_is_conservative():
    assert pc.is_pdf_heading("1. Methodology")
    assert pc.is_pdf_heading("2.1 Data Collection Approach")
    assert pc.is_pdf_heading("OUR UNDERSTANDING OF YOUR NEEDS")
    assert pc.is_pdf_heading("Annex A Team Composition")
    # lowercase fragments and sentence pieces are not headings
    assert not pc.is_pdf_heading("then requirement is")
    assert not pc.is_pdf_heading("into the East Africa")
    assert not pc.is_pdf_heading("Beyond your expectations")
    assert not pc.is_pdf_heading("The consultant will deliver the report.")
    assert not pc.is_pdf_heading("PROJECTPURPOSEANDOBJECTIVESSUMMARY ExpectedOutcome")
    assert not pc.is_pdf_heading("Introduction ........................ 3")


def test_cover_date_ignores_dates_embedded_in_prose():
    prose = (
        "Cortech successfully delivered the SUSTFARM+ endline evaluation for WHH "
        "in April 2025 and the programme started implementation in September 2020."
    )
    assert pc.extract_cover_date(prose) is None
    # A short standalone line is a cover/letter date, in any of the supported forms.
    assert pc.extract_cover_date(prose + "\nNairobi – 8 April 2026\nTo the Procurement Committee") == date(2026, 4, 8)
    assert pc.extract_cover_date("Submitted To\nUNDP- Somalia\nBy\n\n07 November 2024") == date(2024, 11, 7)
    assert pc.extract_cover_date("\tJuly 2025\nTable of Contents") == date(2025, 7, 1)
    # Right-aligned letter date: short once internal whitespace collapses.
    assert pc.extract_cover_date("COVER LETTER" + " " * 100 + "10th November 2025") == date(2025, 11, 10)
    # Filename-body dates still parse regardless of stem length.
    assert pc.parse_filename_date(
        "Technical Proposal _DRC Kenya- Business Needs & Mapping of BDS Providers  _ RegioTrade_ Final_14122024.pdf"
    ) == date(2024, 12, 14)
