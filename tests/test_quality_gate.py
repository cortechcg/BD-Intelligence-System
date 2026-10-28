"""Quality gate: score the written draft, block what a committee would mark down."""
from __future__ import annotations

from intelligence import quality_gate as qg
from reporting import email_report

FILLER = (
    "Cortech will apply this on the Dollo Ado baseline with a stratified household "
    "sample, structured child-protection tools in Somali and English, supervised "
    "enumerators, daily data checks, and a validation workshop with the client so "
    "every finding is traceable to the agreed indicators and the inception report."
)


def _analysis(weights=(60, 40), names=("Child protection sampling design", "Somali enumerator supervision")):
    criteria = []
    for name, weight in zip(names, weights):
        item = {"criterion": name, "description": ""}
        if weight is not None:
            item["weight_percent"] = weight
        criteria.append(item)
    return {"evaluation_criteria": criteria, "opportunity": {"title": "Baseline"}}


def _answer(name: str) -> str:
    """One scored paragraph: the criterion's own words inside a long answer."""
    return f"Our {name.lower()} on this assignment: {FILLER[0].lower()}{FILLER[1:]}"


def _clean_sections() -> dict:
    return {
        "methodology": _answer("Child protection sampling design") + "\n\n" + FILLER,
        "team_section": _answer("Somali enumerator supervision") + "\n\n" + FILLER,
        "claim_grounding": {"claims": []},
    }


# ── Criteria source: never invent a weight ─────────────────────────────────

def test_tor_weights_are_used_when_stated():
    criteria, source = qg.gate_criteria(_analysis(), {})
    assert source == qg.SOURCE_TOR
    assert [c["weight_percent"] for c in criteria] == [60.0, 40.0]


def test_unweighted_tor_gets_no_weight_figure():
    criteria, source = qg.gate_criteria(_analysis(weights=(None, None)), {})
    assert source == qg.SOURCE_TOR_UNWEIGHTED
    assert all(c["weight_percent"] is None for c in criteria)
    assert "no weight figure is claimed" in qg.SOURCE_NOTES[source]


def test_falls_back_to_the_readers_criteria():
    brief = (
        "## Scored criteria and the evidence each one wants\n\n"
        "- **Scoring matrix:** None stated in the source document. Do not invent weights.\n"
        "- **Proposed technical approach** — an evaluation matrix mapped to OECD-DAC.\n"
        "1. **Organisation's experience in the region** (10 points) — similar work.\n"
        "## Compliance requirements\n- Deadline\n"
    )
    criteria, source = qg.gate_criteria({"evaluation_criteria": []}, {"tender_brief": brief})
    assert source == qg.SOURCE_READER
    assert [c["criterion"] for c in criteria] == [
        "Proposed technical approach",
        "Organisation's experience in the region",
    ]
    assert all(c["weight_percent"] is None for c in criteria)


def test_falls_back_to_standard_criteria_marked_inferred():
    criteria, source = qg.gate_criteria({}, {"tender_brief": "## What the client is buying\nx"})
    assert source == qg.SOURCE_STANDARD
    assert criteria and all(c["criterion"].endswith("(inferred)") for c in criteria)
    gate = qg.score_draft(_clean_sections(), {})
    assert all(row["inferred"] for row in gate["rows"])
    assert "INFERRED" in gate["headline"]


def test_a_weight_the_tor_does_not_state_blocks_the_draft():
    sections = _clean_sections()
    sections["methodology"] += "\n\nThe technical proposal carries 80% of the evaluation score."
    unweighted = qg.score_draft(sections, _analysis(weights=(None, None)))
    assert unweighted["status"] == qg.BLOCKED
    assert unweighted["blocker_counts"].get("invented_weight") == 1
    weighted = qg.score_draft(sections, _analysis())
    assert "invented_weight" not in weighted["blocker_counts"]


# ── Score the actual draft ─────────────────────────────────────────────────

def test_a_clean_draft_that_answers_every_criterion_is_ready():
    gate = qg.score_draft(_clean_sections(), _analysis())
    assert gate["blockers"] == []
    assert gate["score"] == 100.0
    assert gate["status"] == qg.READY


def test_below_the_bar_is_blocked_even_without_blockers():
    sections = _clean_sections()
    analysis = _analysis(
        weights=(40, 60),
        names=("Child protection sampling design", "Value-for-money costing expertise"),
    )
    gate = qg.score_draft(sections, analysis)  # the 60-point criterion is never answered
    assert gate["blockers"] == []
    assert gate["score"] < qg.QUALITY_BAR
    assert gate["status"] == qg.BLOCKED


def test_a_section_resting_on_insufficient_evidence_earns_zero():
    sections = _clean_sections()
    sections["methodology"] += "\n\nSample size: [INSUFFICIENT EVIDENCE]."
    gate = qg.score_draft(sections, _analysis())
    row = next(r for r in gate["rows"] if r["criterion"].startswith("Child protection"))
    assert row["earned"] == 0
    assert "Earns zero" in row["reason"]
    assert gate["score"] == 40.0
    assert gate["status"] == qg.BLOCKED


def test_unverified_named_past_work_earns_zero_and_blocks():
    sections = _clean_sections()
    sections["claim_grounding"] = {"claims": [{
        "section": "team_section",
        "sentence": "Cortech led the 2019 UNICEF Gode baseline.",
        "status": "NOT VERIFIED",
    }]}
    gate = qg.score_draft(sections, _analysis())
    assert gate["blocker_counts"] == {"unverified_claim": 1}
    assert next(r for r in gate["rows"] if r["section"] == "team_section")["earned"] == 0
    assert gate["status"] == qg.BLOCKED


def test_unsupported_claims_and_grounding_failure_block():
    sections = _clean_sections()
    sections["claim_grounding"] = {"claims": [{"section": "methodology", "sentence": "We are the best.", "status": "INSUFFICIENT EVIDENCE"}]}
    assert qg.score_draft(sections, _analysis())["blocker_counts"] == {"unsupported_claim": 1}
    sections["claim_grounding"] = {"claims": [], "error": "extractor down"}
    assert qg.score_draft(sections, _analysis())["status"] == qg.BLOCKED


# ── Silence-admiration hygiene ─────────────────────────────────────────────

def test_placeholders_block():
    for marker in ("[NOT VERIFIED]", "[UNKNOWN]", "[TBD]", "[insert client name]", "XXX", "Lorem ipsum"):
        sections = _clean_sections()
        sections["methodology"] += f"\n\nWorked example: {marker} here."
        gate = qg.score_draft(sections, _analysis())
        assert gate["blocker_counts"].get("placeholder"), marker


def test_leaked_template_numbering_blocks():
    sections = _clean_sections()
    sections["methodology"] = "Section 1: Introduction and Background\n\n5.1 Context\n\n" + sections["methodology"]
    gate = qg.score_draft(sections, _analysis())
    found = sorted(b["text"] for b in gate["blockers"] if b["kind"] == "template_numbering")
    assert found == ["5.1", "Section 1"]
    assert gate["status"] == qg.BLOCKED


def test_citing_the_clients_own_sections_is_not_a_leak():
    sections = _clean_sections()
    sections["methodology"] += (
        "\n\nSection 11.1 of the Request for Tender makes references mandatory.\n"
        "Section 6 of the ToR rules out building the system before the design is agreed."
    )
    assert qg.score_draft(sections, _analysis())["blockers"] == []


def test_the_clients_own_numbering_is_allowed():
    sections = _clean_sections()
    sections["submission_outline"] = {"prescribed": True, "sections": [{"heading": "3.2 Methodology"}]}
    sections["methodology"] = "3.2 Methodology\n\n" + sections["methodology"]
    assert qg.score_draft(sections, _analysis())["blockers"] == []


# ── Re-score after repair, bounded ─────────────────────────────────────────

def test_repair_then_rescore_reaches_ready():
    sections = _clean_sections()
    sections["methodology"] += "\n\nSample: [INSUFFICIENT EVIDENCE]."
    calls = []

    def repair(current, gate):
        calls.append(gate["score"])
        current["methodology"] = current["methodology"].replace("Sample: [INSUFFICIENT EVIDENCE].", "")
        return current

    out = qg.enforce(sections, _analysis(), repair=repair)
    assert len(calls) == 1
    assert out["quality_gate"]["status"] == qg.READY
    assert out["quality_gate"]["score_history"] == [40.0, 100.0]
    assert out["quality_gate"]["repair_rounds"] == 1


def test_repair_is_bounded_and_a_failing_draft_stays_blocked():
    sections = _clean_sections()
    sections["methodology"] += "\n\n[INSUFFICIENT EVIDENCE]"
    calls = []
    out = qg.enforce(sections, _analysis(), repair=lambda s, g: calls.append(1) or s)
    assert len(calls) == qg.MAX_REPAIR_ROUNDS
    assert out["quality_gate"]["status"] == qg.BLOCKED
    assert len(out["quality_gate"]["score_history"]) == qg.MAX_REPAIR_ROUNDS + 1


def test_no_repair_runs_on_a_ready_draft():
    calls = []
    out = qg.enforce(_clean_sections(), _analysis(), repair=lambda s, g: calls.append(1) or s)
    assert calls == []
    assert qg.is_ready(out)


# ── BLOCKED is visible in the email subject and the dashboard ──────────────

def _send(monkeypatch, gate):
    sent = {}
    monkeypatch.setattr(email_report, "build_proposal_docx", lambda *args: None)
    monkeypatch.setattr(
        email_report, "_send_email",
        lambda subject, content, attachment_path=None: sent.update(subject=subject, content=content) or True,
    )
    sections = _clean_sections()
    if gate is not None:
        sections["quality_gate"] = gate
    email_report.send_proposal_email({
        "title": "Baseline study", "client": "SOS", "deadline": "2099-12-01", "score": 100,
        "recommendation": "BID", "source_url": "https://example.org/t",
        "analysis": {"opportunity": {}, "bid_analysis": {}}, "matched_team": {"matched_team": {}},
        "budget": {}, "proposal_sections": sections,
    })
    return sent


def test_email_subject_says_blocked_below_the_bar(monkeypatch):
    sent = _send(monkeypatch, {"status": "BLOCKED", "score": 52, "headline": "Quality 52/100 (bar 70) — BLOCKED."})
    assert sent["subject"].startswith("BLOCKED quality 52/100")
    assert "READY" not in sent["subject"]
    assert "Quality 52/100 (bar 70)" in sent["content"]


def test_email_subject_says_ready_only_when_the_gate_passed(monkeypatch):
    sent = _send(monkeypatch, {"status": "READY", "score": 91, "headline": "Quality 91/100 (bar 70) — READY."})
    assert sent["subject"].startswith("READY FOR REVIEW quality 91/100")


def test_email_without_a_gate_result_is_blocked(monkeypatch):
    assert _send(monkeypatch, None)["subject"].startswith("BLOCKED")


def test_dashboard_scores_an_old_draft_and_never_calls_it_ready():
    from dashboard import queries

    old_draft = _clean_sections()
    old_draft["methodology"] += "\n\n[INSUFFICIENT EVIDENCE]"
    gate = queries._quality_gate(_analysis(), old_draft)
    assert gate["status"] == qg.BLOCKED
    assert gate["scored_after_drafting"] is True
    assert queries._quality_gate(_analysis(), {"quality_gate": {"status": "READY", "score": 90}})["status"] == "READY"
