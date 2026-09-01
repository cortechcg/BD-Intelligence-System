from intelligence.tender_reader import _BRIEF_PROMPT, _BRIEF_PROMPT_EOI
from reporting.docx_builder import SECTION_ORDER


def test_eoi_brief_is_shortlisting_not_full_proposal():
    assert "Shortlisting" in _BRIEF_PROMPT_EOI
    assert "Expression of Interest" in _BRIEF_PROMPT_EOI
    assert "must not" in _BRIEF_PROMPT_EOI.lower()
    assert "Gantt" in _BRIEF_PROMPT_EOI or "full technical" in _BRIEF_PROMPT_EOI.lower()


def test_proposal_brief_still_has_award_criteria():
    assert "Scored criteria" in _BRIEF_PROMPT
    assert "technical proposal" in _BRIEF_PROMPT.lower()


def test_docx_order_includes_full_eoi_backbone():
    keys = [k for k, _ in SECTION_ORDER]
    for required in (
        "cover_letter",
        "firm_profile",
        "understanding",
        "approach_summary",
        "relevant_experience",
        "key_experts",
        "eligibility",
        "compliance_matrix",
    ):
        assert required in keys
    assert keys.index("understanding") < keys.index("relevant_experience")
    assert keys.index("approach_summary") < keys.index("eligibility")
