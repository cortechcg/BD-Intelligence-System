from reporting import email_report


def _send_capture(monkeypatch):
    sent = {}
    monkeypatch.setattr(email_report, "build_proposal_docx", lambda *args: None)
    monkeypatch.setattr(
        email_report,
        "_send_email",
        lambda subject, content, attachment_path=None: sent.update(
            subject=subject, content=content, attachment_path=attachment_path
        ) or True,
    )
    return sent


def test_incomplete_budget_is_not_rendered_as_a_fake_zero_total_and_html_is_escaped(monkeypatch):
    sent = _send_capture(monkeypatch)

    email_report.send_proposal_email({
        "title": "Tender <img src=x onerror=alert(1)>\r\nBcc: attacker@example.test",
        "client": "Client <b>untrusted</b>",
        "deadline": "2099-12-01",
        "score": 82,
        "recommendation": "BID",
        "source_url": "https://procurement.example/tender?x=<tag>",
        "budget_cap": None,
        "analysis": {"opportunity": {}, "bid_analysis": {"key_strengths": ["<script>x</script>"], "key_gaps": []}},
        "matched_team": {"matched_team": {}},
        "budget": {
            "status": "INSUFFICIENT DATA",
            "reason": "Missing <rate card>",
            "missing_inputs": ["day rate <required>"],
            "summary": {"known_personnel_subtotal_usd": 0},
        },
        "proposal_sections": {
            "cover_letter": "Do not render <iframe>content</iframe>.",
            "win_strategy": "Lock vocabulary: <script>x</script>",
            "claim_grounding": {
                "verified": 2,
                "not_verified": 1,
                "insufficient_evidence": 0,
                "claims": [],
            },
        },
    })

    assert "Internal financial working" in sent["content"]
    assert "do not copy into the technical/EOI" in sent["content"]
    assert "Not yet calculated" in sent["content"]
    assert "TOTAL" not in sent["content"]
    assert "$0" not in sent["content"]
    assert "&lt;iframe&gt;content&lt;/iframe&gt;" in sent["content"]
    assert "Win strategy (internal" in sent["content"]
    assert "&lt;script&gt;x&lt;/script&gt;" in sent["content"]
    assert "<script>x</script>" not in sent["content"]
    assert "Claim grounding:" in sent["content"]
    assert "1 not verified" in sent["content"]
    assert "<script>x</script>" not in sent["content"]
    assert "\r" not in sent["subject"]
    assert "\n" not in sent["subject"]


def test_email_shows_personnel_budget_table_and_tender_ceiling(monkeypatch):
    sent = _send_capture(monkeypatch)

    email_report.send_proposal_email({
        "title": "Somalia MEL Endline",
        "client": "UNICEF",
        "deadline": "2099-12-01",
        "score": 82,
        "recommendation": "BID",
        "source_url": "https://procurement.example/tender",
        "budget_cap": 100000,
        "analysis": {"opportunity": {}, "bid_analysis": {"key_strengths": [], "key_gaps": []}},
        "matched_team": {"matched_team": {}},
        "budget": {
            "status": "PARTIAL",
            "primary_location": "Somalia",
            "reason": "Personnel costed from explicit ToR effort and rate-card inputs.",
            "missing_inputs": ["travel/logistics assumptions or quotations"],
            "opportunity_budget_cap_usd": 100000,
            "summary": {
                "personnel_subtotal_usd": 14800,
                "known_personnel_subtotal_usd": 14800,
                "grand_total_usd": None,
            },
            "personnel_breakdown": {
                "Team Lead": {
                    "role": "Team Lead",
                    "level": "Senior",
                    "estimated_days_of_effort": 12,
                    "day_rate_usd": 900,
                    "personnel_cost_usd": 10800,
                    "status": "VERIFIED",
                },
                "Analyst": {
                    "role": "Analyst",
                    "level": "Mid",
                    "estimated_days_of_effort": 8,
                    "day_rate_usd": 500,
                    "personnel_cost_usd": 4000,
                    "status": "VERIFIED",
                },
            },
        },
        "proposal_sections": {"cover_letter": "Technical draft only."},
    })

    html = sent["content"]
    assert "Internal financial working" in html
    assert "$100,000" in html
    assert "$14,800" in html
    assert "$10,800" in html
    assert "$4,000" in html
    assert "Team Lead" in html
    assert "Day rate" in html
    assert "Personnel cost" in html
    assert "TOTAL" not in html


def test_eoi_review_email_still_shows_internal_budget(monkeypatch):
    sent = _send_capture(monkeypatch)

    email_report.send_proposal_email({
        "title": "REOI for MEL",
        "client": "World Bank",
        "deadline": "2099-12-01",
        "score": 70,
        "recommendation": "BID",
        "source_url": "https://procurement.example/reoi",
        "budget_cap": 50000,
        "analysis": {"opportunity": {}, "bid_analysis": {"key_strengths": [], "key_gaps": []}},
        "matched_team": {"matched_team": {}},
        "budget": {
            "status": "PARTIAL",
            "primary_location": "Nairobi",
            "reason": "Internal planning figures only.",
            "missing_inputs": ["approved overhead"],
            "opportunity_budget_cap_usd": 50000,
            "summary": {"known_personnel_subtotal_usd": 9000},
            "personnel_breakdown": {
                "Team Lead": {
                    "role": "Team Lead",
                    "level": "Senior",
                    "estimated_days_of_effort": 10,
                    "day_rate_usd": 900,
                    "personnel_cost_usd": 9000,
                    "status": "VERIFIED",
                },
            },
        },
        "proposal_sections": {
            "submission_type": "EOI",
            "cover_letter": "Letter of interest with no fees.",
        },
    })

    html = sent["content"]
    assert "Internal financial working" in html
    assert "Expression of Interest" in html
    assert "$50,000" in html
    assert "$9,000" in html
    assert "must not appear in the EOI Word file" in html


def test_review_email_renders_draft_tables_as_html_not_pipe_rows(monkeypatch):
    sent = _send_capture(monkeypatch)

    email_report.send_proposal_email({
        "title": "Somalia MEL Endline",
        "client": "UNICEF",
        "deadline": "2099-12-01",
        "score": 82,
        "recommendation": "BID",
        "source_url": "https://procurement.example/tender",
        "budget_cap": 100000,
        "analysis": {"opportunity": {}, "bid_analysis": {"key_strengths": [], "key_gaps": []}},
        "matched_team": {"matched_team": {}},
        "budget": {"status": "PARTIAL", "summary": {}},
        "proposal_sections": {
            "cover_letter": "Opening paragraph.",
            "org_profile_and_track_record": (
                "| Project | Client | Year |\n"
                "|---|---|---|\n"
                "| WASH endline | Arche Nova | 2025 |\n"
            ),
        },
    })

    html = sent["content"]
    assert "<th" in html
    assert "Arche Nova" in html
    assert "| WASH endline |" not in html
    assert "Draft Technical Proposal" in html
    assert "Fit score" in html
    assert "82/100" in html
    assert "82.0/100" not in html
    assert "Review before submit" in html
    assert 'href="#c-decision"' in html
    assert 'href="#c-draft"' in html
    assert "display:none" in html
    assert "What still needs a human" in html


def test_review_email_flags_duplicate_consultant_mapping(monkeypatch):
    sent = _send_capture(monkeypatch)

    email_report.send_proposal_email({
        "title": "KPEEL EOI",
        "client": "Ministry of Education",
        "deadline": "2099-12-01",
        "score": 74,
        "recommendation": "BID",
        "source_url": "https://procurement.example/reoi",
        "analysis": {"opportunity": {}, "bid_analysis": {"key_strengths": [], "key_gaps": []}},
        "matched_team": {"matched_team": {
            "Research Assistant 1": {
                "consultant_name": "Salahweli Harun Abdi",
                "similarity_score": 81,
                "availability_flag": "Unknown",
            },
            "Research Assistant 2": {
                "consultant_name": "Salahweli Harun Abdi",
                "similarity_score": 64,
                "availability_flag": "Unknown",
            },
            "Lead Consultant": {
                "consultant_name": "TBD",
                "similarity_score": 0,
            },
        }},
        "budget": {"status": "PARTIAL", "summary": {}},
        "proposal_sections": {
            "submission_type": "EOI",
            "cover_letter": "Letter of interest.",
        },
    })

    html = sent["content"]
    assert "2/3" in html
    assert "mapped to both" in html
    assert "no named consultant" in html
    assert "Review this EOI before shortlisting" in html
    assert "Submit as an Expression of Interest only" in html
    assert "81% match" in html
    assert "81.0% match" not in html


def test_review_email_shows_tor_format_page_limits_and_gantt(monkeypatch):
    sent = _send_capture(monkeypatch)

    email_report.send_proposal_email({
        "title": "SPREAD Learning Paper 2",
        "client": "DanChurchAid Kenya",
        "deadline": "2026-09-24",
        "score": 80,
        "recommendation": "BID",
        "source_url": "https://procurement.example/spread",
        "analysis": {"opportunity": {}, "bid_analysis": {"key_strengths": [], "key_gaps": []}},
        "matched_team": {"matched_team": {}},
        "budget": {"status": "PARTIAL", "summary": {}},
        "proposal_sections": {
            "suitability_statement": " ".join(["word"] * 280),
            "work_plan": (
                "| Activity | Week 1 | Week 2 |\n"
                "| Inception | X | |\n"
                "| Fieldwork | | X |\n"
            ),
            "section_order": [
                ("suitability_statement", "Suitability statement"),
                ("work_plan", "Work-plan"),
            ],
            "submission_outline": {
                "prescribed": True,
                "sections": [
                    {
                        "key": "suitability_statement",
                        "heading": "Suitability statement",
                        "page_limit": "1 page",
                        "require_gantt": False,
                    },
                    {
                        "key": "work_plan",
                        "heading": "Work-plan",
                        "page_limit": "",
                        "require_gantt": True,
                    },
                ],
                "required_attachments": ["CVs of proposed experts"],
                "required_forms": ["Proposal Submission Form (Annex 2)"],
                "omitted_financial": ["Financial proposal"],
            },
            "required_attachments": ["CVs of proposed experts"],
            "omitted_financial": ["Financial proposal"],
        },
    })

    html = sent["content"]
    assert 'href="#c-format"' in html
    assert "ToR format" in html
    assert "ToR format vs this draft" in html
    assert "Gantt included" in html
    assert "CVs of proposed experts" in html
    assert "Financial proposal" in html
    assert "Attach (do not treat as chapters)" in html
