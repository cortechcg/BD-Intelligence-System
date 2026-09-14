"""Selection rules for past-proposal ingest (financial-only, fragments, dupes)."""

from pathlib import Path

from populate_airtable import (
    _corpus_key,
    _proposal_skip_reason,
    select_proposal_files,
)


def test_skips_financial_only_and_draft_fragments():
    assert _proposal_skip_reason(Path("Financial Proposal_PWJ.docx"))
    assert _proposal_skip_reason(
        Path("20241108_Cortech Consulting Group_ Financial Proposal_ LWF GEDSI.docx")
    )
    assert not _proposal_skip_reason(
        Path("Technical and Financial Proposal_NRC Market Survey.pdf")
    )
    assert _proposal_skip_reason(
        Path("Introduction_ Draft Input for the technical proposal.docx")
    )


def test_prefers_docx_over_pdf_and_drops_copy_suffix():
    files = [
        Path("Technical Proposal_ Somalia Nutrition Cluster Strategy and Response Plan.pdf"),
        Path("Technical Proposal_ Somalia Nutrition Cluster Strategy and Response Plan.docx"),
        Path("Technical Proposal_ DRC Mapping Formal Financial Service Providers for Refugees (2).docx"),
        Path("Technical Proposal_ DRC Mapping Formal Financial Service Providers for Refugees.docx"),
    ]
    kept, skipped = select_proposal_files(files)
    names = {p.name for p in kept}
    assert "Technical Proposal_ Somalia Nutrition Cluster Strategy and Response Plan.docx" in names
    assert "Technical Proposal_ DRC Mapping Formal Financial Service Providers for Refugees.docx" in names
    reasons = " ".join(r for _, r in skipped)
    assert "duplicate" in reasons


def test_pgi_variants_collapse_to_one():
    files = [
        Path("20241018_Cortech Consulting Group_Technical_ PGI.docx"),
        Path("Technical Proposal PGI (1).docx"),
    ]
    kept, skipped = select_proposal_files(files)
    assert [p.name for p in kept] == [
        "20241018_Cortech Consulting Group_Technical_ PGI.docx"
    ]
    assert len(skipped) == 1


def test_extracted_title_matches_existing_airtable_row():
    from populate_airtable import _title_already_stored

    existing = [
        "third party monitoring consultancy for the girls education challenge"
    ]
    assert _title_already_stored(
        "Third Party Monitoring Consultancy for the Girls' Education Challenge",
        existing,
    )
    assert not _title_already_stored("Short", existing)


def test_iom_fmp_keeps_latest_cortech_spelling():
    files = [
        Path("20241009_ Crtech Consulting Group_Technical and Financial Proposal_IoM_Monitoring Framework and Scorecard on the Implementation of FMP.pdf"),
        Path("20241009_ Cortech Consulting Group_Technical and Financial Proposal_IoM_Monitoring Framework and Scorecard on the Implementation of FMP.pdf"),
        Path("20241111_ Cortech Consulting Group_Technical Proposal_IoM_Monitoring Framework and Scorecard on the Implementation of FMP.pdf"),
    ]
    kept, skipped = select_proposal_files(files)
    assert len(kept) == 1
    assert kept[0].name.startswith("20241111_")
    assert len(skipped) == 2
    assert _corpus_key(files[0]) == _corpus_key(files[2])
