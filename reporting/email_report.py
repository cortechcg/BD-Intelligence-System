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
from utils.money_scrub import (
    contains_financial_disclosure,
    strip_financial_table_headers,
    strip_monetary_amounts,
)
from utils.urls import UnsafeURLError, assert_public_http_url


def _html(value) -> str:
    """Render operational data as text, never as HTML supplied by a source."""
    return html.escape(str(value or ""), quote=True)


def _is_markdown_separator(line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped.startswith("|"):
        return False
    return set(stripped.replace(" ", "").replace("\t", "")) <= set("|:-")


def _markdown_table_html(lines: list[str]) -> str:
    """Turn pipe tables in a draft into a dashboard table. Cells stay escaped."""
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    header, *body = rows
    head = "".join(
        f'<th style="padding:10px 12px;text-align:left;font-size:11px;'
        f"letter-spacing:0.06em;text-transform:uppercase;font-weight:600;"
        f'background:#1F3864;color:#ffffff;border:0">{_html(h)}</th>'
        for h in header
    )
    body_html = []
    for i, row in enumerate(body):
        padded = row + [""] * (len(header) - len(row))
        bg = "#F7F4EE" if i % 2 else "#ffffff"
        tds = "".join(
            f'<td style="padding:10px 12px;border-bottom:1px solid #E6E1D6;'
            f'font-size:13px;line-height:1.45;vertical-align:top;background:{bg}">'
            f"{_html(cell)}</td>"
            for cell in padded[: len(header)]
        )
        body_html.append(f"<tr>{tds}</tr>")
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="width:100%;border-collapse:collapse;margin:0 0 18px">'
        f"<tr>{head}</tr>{''.join(body_html)}</table>"
    )


def _draft_body_html(content: str) -> str:
    """Render draft prose and markdown tables for the review dashboard email."""
    lines = (content or "").split("\n")
    parts: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("|"):
            table_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not _is_markdown_separator(lines[i]):
                    table_lines.append(lines[i])
                i += 1
            if table_lines:
                parts.append(_markdown_table_html(table_lines))
            continue
        if set(stripped) <= set("-*_= ") or (
            stripped.startswith("|")
            and set(stripped.replace(" ", "")) <= set("|:-")
        ):
            i += 1
            continue
        parts.append(
            f'<p style="margin:0 0 12px;line-height:1.65;font-size:14px;'
            f'color:#1C1914">{_html(stripped)}</p>'
        )
        i += 1
    return "".join(parts)


def _kpi_tile(label: str, value: str, sub: str = "", value_color: str = "#1F3864") -> str:
    color = _safe_color(value_color, "#1F3864")
    sub_html = (
        f'<div style="font-size:12px;color:#6B6458;margin-top:4px;line-height:1.4">'
        f"{sub}</div>"
        if sub
        else ""
    )
    return f"""
    <td width="25%" valign="top" style="padding:6px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="width:100%;background:#F7F4EE">
        <tr>
          <td style="padding:16px 14px">
            <div style="font-size:10px;letter-spacing:0.14em;text-transform:uppercase;
                        color:#6B6458;font-weight:600">{_html(label)}</div>
            <div style="font-size:22px;font-weight:700;color:{color};margin-top:6px;
                        line-height:1.15">{value}</div>
            {sub_html}
          </td>
        </tr>
      </table>
    </td>"""


def _usd(value) -> str:
    """Format a known finite number; invalid/missing values remain UNKNOWN."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return f"${number:,.0f}" if math.isfinite(number) else "UNKNOWN"


def _usd_known(value) -> str:
    """Show a dollar figure only when it is a real positive amount."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number) or number <= 0:
        return "—"
    return f"${number:,.0f}"


def _internal_budget_html(budget: dict, budget_cap, is_eoi: bool) -> str:
    """Team-only financial working. Must never be copied into the Word draft."""
    budget = budget if isinstance(budget, dict) else {}
    summary = budget.get("summary") if isinstance(budget.get("summary"), dict) else {}
    breakdown = budget.get("personnel_breakdown")
    if not isinstance(breakdown, dict):
        breakdown = {}
    missing = budget.get("missing_inputs") if isinstance(budget.get("missing_inputs"), list) else []
    cap = budget.get("opportunity_budget_cap_usd")
    if cap in (None, "", 0, 0.0):
        cap = budget_cap
    cap_str = _usd_known(cap)
    if cap_str == "—":
        cap_str = "Not specified in the tender extraction"
    known = summary.get("known_personnel_subtotal_usd")
    known_str = _usd_known(known)
    personnel_complete = summary.get("personnel_subtotal_usd")
    personnel_complete_str = _usd_known(personnel_complete)
    location = _html(budget.get("primary_location") or "Not set")
    status = _html(budget.get("status") or "UNKNOWN")
    reason = _html(budget.get("reason") or "No budget basis was supplied.")
    stage_note = (
        "This is an Expression of Interest. Figures below are for internal "
        "planning only — they must not appear in the EOI Word file."
        if is_eoi
        else "Figures below are for internal review only — they must not appear "
        "in the technical proposal Word file."
    )
    rows = ""
    for role, line in breakdown.items():
        if not isinstance(line, dict):
            continue
        days = line.get("estimated_days_of_effort")
        days_str = "—" if days in (None, "") else _html(days)
        rows += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd">{_html(line.get("role") or role)}</td>
            <td style="padding:8px;border:1px solid #ddd">{_html(line.get("level") or "—")}</td>
            <td style="padding:8px;border:1px solid #ddd">{days_str}</td>
            <td style="padding:8px;border:1px solid #ddd">{_usd_known(line.get("day_rate_usd"))}</td>
            <td style="padding:8px;border:1px solid #ddd">{_usd_known(line.get("personnel_cost_usd"))}</td>
            <td style="padding:8px;border:1px solid #ddd">{_html(line.get("status") or "UNKNOWN")}</td>
        </tr>"""
    if not rows:
        rows = """
        <tr>
            <td colspan="6" style="padding:8px;border:1px solid #ddd;color:#856404">
                No personnel line could be costed yet. See missing inputs below.
            </td>
        </tr>"""
    missing_html = "".join(
        f"<li>{_html(item)}</li>" for item in missing[:12]
    ) or "<li>No missing-input detail was recorded.</li>"
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="width:100%;margin:0 0 28px;background:#FFF8EC;border-left:4px solid #C4A35A">
    <tr><td style="padding:20px 20px 8px">
        <div style="font-size:10px;letter-spacing:0.14em;text-transform:uppercase;
                    color:#8A6A12;font-weight:600;margin-bottom:6px">Internal only</div>
        <h3 style="color:#1F3864;font-size:16px;margin:0 0 8px;font-weight:700">
            Internal financial working
        </h3>
        <p style="margin:0 0 14px;font-size:13px;color:#856404;line-height:1.5">
            {_html(stage_note)}
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:12px">
            <tr>
                <td style="padding:8px;border:1px solid #ddd;width:50%">
                    <strong>Tender ceiling</strong><br>{_html(cap_str)}
                </td>
                <td style="padding:8px;border:1px solid #ddd">
                    <strong>Rate-card location</strong><br>{location}
                </td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd">
                    <strong>Status</strong><br>{status}
                </td>
                <td style="padding:8px;border:1px solid #ddd">
                    <strong>Known personnel subtotal</strong><br>
                    {known_str if known_str != "—" else "Not yet calculated"}
                </td>
            </tr>
            <tr>
                <td style="padding:8px;border:1px solid #ddd">
                    <strong>Complete personnel subtotal</strong><br>
                    {personnel_complete_str if personnel_complete_str != "—" else "Incomplete"}
                </td>
                <td style="padding:8px;border:1px solid #ddd">
                    <strong>Overall bid amount</strong><br>Not estimated — non-personnel costs are still missing
                </td>
            </tr>
        </table>
        <p style="margin:0 0 6px;font-size:13px">{reason}</p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;margin:10px 0 12px">
            <tr style="background:#1F3864;color:white">
                <th style="padding:8px;text-align:left">Role</th>
                <th style="padding:8px;text-align:left">Level</th>
                <th style="padding:8px;text-align:left">Days</th>
                <th style="padding:8px;text-align:left">Day rate</th>
                <th style="padding:8px;text-align:left">Personnel cost</th>
                <th style="padding:8px;text-align:left">Status</th>
            </tr>
            {rows}
        </table>
        <p style="margin-bottom:4px"><strong>Still required before a financial submission:</strong></p>
        <ul style="margin-top:4px">{missing_html}</ul>
    </td></tr>
    </table>
"""


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
    team_matches   = matched.get("matched_team", {})
    cap_for_header = (
        budget.get("opportunity_budget_cap_usd") if isinstance(budget, dict) else None
    )
    if cap_for_header in (None, "", 0, 0.0):
        cap_for_header = budget_cap
    budget_cap_str = _usd_known(cap_for_header)
    if budget_cap_str == "—":
        budget_cap_str = "Not specified"
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
    grounding = proposal.get("claim_grounding") if isinstance(proposal.get("claim_grounding"), dict) else {}
    grounding_line = ""
    if grounding:
        nv = int(grounding.get("not_verified") or 0)
        ver = int(grounding.get("verified") or 0)
        ins = int(grounding.get("insufficient_evidence") or 0)
        removed = grounding.get("removed_unverified")
        removed_n = len(removed) if isinstance(removed, list) else 0
        color = "#dc3545" if nv else "#555"
        strip_note = (
            f"{removed_n} unsupported named claim"
            f"{'s' if removed_n != 1 else ''} removed from the client draft."
            if removed_n
            else "Unsupported named claims were removed from the client draft."
        )
        grounding_line = (
            f"<p style='margin:8px 0 0;font-size:13px;color:{color}'>"
            f"Claim grounding: {ver} verified, {nv} not verified, "
            f"{ins} insufficient evidence. {strip_note}"
            f"</p>"
        )

    # ── TEAM TABLE ─────────────────────────────────────────────────────────
    team_rows_html = ""
    for role, match in team_matches.items():
        if not isinstance(match, dict):
            continue
        name  = match.get("consultant_name", "TBD")
        capability = match.get("capability") if isinstance(match.get("capability"), dict) else {}
        cap_score = capability.get("match_score")
        score_pct = _number(
            cap_score if cap_score is not None else match.get("similarity_score", 0)
        )
        avail = match.get("availability_flag") or "Unknown"
        color = "#28a745" if score_pct >= 80 else "#f0a500" if score_pct >= 60 else "#dc3545"
        team_rows_html += f"""
        <tr>
            <td style="padding:12px 14px;border-bottom:1px solid #E6E1D6;font-size:13px">{_html(role)}</td>
            <td style="padding:12px 14px;border-bottom:1px solid #E6E1D6;font-size:13px;font-weight:700;color:#1F3864">{_html(name)}</td>
            <td style="padding:12px 14px;border-bottom:1px solid #E6E1D6;font-size:13px;color:{color};font-weight:700">{score_pct}% match</td>
            <td style="padding:12px 14px;border-bottom:1px solid #E6E1D6;font-size:13px">{_html(avail)}</td>
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
        text, _ = strip_monetary_amounts(content)
        text, _ = strip_financial_table_headers(text)
        if contains_financial_disclosure(text):
            logger.error(
                f"Omitting emailed draft section '{heading}': financial information remains"
            )
            return ""
        body = _draft_body_html(text)
        if not body.strip():
            return ""
        return f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;margin:0 0 28px">
          <tr>
            <td style="width:4px;background:#C4A35A;font-size:0;line-height:0">&nbsp;</td>
            <td style="padding:4px 0 8px 16px">
              <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                          color:#8A7A5A;font-weight:600;margin-bottom:4px">Draft section</div>
              <h3 style="color:#1F3864;font-size:16px;margin:0 0 12px;font-weight:700">
                {heading}
              </h3>
              {body}
            </td>
          </tr>
        </table>"""

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

    lock_text = proposal.get("document_lock") or ""
    lock_block = ""
    if isinstance(lock_text, str) and lock_text.strip():
        lock_block = section_block(
            "Assignment lock from the ToR/RFP/REOI (internal — not in the Word file)",
            lock_text,
        )
    strategy_text = proposal.get("win_strategy") or ""
    strategy_block = ""
    if isinstance(strategy_text, str) and strategy_text.strip():
        strategy_block = section_block(
            "Win strategy (internal — not in the client Word file)",
            strategy_text,
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
        """<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;margin:0 0 8px;background:#E8F1F8">
        <tr>
          <td style="width:4px;background:#2e86c1;font-size:0">&nbsp;</td>
          <td style="padding:16px 20px;font-size:14px;line-height:1.55;color:#1C1914">
            <strong>EOI STAGE:</strong> This document asks for an Expression of Interest only.
            The draft below is a complete shortlisting file (understanding, approach
            summary, experience, team, eligibility, criteria matrix) — not a full
            technical/financial proposal. Review, confirm team availability, then submit
            as an EOI unless you have been invited to the next stage.
          </td>
        </tr>
        </table>"""
        if is_eoi
        else """<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;margin:0 0 8px;background:#FFF4D6">
        <tr>
          <td style="width:4px;background:#f0a500;font-size:0">&nbsp;</td>
          <td style="padding:16px 20px;font-size:14px;line-height:1.55;color:#1C1914">
            <strong>WATCH — QUICK FLAG ONLY:</strong> This is a lightweight preview
            (cover letter + executive summary). No full proposal was generated.
            Review the opportunity and decide whether to pursue a full bid.
          </td>
        </tr>
        </table>"""
        if is_lightweight
        else """<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;margin:0 0 8px;background:#FFF4D6">
        <tr>
          <td style="width:4px;background:#C4A35A;font-size:0">&nbsp;</td>
          <td style="padding:16px 20px;font-size:14px;line-height:1.55;color:#1C1914">
            <strong>ACTION REQUIRED:</strong> Review the draft below, make edits,
            confirm team availability, verify the budget, then approve for submission.
            <strong>Nothing has been sent to the client.</strong>
          </td>
        </tr>
        </table>"""
    )
    draft_heading = (
        "Expression of Interest Draft"
        if is_eoi
        else "Quick-Flag Preview (Cover Letter + Executive Summary)"
        if is_lightweight
        else "Draft Technical Proposal"
    )
    budget_block = _internal_budget_html(budget, budget_cap, is_eoi)
    if not team_rows_html:
        team_rows_html = (
            '<tr><td colspan="4" style="padding:12px 14px;color:#6B6458">'
            "No consultants were matched for this draft.</td></tr>"
        )
    source_href = _safe_href(source_url)
    source_line = (
        f'<a href="{source_href}" style="color:#C4A35A;text-decoration:underline">'
        f"{_html(source_url)}</a>"
        if source_href
        else _html(source_url or "Not provided")
    )
    stage_label = (
        "Expression of Interest"
        if is_eoi
        else "Quick flag"
        if is_lightweight
        else "Technical proposal"
    )

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>
    <body style="margin:0;padding:0;background:#EDE9E0;color:#1C1914;
                 font-family:Georgia,'Times New Roman',serif;font-size:14px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="width:100%;background:#EDE9E0">
    <tr>
      <td align="center" style="padding:28px 12px">
      <table role="presentation" width="680" cellpadding="0" cellspacing="0"
             style="width:680px;max-width:680px;background:#ffffff">

    <tr>
      <td style="background:#1F3864;padding:28px 32px 24px">
        <div style="font-size:10px;letter-spacing:0.18em;text-transform:uppercase;
                    color:#C4A35A;font-weight:700;margin-bottom:10px;
                    font-family:Arial,Helvetica,sans-serif">
          Cortech BD Intelligence · {_html(stage_label)}
        </div>
        <h1 style="margin:0;font-size:26px;line-height:1.2;color:#ffffff;
                   font-weight:700;font-family:Georgia,'Times New Roman',serif">
          {header_title}
        </h1>
        <p style="margin:10px 0 0;color:#D9D2C5;font-size:14px;line-height:1.5;
                  font-family:Arial,Helvetica,sans-serif">
          {_html(header_subtitle)}
        </p>
      </td>
    </tr>

    <tr>
      <td style="padding:28px 32px 8px">
        <h2 style="margin:0 0 6px;color:#1F3864;font-size:22px;line-height:1.3;
                   font-weight:700">{_html(title)}</h2>
        <p style="margin:0 0 18px;color:#6B6458;font-size:14px;
                  font-family:Arial,Helvetica,sans-serif">
          {_html(client)}
        </p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%">
          <tr>
            {_kpi_tile("Fit score", f"{_html(score)}/100", _html(recommendation), score_color)}
            {_kpi_tile("Deadline", _html(str(deadline)[:10] if deadline else "TBD"), _html(urgency.get("prefix") or "Review timing"), _safe_color(urgency.get("color"), "#1F3864"))}
            {_kpi_tile("Ceiling (internal)", _html(budget_cap_str), "do not copy into the technical/EOI", "#1F3864")}
            {_kpi_tile("Status", "Review", "Not sent to the client", "#1F3864")}
          </tr>
        </table>
        <div style="font-family:Arial,Helvetica,sans-serif;padding:8px 6px 0">
          {quality_line}{grounding_line}
          <p style="margin:10px 0 0;font-size:13px;color:#6B6458">
            <strong style="color:#1F3864">TOR / Source:</strong> {source_line}
          </p>
        </div>
      </td>
    </tr>

    <tr><td style="padding:12px 32px 0">{action_banner}</td></tr>

    <tr>
      <td style="padding:20px 32px 8px;font-family:Arial,Helvetica,sans-serif">
        {budget_block}

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;margin:0 0 28px">
          <tr>
            <td width="50%" valign="top" style="padding:0 8px 0 0">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                     style="width:100%;background:#F1F7F2">
                <tr><td style="padding:16px 18px">
                  <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                              color:#155724;font-weight:700;margin-bottom:8px">Key Strengths</div>
                  <ul style="margin:0;padding-left:18px;color:#1C1914;font-size:13px;
                             line-height:1.5">{strengths_html}</ul>
                </td></tr>
              </table>
            </td>
            <td width="50%" valign="top" style="padding:0 0 0 8px">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                     style="width:100%;background:#FFF8EC">
                <tr><td style="padding:16px 18px">
                  <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                              color:#856404;font-weight:700;margin-bottom:8px">Gaps to Address</div>
                  <ul style="margin:0;padding-left:18px;color:#1C1914;font-size:13px;
                             line-height:1.5">{gaps_html}</ul>
                </td></tr>
              </table>
            </td>
          </tr>
        </table>

        <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                    color:#8A7A5A;font-weight:600;margin:0 0 8px">People</div>
        <h3 style="color:#1F3864;font-size:16px;margin:0 0 12px;font-weight:700;
                   font-family:Georgia,'Times New Roman',serif">Matched Team</h3>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;border-collapse:collapse;margin:0 0 28px">
            <tr style="background:#1F3864;color:white">
                <th style="padding:10px 12px;text-align:left;font-size:11px;
                           letter-spacing:0.06em;text-transform:uppercase">Required Role</th>
                <th style="padding:10px 12px;text-align:left;font-size:11px;
                           letter-spacing:0.06em;text-transform:uppercase">Matched Consultant</th>
                <th style="padding:10px 12px;text-align:left;font-size:11px;
                           letter-spacing:0.06em;text-transform:uppercase">CV Match</th>
                <th style="padding:10px 12px;text-align:left;font-size:11px;
                           letter-spacing:0.06em;text-transform:uppercase">Availability</th>
            </tr>
            {team_rows_html}
        </table>

        {lock_block}

        {strategy_block}

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;margin:12px 0 20px">
          <tr>
            <td style="border-top:2px solid #1F3864;padding-top:20px">
              <div style="font-size:10px;letter-spacing:0.14em;text-transform:uppercase;
                          color:#C4A35A;font-weight:700;margin-bottom:6px">
                Client-facing file · no fees, rates, or budgets
              </div>
              <h2 style="color:#1F3864;margin:0 0 20px;font-size:20px;font-weight:700;
                         font-family:Georgia,'Times New Roman',serif">{draft_heading}</h2>
              {proposal_html}
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <tr>
      <td style="background:#1F3864;padding:18px 32px;font-family:Arial,Helvetica,sans-serif">
        <p style="font-size:12px;color:#D9D2C5;margin:0;line-height:1.5">
            Generated by Cortech BD Intelligence Agent ·
            This is a draft — human review and approval required before any submission ·
            <a href="{source_href}" style="color:#C4A35A">View original TOR</a>
        </p>
      </td>
    </tr>

      </table>
      </td>
    </tr>
    </table>
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
