from reporting import email_report


def test_incomplete_budget_is_not_rendered_as_a_fake_zero_total_and_html_is_escaped(monkeypatch):
    sent = {}
    monkeypatch.setattr(email_report, "build_proposal_docx", lambda *args: None)
    monkeypatch.setattr(
        email_report,
        "_send_email",
        lambda subject, content, attachment_path=None: sent.update(
            subject=subject, content=content, attachment_path=attachment_path
        ) or True,
    )

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
        "proposal_sections": {"cover_letter": "Do not render <iframe>content</iframe>."},
    })

    assert "Budget validation required" in sent["content"]
    assert "TOTAL" not in sent["content"]
    assert "$0" not in sent["content"]
    assert "&lt;iframe&gt;content&lt;/iframe&gt;" in sent["content"]
    assert "<script>x</script>" not in sent["content"]
    assert "\r" not in sent["subject"]
    assert "\n" not in sent["subject"]
