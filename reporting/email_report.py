# reporting/email_report.py
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import base64
import html
import math
import os
import re
import smtplib
import ssl
import httpx
from loguru import logger
from database.airtable_client import get_table
from reporting.docx_builder import build_proposal_docx
from utils.urls import UnsafeURLError, assert_public_http_url


def _html(value) -> str:
    """Render operational data as text, never as HTML supplied by a source."""
    return html.escape(str(value or ""), quote=True)


def _usd(value) -> str:
    """Format a known finite number; invalid/missing values remain UNKNOWN."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return f"${number:,.0f}" if math.isfinite(number) else "UNKNOWN"


def _subject_text(value) -> str:
    """Prevent untrusted titles from injecting a second mail header."""
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _safe_color(value, default: str = "#666666") -> str:
    """Allow only literal CSS colours controlled by this application."""
    candidate = str(value or "")
    return candidate if re.fullmatch(r"#[0-9A-Fa-f]{6}", candidate) else default


def _safe_href(value) -> str:
    """Return a safe public HTTP(S) href or an inert empty target."""
    try:
        return _html(assert_public_http_url(str(value or "")))
    except UnsafeURLError:
        return ""


def _number(value, default: float = 0) -> float:
    """Keep external/Airtable values out of numeric HTML/style decisions."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _get_recipients() -> list[str]:
    return [r.strip() for r in (os.getenv("EMAIL_RECIPIENTS") or "").split(",") if r.strip()]


def _send_via_gmail(subject: str, html_content: str, attachment_path: str = None) -> bool:
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

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html"))

    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            part = MIMEApplication(
                f.read(),
                _subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=os.path.basename(attachment_path),
        )
        msg.attach(part)

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


def _send_via_resend(subject: str, html_content: str, attachment_path: str = None) -> bool:
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
        payload = {
            "from": sender,
            "to": recipients,
            "subject": subject,
            "html": html_content,
        }

        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode()
            payload["attachments"] = [{
                "filename": os.path.basename(attachment_path),
                "content": encoded,
            }]

        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
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


def _send_email(subject: str, html_content: str, attachment_path: str = None) -> bool:
    """
    Send an HTML email, preferring Gmail SMTP and falling back to Resend.

    Gmail is tried first (free, no verified domain). If Gmail isn't configured
    or fails (e.g. SMTP blocked on Railway), Resend is used instead.
    """
    if _send_via_gmail(subject, html_content, attachment_path):
        return True
    if _send_via_resend(subject, html_content, attachment_path):
        return True

    logger.error(
        "Email not sent: configure GMAIL_ADDRESS + GMAIL_APP_PASSWORD "
        "(and EMAIL_RECIPIENTS), or RESEND_API_KEY + EMAIL_SENDER"
    )
    return False


def get_urgency_level(deadline_str: str) -> dict:
    """Calculate urgency based on days to deadline."""
    try:
        deadline = datetime.strptime(deadline_str[:10], "%Y-%m-%d").date()
        days_left = (deadline - datetime.now().date()).days
    except Exception:
        return {"level": "unknown", "days": None, "color": "#666666", "prefix": ""}

    if days_left <= 0:
        return {"level": "EXPIRED", "days": days_left, "color": "#dc3545", "prefix": "DEADLINE PASSED"}
    elif days_left <= 1:
        return {"level": "CRITICAL", "days": days_left, "color": "#dc3545", "prefix": "24 HOURS REMAINING"}
    elif days_left <= 3:
        return {"level": "URGENT", "days": days_left, "color": "#e85d04", "prefix": f"{days_left} DAYS LEFT"}
    elif days_left <= 7:
        return {"level": "HIGH", "days": days_left, "color": "#f0a500", "prefix": f"{days_left} DAYS LEFT"}
    elif days_left <= 14:
        return {"level": "NORMAL", "days": days_left, "color": "#2e86c1", "prefix": f"{days_left} days left"}
    else:
        return {"level": "LOW", "days": days_left, "color": "#28a745", "prefix": f"{days_left} days left"}


def send_deadline_alert_email(urgent: list[dict]) -> None:
    """Single digest email for opportunities with approaching deadlines."""
    sorted_opps = sorted(
        urgent,
        key=lambda o: o.get("urgency", {}).get("days") if o.get("urgency", {}).get("days") is not None else 9999,
    )

    rows_html = ""
    for opp in sorted_opps:
        urgency = opp.get("urgency", {})
        color = _safe_color(urgency.get("color"), "#666666")
        rows_html += f"""
        <tr style="border-left:4px solid {color}">
            <td style="padding:10px;border:1px solid #ddd;font-weight:bold">{_html(opp.get('title', ''))}</td>
            <td style="padding:10px;border:1px solid #ddd">{_html(opp.get('client', ''))}</td>
            <td style="padding:10px;border:1px solid #ddd">{_html(str(opp.get('deadline', ''))[:10])}</td>
            <td style="padding:10px;border:1px solid #ddd;color:{color};font-weight:bold">
                {_html(urgency.get('prefix', urgency.get('level', '')))}
            </td>
        </tr>"""

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;color:#333">
    <div style="background:#1F3864;color:white;padding:20px;border-radius:8px 8px 0 0">
        <h1 style="margin:0;font-size:22px">Deadline Escalation Alert</h1>
        <p style="margin:8px 0 0;opacity:0.85">{datetime.now().strftime('%A, %d %B %Y at %H:%M')}</p>
    </div>
    <div style="padding:20px">
        <p><strong>{len(sorted_opps)}</strong> active opportunit
        {"y" if len(sorted_opps) == 1 else "ies"} need attention within 7 days.</p>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#1F3864;color:white">
                <th style="padding:8px;text-align:left">Title</th>
                <th style="padding:8px;text-align:left">Client</th>
                <th style="padding:8px;text-align:left">Deadline</th>
                <th style="padding:8px;text-align:left">Urgency</th>
            </tr>
            {rows_html}
        </table>
        <p style="font-size:12px;color:#666;margin-top:16px">
            Review these in Airtable and confirm submission status.
        </p>
    </div>
    </body>
    </html>
    """

    subject = f"DEADLINE ALERT: {len(sorted_opps)} opportunit{'y' if len(sorted_opps) == 1 else 'ies'} need action"
    if _send_email(subject, html):
        logger.success(f"Deadline alert email sent ({len(sorted_opps)} items)")
    else:
        logger.error("Deadline alert email failed to send")


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
                        "title": (fields.get("title") or "")[:50],
                        "client": fields.get("client", ""),
                        "deadline": deadline_str[:10],
                        "days_left": days_left,
                        "score": score,
                        "status": status,
                    })

                elif recommendation == "BID" and score >= 70:
                    stats["high_priority"].append({
                        "title": (fields.get("title") or "")[:50],
                        "client": fields.get("client", ""),
                        "deadline": deadline_str[:10],
                        "days_left": days_left,
                        "score": score,
                    })

                elif recommendation == "WATCH":
                    stats["watchlist"].append({
                        "title": (fields.get("title") or "")[:50],
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
            <td style="padding:8px;border:1px solid #ddd;font-weight:bold">{_html(opp.get('title', ''))}</td>
            <td style="padding:8px;border:1px solid #ddd">{_html(opp.get('client', ''))}</td>
            <td style="padding:8px;border:1px solid #ddd;color:red;font-weight:bold">{_html(opp.get('days_left', ''))} days</td>
            <td style="padding:8px;border:1px solid #ddd">{_html(opp.get('score', ''))}/100</td>
            <td style="padding:8px;border:1px solid #ddd">{_html(opp.get('status', ''))}</td>
        </tr>"""

    new_opps_html = ""
    for opp in new_opportunities[:10]:
        score = _number(opp.get("relevance_score", 0))
        color = "#d4edda" if score >= 70 else "#fff3cd" if score >= 50 else "#f8d7da"
        rec = opp.get("bid_recommendation", "N/A")
        new_opps_html += f"""
        <tr style="background:{color}">
            <td style="padding:8px;border:1px solid #ddd">{_html(str(opp.get('title', ''))[:50])}</td>
            <td style="padding:8px;border:1px solid #ddd">{_html(opp.get('client', ''))}</td>
            <td style="padding:8px;border:1px solid #ddd;font-weight:bold">{_html(score)}/100</td>
            <td style="padding:8px;border:1px solid #ddd;font-weight:bold">{_html(rec)}</td>
            <td style="padding:8px;border:1px solid #ddd">{_html(str(opp.get('submission_deadline', 'TBD'))[:10])}</td>
        </tr>"""

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Cortech BD Report</title></head>
    <body style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;color:#333">

    <!-- HEADER -->
    <div style="background:#1F3864;color:white;padding:20px;border-radius:8px 8px 0 0">
        <h1 style="margin:0;font-size:22px">Cortech BD Intelligence Report</h1>
        <p style="margin:5px 0 0;font-size:14px;opacity:0.8">
            {datetime.now().strftime('%A, %d %B %Y at %H:%M EAT')}
        </p>
    </div>

    <!-- PIPELINE STATS -->
    <div style="background:#f8f9fa;padding:20px;border-left:4px solid #1F3864">
        <h2 style="color:#1F3864;margin-top:0">Pipeline Overview</h2>
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
        <h2 style="color:#dc3545;margin-top:0">URGENT — Deadlines in 72 Hours</h2>
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
        <h2 style="color:#1F3864">New Opportunities Discovered</h2>
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
            Score ≥70 = Strong Match | Score 50-69 = Moderate | Score <50 = Weak Match
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
    title       = opportunity_result.get("title") or "Unknown"
    if not isinstance(title, str):
        title = "Unknown"
    client      = opportunity_result.get("client") or "Unknown Client"
    if not isinstance(client, str):
        client = "Unknown Client"
    deadline    = opportunity_result.get("deadline", "TBD")
    score       = _number(opportunity_result.get("score", 0))
    source_url  = opportunity_result.get("source_url", "")
    budget_cap  = opportunity_result.get("budget_cap", 0)
    proposal    = opportunity_result.get("proposal_sections", {})
    budget      = opportunity_result.get("budget", {})
    analysis    = opportunity_result.get("analysis", {})
    matched     = opportunity_result.get("matched_team", {})
    recommendation = opportunity_result.get("recommendation", "WATCH")
    is_lightweight = proposal.get("lightweight", False)
    submission_type = proposal.get("submission_type", "FULL_PROPOSAL")
    is_eoi = submission_type == "EOI"
    doc_label = "EXPRESSION OF INTEREST" if is_eoi else "DRAFT PROPOSAL"

    opportunity = dict(analysis.get("opportunity", {}))
    if title:
        opportunity["title"] = title
    if client:
        opportunity["client"] = client

    docx_path = None
    try:
        docx_path = build_proposal_docx(proposal, opportunity)
    except Exception as e:
        logger.error(
            f"Could not build proposal docx — email will still send without it: {e}"
        )

    bid_analysis   = analysis.get("bid_analysis", {})
    key_strengths  = bid_analysis.get("key_strengths", [])
    key_gaps       = bid_analysis.get("key_gaps", [])
    budget_summary = budget.get("summary", {}) if isinstance(budget, dict) else {}
    team_matches   = matched.get("matched_team", {})
    budget_status  = budget.get("status", "UNKNOWN") if isinstance(budget, dict) else "UNKNOWN"
    budget_reason  = budget.get("reason", "No budget basis was supplied.") if isinstance(budget, dict) else "No budget basis was supplied."
    budget_missing = budget.get("missing_inputs", []) if isinstance(budget, dict) else []
    budget_cap_str = _usd(budget_cap) if budget_cap else "Not specified"
    urgency        = get_urgency_level(str(deadline))
    quality        = proposal.get("quality_score", {}) if isinstance(proposal.get("quality_score"), dict) else {}
    quality_line   = ""
    if quality.get("overall_score") is not None:
        quality_line = (
            f"<p style='margin:8px 0 0;font-size:13px;color:#555'>"
            f"Self-assessed: <strong>{_html(quality['overall_score'])}/100</strong> — "
            f"weakest: {_html(quality.get('weakest_criterion', 'N/A'))}. "
            f"{_html(quality.get('one_improvement', ''))}"
            f"</p>"
        )

    # ── TEAM TABLE ─────────────────────────────────────────────────────────
    team_rows_html = ""
    for role, match in team_matches.items():
        if not isinstance(match, dict):
            continue
        name  = match.get("consultant_name", "TBD")
        score_pct = _number(match.get("similarity_score", 0))
        avail = match.get("availability_flag", "Unknown")
        color = "#28a745" if score_pct >= 80 else "#f0a500" if score_pct >= 60 else "#dc3545"
        team_rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd">{_html(role)}</td>
            <td style="padding:8px;border:1px solid #ddd;font-weight:bold">{_html(name)}</td>
            <td style="padding:8px;border:1px solid #ddd;color:{color};font-weight:bold">{score_pct}% match</td>
            <td style="padding:8px;border:1px solid #ddd">{_html(avail)}</td>
        </tr>"""

    # ── STRENGTHS / GAPS ───────────────────────────────────────────────────
    strengths_html = "".join(
        f"<li style='margin-bottom:4px'>{_html(s)}</li>"
        for s in key_strengths[:5]
    )
    gaps_html = "".join(
        f"<li style='margin-bottom:4px'>{_html(g)}</li>"
        for g in key_gaps[:5]
    )

    # ── PROPOSAL SECTIONS ──────────────────────────────────────────────────
    def section_block(heading: str, content: str) -> str:
        if not content:
            return ""
        # Convert newlines to paragraphs for HTML. Skip markdown rules and
        # table separators so "---" never appears in the emailed draft.
        paragraphs = "".join(
            f"<p style='margin:0 0 10px;line-height:1.6'>{_html(p.strip())}</p>"
            for p in content.split("\n")
            if p.strip()
            and not set(p.strip()) <= set("-*_= ")
            and not (
                p.strip().startswith("|")
                and set(p.strip().replace(" ", "")) <= set("|:-")
            )
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
    )
    if is_eoi:
        proposal_html = (
            section_block("Cover Letter / Letter of Interest", proposal.get("cover_letter", ""))
            + section_block("Presentation of Cortech Consulting Group", proposal.get("firm_profile", ""))
            + section_block("Our Understanding of the Assignment", proposal.get("understanding", ""))
            + section_block("Proposed Technical Approach — Summary", proposal.get("approach_summary", ""))
            + section_block("Relevant Experience", proposal.get("relevant_experience", ""))
            + section_block("Resources in Staff", proposal.get("key_experts", ""))
            + section_block("Eligibility", proposal.get("eligibility", ""))
            + section_block("Capability Matrix", proposal.get("compliance_matrix", ""))
        )
    elif not is_lightweight:
        proposal_html += (
            section_block("Organisational Profile & Track Record", proposal.get("org_profile_and_track_record", ""))
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
    header_title = (
        "EXPRESSION OF INTEREST READY FOR REVIEW"
        if is_eoi
        else "QUICK FLAG — WATCH OPPORTUNITY"
        if is_lightweight
        else "PROPOSAL DRAFT READY FOR REVIEW"
    )
    header_subtitle = (
        "EOI-stage submission — a complete shortlisting draft. Review and submit "
        "as an Expression of Interest, not a full technical proposal unless shortlisted."
        if is_eoi
        else proposal.get(
            "lightweight_reason",
            "WATCH recommendation — quick flag, not a full draft",
        )
        if is_lightweight
        else "Human review required before submission — do not submit without approval"
    )
    action_banner = (
        """<div style="background:#e8f4fd;padding:14px 20px;border-left:4px solid #2e86c1">
        <strong>EOI STAGE:</strong> This document asks for an Expression of Interest only.
        The draft below is a complete shortlisting file (understanding, approach
        summary, experience, team, eligibility, criteria matrix) — not a full
        technical/financial proposal. Review, confirm team availability, then submit
        as an EOI unless you have been invited to the next stage.
        </div>"""
        if is_eoi
        else f"""<div style="background:#fff3cd;padding:14px 20px;border-left:4px solid #f0a500">
        <strong>WATCH — QUICK FLAG ONLY:</strong> This is a lightweight preview
        (cover letter + executive summary). No full proposal was generated.
        Review the opportunity and decide whether to pursue a full bid.
        </div>"""
        if is_lightweight
        else """<div style="background:#fff3cd;padding:14px 20px;border-left:4px solid #f0a500">
        <strong>ACTION REQUIRED:</strong> Review the draft below, make edits,
        confirm team availability, verify the budget, then approve for submission.
        <strong>Nothing has been sent to the client.</strong>
        </div>"""
    )
    draft_heading = (
        "Expression of Interest Draft"
        if is_eoi
        else "Quick-Flag Preview (Cover Letter + Executive Summary)"
        if is_lightweight
        else "Draft Technical Proposal"
    )
    budget_missing_html = "".join(
        f"<li>{_html(item)}</li>" for item in budget_missing[:12]
    ) or "<li>No missing-input detail was recorded.</li>"
    known_personnel = budget_summary.get("known_personnel_subtotal_usd")
    known_personnel_line = (
        f"<p>Verified personnel subtotal only: <strong>{_usd(known_personnel)}</strong>.</p>"
        if known_personnel not in (None, 0, 0.0)
        else ""
    )
    budget_block = "" if is_eoi else f"""
    <!-- BUDGET STATUS -->
    <div style="margin-bottom:24px;background:#fff3cd;padding:14px 16px;border-left:4px solid #f0a500">
        <h3 style="color:#1F3864;font-size:15px;margin:0 0 10px">Budget validation required</h3>
        <p><strong>Status:</strong> {_html(budget_status)}. {_html(budget_reason)}</p>
        {known_personnel_line}
        <p style="margin-bottom:4px"><strong>Inputs still required before a financial submission:</strong></p>
        <ul style="margin-top:4px">{budget_missing_html}</ul>
    </div>
"""

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:Arial,sans-serif;max-width:900px;
                 margin:0 auto;color:#333;font-size:14px">

    <!-- HEADER -->
    <div style="background:#1F3864;color:white;padding:24px 20px;
                border-radius:8px 8px 0 0">
        <h1 style="margin:0;font-size:22px">{header_title}</h1>
        <p style="margin:8px 0 0;opacity:0.85;font-size:14px">
            {_html(header_subtitle)}
        </p>
    </div>

    <!-- OPPORTUNITY SUMMARY -->
    <div style="background:#f8f9fa;padding:20px;
                border-left:4px solid #1F3864;margin-bottom:0">
        <h2 style="margin:0 0 12px;color:#1F3864;font-size:18px">{_html(title)}</h2>
        <table style="width:100%;border-collapse:collapse">
            <tr>
                <td style="padding:4px 12px 4px 0;width:50%">
                    <strong>Client:</strong> {_html(client)}
                </td>
                <td style="padding:4px 0">
                    <strong>Deadline:</strong>
                    <span style="color:{_safe_color(urgency.get('color'), '#dc3545')};font-weight:bold">
                        {_html(deadline)} {_html(urgency.get('prefix', ''))}
                    </span>
                </td>
            </tr>
            <tr>
                <td style="padding:4px 12px 4px 0">
                    <strong>Fit Score:</strong>
                    <span style="color:{score_color};font-weight:bold;font-size:16px">
                        {_html(score)}/100
                    </span>
                    ({_html(recommendation)})
                </td>
                <td style="padding:4px 0">
                    <strong>Budget Cap:</strong> {budget_cap_str}
                </td>
            </tr>
            <tr>
                <td colspan="2">{quality_line}</td>
            </tr>
            <tr>
                <td colspan="2" style="padding:8px 0 4px">
                    <strong>TOR / Source:</strong>
                    <a href="{_safe_href(source_url)}" style="color:#1F3864">{_html(source_url)}</a>
                </td>
            </tr>
        </table>
    </div>

    {action_banner}

    <div style="padding:20px">

    <!-- STRENGTHS AND GAPS -->
    <div style="display:flex;gap:20px;margin-bottom:24px">
        <div style="flex:1;background:#d4edda;padding:16px;border-radius:6px">
            <h3 style="color:#155724;margin:0 0 10px;font-size:14px">
                Key Strengths
            </h3>
            <ul style="margin:0;padding-left:18px">{strengths_html}</ul>
        </div>
        <div style="flex:1;background:#fff3cd;padding:16px;border-radius:6px">
            <h3 style="color:#856404;margin:0 0 10px;font-size:14px">
                Gaps to Address
            </h3>
            <ul style="margin:0;padding-left:18px">{gaps_html}</ul>
        </div>
    </div>

    <!-- TEAM TABLE -->
    <div style="margin-bottom:24px">
        <h3 style="color:#1F3864;font-size:15px;margin:0 0 10px;
                   border-bottom:2px solid #1F3864;padding-bottom:6px">
            Matched Team
        </h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#1F3864;color:white">
                <th style="padding:8px;text-align:left">Required Role</th>
                <th style="padding:8px;text-align:left">Matched Consultant</th>
                <th style="padding:8px;text-align:left">CV Match</th>
                <th style="padding:8px;text-align:left">Availability</th>
            </tr>
            {team_rows_html}
        </table>
    </div>

    {budget_block}

    <!-- PROPOSAL DRAFT -->
    <div style="border-top:3px solid #1F3864;padding-top:20px;margin-top:8px">
        <h2 style="color:#1F3864;margin:0 0 20px">{draft_heading}</h2>
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
            <a href="{_safe_href(source_url)}" style="color:#1F3864">View original TOR</a>
        </p>
    </div>

    </body>
    </html>
    """

    # ── SUBJECT LINE ───────────────────────────────────────────────────────
    safe_deadline_subject = _subject_text(deadline)[:10]
    if is_lightweight:
        subject = (
            f"QUICK FLAG: {_subject_text(title)[:50]} | "
            f"Deadline: {safe_deadline_subject} | "
            f"Score: {score}/100 | WATCH"
        )
    elif is_eoi:
        subject = (
            f"{doc_label}: {_subject_text(title)[:50]} | "
            f"Deadline: {safe_deadline_subject} | "
            f"Score: {score}/100 | REVIEW REQUIRED"
        )
    else:
        subject = (
            f"{doc_label}: {_subject_text(title)[:50]} | "
            f"Deadline: {safe_deadline_subject} | "
            f"Score: {score}/100 | REVIEW REQUIRED"
        )

    if _send_email(subject, html, attachment_path=docx_path):
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
        subject = f"BD ALERT: {urgent_count} Urgent Deadline(s) | {new_count} New Opportunities"
    else:
        subject = f"Cortech BD Report | {new_count} New Opportunities"

    # Send email via Resend HTTP API (Railway blocks SMTP)
    if _send_email(subject, html_content):
        logger.success("BD report email sent!")
    else:
        logger.error(
            "Pipeline summary report email failed via both Gmail and Resend — "
            "check GMAIL_ADDRESS/GMAIL_APP_PASSWORD and RESEND_API_KEY/EMAIL_SENDER "
            "are actually set in Railway's environment (not just present in "
            ".env.example), not just locally."
        )
