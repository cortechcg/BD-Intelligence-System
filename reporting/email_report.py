# reporting/email_report.py
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os
import smtplib
import ssl
import httpx
from loguru import logger
from database.airtable_client import get_table


def _get_recipients() -> list[str]:
    return [r.strip() for r in (os.getenv("EMAIL_RECIPIENTS") or "").split(",") if r.strip()]


def _send_via_gmail(subject: str, html_content: str) -> bool:
    """
    Send an HTML email through Gmail's SMTP server.

    Free and needs no third-party service, but only works from networks that
    allow outbound SMTP (e.g. your own computer). Railway blocks SMTP ports —
    there, _send_via_resend() is used instead.

    Requires env vars:
      GMAIL_ADDRESS       — your Gmail address (also used as the "from")
      GMAIL_APP_PASSWORD  — 16-char Google App Password (NOT your login password)
      EMAIL_RECIPIENTS    — comma-separated recipient list
    """
    gmail_user = os.getenv("GMAIL_ADDRESS")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD")
    recipients = _get_recipients()

    if not gmail_user or not gmail_pass or not recipients:
        return False

    # strip spaces Google shows in the app password (e.g. "abcd efgh ijkl mnop")
    gmail_pass = gmail_pass.replace(" ", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls(context=context)
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, recipients, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Gmail send failed: {e}")
        return False


def _send_via_resend(subject: str, html_content: str) -> bool:
    """
    Send an HTML email via the Resend HTTP API.

    Uses plain HTTPS, so it works even where SMTP is blocked (e.g. Railway).

    Requires env vars:
      RESEND_API_KEY    — Resend API key
      EMAIL_SENDER      — verified "from" address (e.g. bd@yourdomain.com)
      EMAIL_RECIPIENTS  — comma-separated recipient list
    """
    api_key = os.getenv("RESEND_API_KEY")
    sender = os.getenv("EMAIL_SENDER")
    recipients = _get_recipients()

    if not api_key or not sender or not recipients:
        return False

    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": recipients,
                "subject": subject,
                "html": html_content,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        detail = ""
        if isinstance(e, httpx.HTTPStatusError):
            detail = f" | response: {e.response.text}"
        logger.error(f"Resend send failed: {e}{detail}")
        return False


def _send_email(subject: str, html_content: str) -> bool:
    """
    Send an HTML email, preferring Gmail SMTP and falling back to Resend.

    Gmail is tried first (free, no verified domain). If Gmail isn't configured
    or fails (e.g. SMTP blocked on Railway), Resend is used instead.
    """
    if _send_via_gmail(subject, html_content):
        return True
    if _send_via_resend(subject, html_content):
        return True

    logger.error(
        "Email not sent: configure GMAIL_ADDRESS + GMAIL_APP_PASSWORD "
        "(and EMAIL_RECIPIENTS), or RESEND_API_KEY + EMAIL_SENDER"
    )
    return False


def get_pipeline_summary() -> dict:
    """Get current pipeline stats from Airtable."""
    table = get_table("opportunities")

    all_records = table.all()

    stats = {
        "total": len(all_records),
        "new": 0,
        "bidding": 0,
        "submitted": 0,
        "urgent": [],
        "high_priority": [],
        "watchlist": [],
    }

    today = datetime.now().date()

    for record in all_records:
        fields = record["fields"]
        status = fields.get("status", "")
        score = fields.get("relevance_score", 0)
        recommendation = fields.get("bid_recommendation", "")

        if status == "New":
            stats["new"] += 1
        elif status == "Bidding":
            stats["bidding"] += 1
        elif status == "Submitted":
            stats["submitted"] += 1

        # Check urgency
        deadline_str = fields.get("submission_deadline", "")
        if deadline_str:
            try:
                deadline = datetime.strptime(deadline_str[:10], "%Y-%m-%d").date()
                days_left = (deadline - today).days

                if days_left <= 3 and status not in ["Submitted", "Won", "Lost"]:
                    stats["urgent"].append({
                        "title": fields.get("title", "")[:50],
                        "client": fields.get("client", ""),
                        "deadline": deadline_str[:10],
                        "days_left": days_left,
                        "score": score,
                        "status": status,
                    })

                elif recommendation == "BID" and score >= 70:
                    stats["high_priority"].append({
                        "title": fields.get("title", "")[:50],
                        "client": fields.get("client", ""),
                        "deadline": deadline_str[:10],
                        "days_left": days_left,
                        "score": score,
                    })

                elif recommendation == "WATCH":
                    stats["watchlist"].append({
                        "title": fields.get("title", "")[:50],
                        "client": fields.get("client", ""),
                        "score": score,
                        "days_left": days_left,
                    })
            except:
                pass

    return stats


def build_html_report(stats: dict, new_opportunities: list[dict]) -> str:
    """Build the HTML email report."""

    urgent_html = ""
    for opp in stats.get("urgent", []):
        urgent_html += f"""
        <tr style="background:#fff3cd">
            <td style="padding:8px;border:1px solid #ddd;font-weight:bold">{opp['title']}</td>
            <td style="padding:8px;border:1px solid #ddd">{opp['client']}</td>
            <td style="padding:8px;border:1px solid #ddd;color:red;font-weight:bold">{opp['days_left']} days</td>
            <td style="padding:8px;border:1px solid #ddd">{opp['score']}/100</td>
            <td style="padding:8px;border:1px solid #ddd">{opp['status']}</td>
        </tr>"""

    new_opps_html = ""
    for opp in new_opportunities[:10]:
        score = opp.get("relevance_score", 0)
        color = "#d4edda" if score >= 70 else "#fff3cd" if score >= 50 else "#f8d7da"
        rec = opp.get("bid_recommendation", "N/A")
        new_opps_html += f"""
        <tr style="background:{color}">
            <td style="padding:8px;border:1px solid #ddd">{opp.get('title', '')[:50]}</td>
            <td style="padding:8px;border:1px solid #ddd">{opp.get('client', '')}</td>
            <td style="padding:8px;border:1px solid #ddd;font-weight:bold">{score}/100</td>
            <td style="padding:8px;border:1px solid #ddd;font-weight:bold">{rec}</td>
            <td style="padding:8px;border:1px solid #ddd">{opp.get('submission_deadline', 'TBD')[:10]}</td>
        </tr>"""

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Cortech BD Report</title></head>
    <body style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;color:#333">

    <!-- HEADER -->
    <div style="background:#1F3864;color:white;padding:20px;border-radius:8px 8px 0 0">
        <h1 style="margin:0;font-size:22px">🤖 Cortech BD Intelligence Report</h1>
        <p style="margin:5px 0 0;font-size:14px;opacity:0.8">
            {datetime.now().strftime('%A, %d %B %Y at %H:%M EAT')}
        </p>
    </div>

    <!-- PIPELINE STATS -->
    <div style="background:#f8f9fa;padding:20px;border-left:4px solid #1F3864">
        <h2 style="color:#1F3864;margin-top:0">📊 Pipeline Overview</h2>
        <table style="width:100%;border-collapse:collapse">
            <tr>
                <td style="text-align:center;padding:15px">
                    <div style="font-size:32px;font-weight:bold;color:#1F3864">{stats['new']}</div>
                    <div style="font-size:12px;color:#666">New Opportunities</div>
                </td>
                <td style="text-align:center;padding:15px">
                    <div style="font-size:32px;font-weight:bold;color:#f0a500">{stats['bidding']}</div>
                    <div style="font-size:12px;color:#666">In Progress</div>
                </td>
                <td style="text-align:center;padding:15px">
                    <div style="font-size:32px;font-weight:bold;color:#28a745">{stats['submitted']}</div>
                    <div style="font-size:12px;color:#666">Submitted</div>
                </td>
                <td style="text-align:center;padding:15px">
                    <div style="font-size:32px;font-weight:bold;color:#dc3545">{len(stats['urgent'])}</div>
                    <div style="font-size:12px;color:#666">Urgent (≤3 days)</div>
                </td>
            </tr>
        </table>
    </div>

    {"<!-- URGENT -->" if stats.get('urgent') else ""}
    {f"""
    <div style="background:#fff3cd;padding:20px;border-left:4px solid #dc3545">
        <h2 style="color:#dc3545;margin-top:0">🚨 URGENT — Deadlines in 72 Hours</h2>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#dc3545;color:white">
                <th style="padding:8px;text-align:left">Title</th>
                <th style="padding:8px;text-align:left">Client</th>
                <th style="padding:8px;text-align:left">Days Left</th>
                <th style="padding:8px;text-align:left">Score</th>
                <th style="padding:8px;text-align:left">Status</th>
            </tr>
            {urgent_html}
        </table>
    </div>
    """ if stats.get('urgent') else ""}

    <!-- NEW OPPORTUNITIES -->
    <div style="padding:20px">
        <h2 style="color:#1F3864">🆕 New Opportunities Discovered</h2>
        {f"""
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#1F3864;color:white">
                <th style="padding:8px;text-align:left">Title</th>
                <th style="padding:8px;text-align:left">Client</th>
                <th style="padding:8px;text-align:left">Score</th>
                <th style="padding:8px;text-align:left">Recommendation</th>
                <th style="padding:8px;text-align:left">Deadline</th>
            </tr>
            {new_opps_html}
        </table>
        <p style="font-size:11px;color:#999;margin-top:10px">
            🟢 Score ≥70 = Strong Match | 🟡 Score 50-69 = Moderate | 🔴 Score <50 = Weak Match
        </p>
        """ if new_opportunities else "<p>No new opportunities discovered in this cycle.</p>"}
    </div>

    <!-- FOOTER -->
    <div style="background:#f8f9fa;padding:15px;border-radius:0 0 8px 8px;text-align:center">
        <p style="font-size:11px;color:#999;margin:0">
            Cortech BD Intelligence Agent • All opportunities require human review before submission<br>
            <a href="https://airtable.com" style="color:#1F3864">View Full Dashboard in Airtable</a>
        </p>
    </div>

    </body>
    </html>
    """

    return html

def send_proposal_email(opportunity_result: dict) -> None:
    """
    Send one email per processed opportunity containing:
    - TOR link and key analysis
    - Matched team
    - Budget summary
    - Full proposal draft sections
    - Clear call to action for human review before submission
    """
    title       = opportunity_result.get("title", "Unknown")
    client      = opportunity_result.get("client", "Unknown Client")
    deadline    = opportunity_result.get("deadline", "TBD")
    score       = opportunity_result.get("score", 0)
    source_url  = opportunity_result.get("source_url", "")
    budget_cap  = opportunity_result.get("budget_cap", 0)
    proposal    = opportunity_result.get("proposal_sections", {})
    budget      = opportunity_result.get("budget", {})
    analysis    = opportunity_result.get("analysis", {})
    matched     = opportunity_result.get("matched_team", {})
    compliance  = opportunity_result.get("compliance_matrix", "")
    recommendation = opportunity_result.get("recommendation", "WATCH")

    bid_analysis   = analysis.get("bid_analysis", {})
    key_strengths  = bid_analysis.get("key_strengths", [])
    key_gaps       = bid_analysis.get("key_gaps", [])
    budget_summary = budget.get("summary", {})
    team_matches   = matched.get("matched_team", {})
    budget_total   = budget_summary.get("grand_total_usd", 0)
    budget_cap_str = f"${budget_cap:,.0f}" if budget_cap else "Not specified"

    # ── TEAM TABLE ─────────────────────────────────────────────────────────
    team_rows_html = ""
    for role, match in team_matches.items():
        name  = match.get("consultant_name", "TBD")
        score_pct = match.get("similarity_score", 0)
        color = "#28a745" if score_pct >= 80 else "#f0a500" if score_pct >= 60 else "#dc3545"
        team_rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd">{role}</td>
            <td style="padding:8px;border:1px solid #ddd;font-weight:bold">{name}</td>
            <td style="padding:8px;border:1px solid #ddd;color:{color};font-weight:bold">{score_pct}% match</td>
        </tr>"""

    # ── STRENGTHS / GAPS ───────────────────────────────────────────────────
    strengths_html = "".join(
        f"<li style='margin-bottom:4px'>✅ {s}</li>"
        for s in key_strengths[:5]
    )
    gaps_html = "".join(
        f"<li style='margin-bottom:4px'>⚠️ {g}</li>"
        for g in key_gaps[:5]
    )

    # ── BUDGET TABLE ───────────────────────────────────────────────────────
    budget_rows_html = ""
    for line, details in [
        ("Personnel", f"${budget_summary.get('personnel_subtotal_usd', 0):,.0f}"),
        ("Field & Logistics", f"${budget_summary.get('field_logistics_usd', 0):,.0f}"),
        ("Data & Tools", f"${budget_summary.get('data_tools_usd', 0):,.0f}"),
        ("Reporting & Design", f"${budget_summary.get('reporting_design_usd', 0):,.0f}"),
        ("Management Overhead", f"${budget_summary.get('management_overhead_usd', 0):,.0f}"),
        ("Contingency (5%)", f"${budget_summary.get('contingency_5pct_usd', 0):,.0f}"),
        ("Workshop Costs", f"${budget_summary.get('workshop_costs_usd', 0):,.0f}"),
    ]:
        budget_rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd">{line}</td>
            <td style="padding:8px;border:1px solid #ddd;text-align:right">{details}</td>
        </tr>"""

    # ── PROPOSAL SECTIONS ──────────────────────────────────────────────────
    def section_block(heading: str, content: str) -> str:
        if not content:
            return ""
        # Convert newlines to paragraphs for HTML
        paragraphs = "".join(
            f"<p style='margin:0 0 10px;line-height:1.6'>{p.strip()}</p>"
            for p in content.split("\n") if p.strip()
        )
        return f"""
        <div style="margin-bottom:24px">
            <h3 style="color:#1F3864;font-size:15px;margin:0 0 10px;
                       border-bottom:2px solid #1F3864;padding-bottom:6px">
                {heading}
            </h3>
            {paragraphs}
        </div>"""

    proposal_html = (
        section_block("Cover Letter", proposal.get("cover_letter", ""))
        + section_block("Executive Summary", proposal.get("executive_summary", ""))
        + section_block("Organisational Profile & Track Record", proposal.get("org_profile_and_track_record", ""))
        + section_block("Introduction, Background & Conceptual Framework", proposal.get("introduction_and_framework", ""))
        + section_block("Methodology", proposal.get("methodology", ""))
        + section_block("Sampling & Data Analysis Plan", proposal.get("analysis_plan", ""))
        + section_block("Quality Assurance & Ethical Safeguarding", proposal.get("qa_and_ethics", ""))
        + section_block("Risk Register", proposal.get("risk_register", ""))
        + section_block("Team Composition", proposal.get("team_section", ""))
        + section_block("Work Plan", proposal.get("work_plan", ""))
    )

    # ── SCORE COLOR ────────────────────────────────────────────────────────
    score_color = (
        "#28a745" if score >= 70
        else "#f0a500" if score >= 50
        else "#dc3545"
    )

    # ── FULL HTML EMAIL ────────────────────────────────────────────────────
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:Arial,sans-serif;max-width:900px;
                 margin:0 auto;color:#333;font-size:14px">

    <!-- HEADER -->
    <div style="background:#1F3864;color:white;padding:24px 20px;
                border-radius:8px 8px 0 0">
        <h1 style="margin:0;font-size:22px">📋 PROPOSAL DRAFT READY FOR REVIEW</h1>
        <p style="margin:8px 0 0;opacity:0.85;font-size:14px">
            Human review required before submission — do not submit without approval
        </p>
    </div>

    <!-- OPPORTUNITY SUMMARY -->
    <div style="background:#f8f9fa;padding:20px;
                border-left:4px solid #1F3864;margin-bottom:0">
        <h2 style="margin:0 0 12px;color:#1F3864;font-size:18px">{title}</h2>
        <table style="width:100%;border-collapse:collapse">
            <tr>
                <td style="padding:4px 12px 4px 0;width:50%">
                    <strong>Client:</strong> {client}
                </td>
                <td style="padding:4px 0">
                    <strong>Deadline:</strong>
                    <span style="color:#dc3545;font-weight:bold">{deadline}</span>
                </td>
            </tr>
            <tr>
                <td style="padding:4px 12px 4px 0">
                    <strong>Fit Score:</strong>
                    <span style="color:{score_color};font-weight:bold;font-size:16px">
                        {score}/100
                    </span>
                    ({recommendation})
                </td>
                <td style="padding:4px 0">
                    <strong>Budget Cap:</strong> {budget_cap_str}
                </td>
            </tr>
            <tr>
                <td colspan="2" style="padding:8px 0 4px">
                    <strong>📎 TOR / Source:</strong>
                    <a href="{source_url}" style="color:#1F3864">{source_url}</a>
                </td>
            </tr>
        </table>
    </div>

    <!-- ACTION BANNER -->
    <div style="background:#fff3cd;padding:14px 20px;
                border-left:4px solid #f0a500">
        <strong>⚠️ ACTION REQUIRED:</strong> Review the draft below, make edits,
        confirm team availability, verify the budget, then approve for submission.
        <strong>Nothing has been sent to the client.</strong>
    </div>

    <div style="padding:20px">

    <!-- STRENGTHS AND GAPS -->
    <div style="display:flex;gap:20px;margin-bottom:24px">
        <div style="flex:1;background:#d4edda;padding:16px;border-radius:6px">
            <h3 style="color:#155724;margin:0 0 10px;font-size:14px">
                ✅ Key Strengths
            </h3>
            <ul style="margin:0;padding-left:18px">{strengths_html}</ul>
        </div>
        <div style="flex:1;background:#fff3cd;padding:16px;border-radius:6px">
            <h3 style="color:#856404;margin:0 0 10px;font-size:14px">
                ⚠️ Gaps to Address
            </h3>
            <ul style="margin:0;padding-left:18px">{gaps_html}</ul>
        </div>
    </div>

    <!-- TEAM TABLE -->
    <div style="margin-bottom:24px">
        <h3 style="color:#1F3864;font-size:15px;margin:0 0 10px;
                   border-bottom:2px solid #1F3864;padding-bottom:6px">
            👥 Matched Team
        </h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#1F3864;color:white">
                <th style="padding:8px;text-align:left">Required Role</th>
                <th style="padding:8px;text-align:left">Matched Consultant</th>
                <th style="padding:8px;text-align:left">CV Match</th>
            </tr>
            {team_rows_html}
        </table>
    </div>

    <!-- BUDGET TABLE -->
    <div style="margin-bottom:24px">
        <h3 style="color:#1F3864;font-size:15px;margin:0 0 10px;
                   border-bottom:2px solid #1F3864;padding-bottom:6px">
            💰 Budget Draft
        </h3>
        <table style="width:50%;border-collapse:collapse;font-size:13px">
            <tr style="background:#1F3864;color:white">
                <th style="padding:8px;text-align:left">Line Item</th>
                <th style="padding:8px;text-align:right">Amount</th>
            </tr>
            {budget_rows_html}
            <tr style="background:#1F3864;color:white;font-weight:bold">
                <td style="padding:8px">TOTAL</td>
                <td style="padding:8px;text-align:right">
                    ${budget_total:,.0f}
                </td>
            </tr>
        </table>
        <p style="font-size:12px;color:#666;margin:8px 0 0">
            ⚠️ Verify against the budget cap before submission.
            Adjust line items as needed.
        </p>
    </div>

    <!-- COMPLIANCE MATRIX -->
    <div style="margin-bottom:24px">
        <h3 style="color:#1F3864;font-size:15px;margin:0 0 10px;
                   border-bottom:2px solid #1F3864;padding-bottom:6px">
            📊 Compliance Matrix
        </h3>
        <pre style="background:#f8f9fa;padding:14px;border-radius:4px;
                    font-size:12px;overflow-x:auto;white-space:pre-wrap">{compliance}</pre>
    </div>

    <!-- PROPOSAL DRAFT -->
    <div style="border-top:3px solid #1F3864;padding-top:20px;margin-top:8px">
        <h2 style="color:#1F3864;margin:0 0 20px">📄 Draft Technical Proposal</h2>
        {proposal_html}
    </div>

    </div>

    <!-- FOOTER -->
    <div style="background:#f8f9fa;padding:16px 20px;
                border-radius:0 0 8px 8px;text-align:center;
                border-top:1px solid #dee2e6">
        <p style="font-size:12px;color:#999;margin:0">
            Generated by Cortech BD Intelligence Agent •
            This is a draft — human review and approval required before any submission •
            <a href="{source_url}" style="color:#1F3864">View original TOR</a>
        </p>
    </div>

    </body>
    </html>
    """

    # ── SUBJECT LINE ───────────────────────────────────────────────────────
    subject = (
        f"📋 DRAFT READY: {title[:50]} | "
        f"Deadline: {str(deadline)[:10]} | "
        f"Score: {score}/100 | REVIEW REQUIRED"
    )

    if _send_email(subject, html):
        logger.success(f"Proposal email sent: {title[:50]}")
    else:
        logger.error(f"Proposal email failed for {title[:50]}")


def send_report(new_opportunities: list[dict] = None) -> None:
    """Send the BD intelligence report via email."""
    stats = get_pipeline_summary()
    html_content = build_html_report(stats, new_opportunities or [])

    # Determine subject urgency
    urgent_count = len(stats.get("urgent", []))
    new_count = len(new_opportunities or [])

    if urgent_count > 0:
        subject = f"🚨 BD ALERT: {urgent_count} Urgent Deadline(s) | {new_count} New Opportunities"
    else:
        subject = f"📊 Cortech BD Report | {new_count} New Opportunities"

    # Send email via Resend HTTP API (Railway blocks SMTP)
    if _send_email(subject, html_content):
        logger.success("BD report email sent!")