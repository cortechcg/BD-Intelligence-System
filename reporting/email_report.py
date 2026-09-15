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
from reporting.docx_builder import build_proposal_docx, iter_client_sections
from intelligence.tender_reader import build_format_compliance
from intelligence.organizations import build_client_intelligence, reviewer_sentences
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
        f'<div style="font-size:12px;color:#6B6458;margin-top:4px;line-height:1.4;'
        f'font-family:Arial,Helvetica,sans-serif">{sub}</div>'
        if sub
        else ""
    )
    return f"""
    <td width="50%" valign="top" style="padding:4px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="width:100%;background:#F7F4EE">
        <tr>
          <td style="padding:14px 16px">
            <div style="font-size:10px;letter-spacing:0.14em;text-transform:uppercase;
                        color:#6B6458;font-weight:600;font-family:Arial,Helvetica,sans-serif">
              {_html(label)}</div>
            <div style="font-size:22px;font-weight:700;color:{color};margin-top:6px;
                        line-height:1.15">{value}</div>
            {sub_html}
          </td>
        </tr>
      </table>
    </td>"""


def _kpi_grid(tiles: list[str]) -> str:
    """Two-up tiles — four-column rows collapse unreadably in Outlook/Gmail."""
    rows = []
    for i in range(0, len(tiles), 2):
        pair = tiles[i : i + 2]
        if len(pair) == 1:
            pair.append('<td width="50%" style="padding:4px">&nbsp;</td>')
        rows.append(f"<tr>{''.join(pair)}</tr>")
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="width:100%">{"".join(rows)}</table>'
    )


def _preheader_html(text: str) -> str:
    """Inbox preview line. Hidden in the opened message."""
    return (
        '<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
        'opacity:0;color:transparent;font-size:1px;line-height:1px">'
        f"{_html(text)}</div>"
    )


def _jump_nav_html(items: list[tuple[str, str]]) -> str:
    links = []
    for i, (anchor, label) in enumerate(items):
        sep = (
            '<span style="color:#8A7A5A;padding:0 8px">·</span>' if i else ""
        )
        links.append(
            f'{sep}<a href="#{_html(anchor)}" style="color:#C4A35A;text-decoration:none;'
            f'font-size:11px;letter-spacing:0.08em;text-transform:uppercase;'
            f'font-weight:700">{_html(label)}</a>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="width:100%;background:#162A4A">'
        '<tr><td style="padding:12px 32px;font-family:Arial,Helvetica,sans-serif">'
        f'{"".join(links)}</td></tr></table>'
    )


def _named_anchor(anchor_id: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "", (anchor_id or "").lower())[:48]
    if not slug:
        return ""
    return f'<a id="{slug}" name="{slug}" style="line-height:0;font-size:0"></a>'


def _team_coverage(team_matches) -> tuple[int, int, list[str]]:
    """Named consultants vs required roles, plus duplicate-mapping gaps."""
    named = 0
    total = 0
    gaps: list[str] = []
    seen: dict[str, str] = {}
    if not isinstance(team_matches, dict):
        return 0, 0, gaps
    blank = {"", "tbd", "unfilled", "unknown", "none", "n/a"}
    for role, match in team_matches.items():
        if not isinstance(match, dict):
            continue
        total += 1
        role_label = str(role or "Role")
        name = str(match.get("consultant_name") or "").strip()
        if name.lower() in blank:
            gaps.append(f"{role_label}: no named consultant")
            continue
        named += 1
        key = name.lower()
        prior = seen.get(key)
        if prior:
            gaps.append(f"{name} is mapped to both {prior} and {role_label}")
        else:
            seen[key] = role_label
    return named, total, gaps


def _checklist_html(items: list[tuple[str, str]]) -> str:
    if not items:
        return ""
    rows = []
    for i, (label, body) in enumerate(items):
        border = "border-bottom:1px solid #E6E1D6;" if i < len(items) - 1 else ""
        rows.append(f"""
        <tr>
          <td valign="top" width="92" style="width:92px;padding:10px 12px 10px 0;
              font-family:Arial,Helvetica,sans-serif">
            <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                        color:#8A7A5A;font-weight:700">{_html(label)}</div>
          </td>
          <td valign="top" style="padding:10px 0;font-size:13px;line-height:1.5;
              color:#1C1914;{border}font-family:Arial,Helvetica,sans-serif">
            {_html(body)}
          </td>
        </tr>""")
    return f"""
    {_named_anchor("c-decision")}
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="width:100%;margin:0 0 28px">
      <tr>
        <td style="padding:0 0 12px">
          <div style="font-size:10px;letter-spacing:0.14em;text-transform:uppercase;
                      color:#C4A35A;font-weight:700;font-family:Arial,Helvetica,sans-serif">
            Review before submit</div>
          <h3 style="color:#1F3864;font-size:16px;margin:4px 0 0;font-weight:700">
            What still needs a human</h3>
        </td>
      </tr>
      {"".join(rows)}
    </table>"""


def _client_intelligence_payload(opportunity_result: dict, client: str) -> dict:
    """Prefer orchestrator roll-up; otherwise a local empty-history view (no IO)."""
    intel = opportunity_result.get("client_intelligence")
    if isinstance(intel, dict) and isinstance(intel.get("client"), dict):
        return intel
    raw = client if isinstance(client, str) else ""
    if raw.strip().casefold() in {"unknown client", "unknown"}:
        raw = ""
    return build_client_intelligence(
        client=raw,
        persist=False,
        fetch_stored=False,
        index=[],
        observed=[],
    )


def _client_intelligence_html(payload: dict | None) -> str:
    """Cited client/donor history. Counts are code aggregations; never invents wins."""
    if not isinstance(payload, dict):
        return ""
    client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
    match = client.get("match") if isinstance(client.get("match"), dict) else {}
    name = match.get("canonical_name") or match.get("query_sanitized") or ""
    method = match.get("method") or "empty"
    status = match.get("status") or "UNKNOWN"
    citations = client.get("citations") if isinstance(client.get("citations"), list) else []
    cite_rows = []
    for cite in citations[:8]:
        if not isinstance(cite, dict):
            continue
        title = cite.get("title") or cite.get("source_id") or "untitled"
        cite_rows.append(
            "<li style='margin-bottom:4px'>"
            f"{_html(title)} "
            f"({_html(cite.get('source_kind'))} {_html(cite.get('source_id'))}) "
            f"— outcome {_html(cite.get('outcome') or 'UNKNOWN')}"
            "</li>"
        )
    if not cite_rows:
        cite_rows.append(
            "<li style='margin-bottom:4px'>No stored past records cited.</li>"
        )
    overlap = client.get("known_client_overlap") or ""
    overlap_html = (
        f"<p style='margin:10px 0 0;font-size:13px;line-height:1.5;color:#1C1914'>"
        f"{_html(overlap)}</p>"
        if overlap
        else ""
    )
    donor = payload.get("donor") if isinstance(payload.get("donor"), dict) else None
    donor_html = ""
    if donor and donor.get("headline"):
        donor_html = (
            f"<p style='margin:10px 0 0;font-size:13px;line-height:1.5;color:#1C1914'>"
            f"{_html(donor.get('headline'))}</p>"
        )
        if donor.get("outcome_note"):
            donor_html += (
                f"<p style='margin:6px 0 0;font-size:13px;line-height:1.5;color:#6B6458'>"
                f"{_html(donor.get('outcome_note'))}</p>"
            )
    heading_name = _html(name) if name else "UNKNOWN"
    return f"""
    {_named_anchor("c-client")}
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="width:100%;margin:0 0 28px;background:#F7F4EE">
      <tr>
        <td style="padding:16px 18px">
          <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                      color:#8A7A5A;font-weight:700;margin-bottom:8px;
                      font-family:Arial,Helvetica,sans-serif">
            Client history · observed records only</div>
          <h3 style="color:#1F3864;font-size:16px;margin:0 0 8px;font-weight:700;
                     font-family:Georgia,'Times New Roman',serif">
            {heading_name}
          </h3>
          <p style="margin:0 0 8px;font-size:12px;color:#6B6458;
                    font-family:Arial,Helvetica,sans-serif">
            Match: {_html(method)} ({_html(status)}). Code counts from stored
            rows — not an LLM estimate. Nothing has been sent to the client.
          </p>
          <p style="margin:0;font-size:13px;line-height:1.5;color:#1C1914">
            {_html(client.get("headline") or "")}
          </p>
          <p style="margin:8px 0 0;font-size:13px;line-height:1.5;color:#6B6458">
            {_html(client.get("outcome_note") or "")}
          </p>
          {overlap_html}
          {donor_html}
          <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                      color:#8A7A5A;font-weight:700;margin:12px 0 6px;
                      font-family:Arial,Helvetica,sans-serif">Cited records</div>
          <ul style="margin:0;padding-left:18px;color:#1C1914;font-size:13px;
                     line-height:1.5">{"".join(cite_rows)}</ul>
        </td>
      </tr>
    </table>"""


def _relationship_facts_payload(opportunity_result: dict | None) -> list[dict]:
    """Cited past-submission partners only. Fail-open to []."""
    result = opportunity_result if isinstance(opportunity_result, dict) else {}
    injected = result.get("relationship_facts")
    if isinstance(injected, list) and injected:
        return [row for row in injected if isinstance(row, dict)]
    try:
        from database.intelligence_facts import load_relationship_edges

        return load_relationship_edges(limit=8)
    except Exception:
        return []


def _relationship_facts_html(rows: list[dict] | None) -> str:
    """Small cited-partner section. Omitted when the store is empty."""
    facts = [r for r in (rows or []) if isinstance(r, dict)]
    if not facts:
        return ""
    items = []
    for row in facts[:8]:
        name = row.get("observed_name") or ""
        kind = row.get("relationship_kind") or ""
        doc = row.get("document_name") or row.get("chunk_id") or ""
        excerpt = row.get("excerpt") or ""
        items.append(
            "<li style='margin-bottom:8px'>"
            f"<strong>{_html(name)}</strong> — {_html(kind)}"
            f"<br><span style='color:#6B6458;font-size:12px'>"
            f"{_html(doc)}</span>"
            f"<br><span style='font-size:12px;color:#1C1914'>"
            f"{_html(excerpt[:240])}</span>"
            "</li>"
        )
    return f"""
    {_named_anchor("c-partners")}
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="width:100%;margin:0 0 28px;background:#F7F4EE">
      <tr>
        <td style="padding:16px 18px">
          <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                      color:#8A7A5A;font-weight:700;margin-bottom:8px;
                      font-family:Arial,Helvetica,sans-serif">
            Cited past partners · Cortech submissions only</div>
          <p style="margin:0 0 8px;font-size:12px;color:#6B6458;
                    font-family:Arial,Helvetica,sans-serif">
            VERIFIED edges from data/proposals (document + excerpt).
            Typical-partner language is not stored. Nothing has been sent.
          </p>
          <ul style="margin:0;padding-left:18px;color:#1C1914;font-size:13px;
                     line-height:1.5">{"".join(items)}</ul>
        </td>
      </tr>
    </table>"""


def _status_color(status: str) -> str:
    return {
        "ok": "#28a745",
        "no_limit": "#1F3864",
        "over": "#dc3545",
        "under": "#f0a500",
        "gantt_missing": "#dc3545",
    }.get(status or "", "#1F3864")


def _format_compliance_html(proposal: dict) -> str:
    """ToR vs house format, page limits vs actual, Gantt present/missing."""
    proposal = proposal if isinstance(proposal, dict) else {}
    fc = proposal.get("format_compliance")
    if not isinstance(fc, dict) or not fc.get("rows"):
        try:
            fc = build_format_compliance(
                proposal, proposal.get("submission_outline") or {}
            )
        except Exception:
            fc = {}
    if not isinstance(fc, dict):
        fc = {}
    rows = fc.get("rows") if isinstance(fc.get("rows"), list) else []
    structure = str(fc.get("structure") or "Format not recorded")
    attachments = fc.get("required_attachments") or proposal.get("required_attachments") or []
    forms = fc.get("required_forms") or proposal.get("required_forms") or []
    financial = fc.get("omitted_financial") or proposal.get("omitted_financial") or []
    if not isinstance(attachments, list):
        attachments = []
    if not isinstance(forms, list):
        forms = []
    if not isinstance(financial, list):
        financial = []

    body_rows = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "no_limit")
        color = _status_color(status)
        gantt = str(row.get("gantt") or "not_required")
        gantt_label = {
            "yes": "Gantt included",
            "missing": "Gantt missing",
            "not_required": "—",
        }.get(gantt, gantt)
        limit = str(row.get("page_limit") or "None stated")
        bg = "#F7F4EE" if i % 2 else "#ffffff"
        body_rows.append(f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #E6E1D6;font-size:13px;
                     background:{bg}">{_html(row.get("heading") or row.get("key"))}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #E6E1D6;font-size:13px;
                     background:{bg}">{_html(limit)}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #E6E1D6;font-size:13px;
                     background:{bg}">{_html(row.get("note") or "")}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #E6E1D6;font-size:13px;
                     font-weight:700;color:{color};background:{bg}">{_html(gantt_label)}</td>
        </tr>""")
    if not body_rows:
        body_rows.append(
            '<tr><td colspan="4" style="padding:12px 14px;color:#6B6458">'
            "No drafted sections to audit.</td></tr>"
        )

    extra_bits = []
    if financial:
        extra_bits.append(
            "<p style='margin:10px 0 0;font-size:13px;line-height:1.5;color:#1C1914'>"
            "<strong style='color:#1F3864'>Financial envelope (not in this file):</strong> "
            f"{_html('; '.join(str(x) for x in financial[:6]))}</p>"
        )
    if attachments:
        extra_bits.append(
            "<p style='margin:10px 0 0;font-size:13px;line-height:1.5;color:#1C1914'>"
            "<strong style='color:#1F3864'>Attach (do not treat as chapters):</strong> "
            f"{_html('; '.join(str(x) for x in attachments[:8]))}</p>"
        )
    if forms:
        extra_bits.append(
            "<p style='margin:10px 0 0;font-size:13px;line-height:1.5;color:#1C1914'>"
            "<strong style='color:#1F3864'>Signed forms / annexes:</strong> "
            f"{_html('; '.join(str(x) for x in forms[:8]))}</p>"
        )
    extras = "".join(extra_bits)
    return f"""
    {_named_anchor("c-format")}
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="width:100%;margin:0 0 28px">
      <tr>
        <td style="padding:0 0 12px">
          <div style="font-size:10px;letter-spacing:0.14em;text-transform:uppercase;
                      color:#C4A35A;font-weight:700;font-family:Arial,Helvetica,sans-serif">
            Format lock</div>
          <h3 style="color:#1F3864;font-size:16px;margin:4px 0 6px;font-weight:700">
            ToR format vs this draft</h3>
          <p style="margin:0;font-size:13px;color:#6B6458;font-family:Arial,Helvetica,sans-serif">
            {_html(structure)}</p>
        </td>
      </tr>
      <tr>
        <td>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="width:100%;border-collapse:collapse">
            <tr style="background:#1F3864;color:white">
              <th style="padding:10px 12px;text-align:left;font-size:11px;
                         letter-spacing:0.06em;text-transform:uppercase">Section</th>
              <th style="padding:10px 12px;text-align:left;font-size:11px;
                         letter-spacing:0.06em;text-transform:uppercase">ToR limit</th>
              <th style="padding:10px 12px;text-align:left;font-size:11px;
                         letter-spacing:0.06em;text-transform:uppercase">This draft</th>
              <th style="padding:10px 12px;text-align:left;font-size:11px;
                         letter-spacing:0.06em;text-transform:uppercase">Gantt</th>
            </tr>
            {"".join(body_rows)}
          </table>
          {extras}
        </td>
      </tr>
    </table>"""


def _grounding_table_html(grounding: dict) -> str:
    claims = grounding.get("claims") if isinstance(grounding.get("claims"), list) else []
    rows = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        status = str(claim.get("status") or "")
        if status not in {"NOT VERIFIED", "INSUFFICIENT EVIDENCE"}:
            continue
        sentence = str(claim.get("sentence") or "")
        preview = sentence[:140] + ("…" if len(sentence) > 140 else "")
        tone = "#dc3545" if status == "NOT VERIFIED" else "#8A6A12"
        rows.append(f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #E6E1D6;font-size:12px;
                     color:{tone};font-weight:700;white-space:nowrap;
                     font-family:Arial,Helvetica,sans-serif">{_html(status)}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #E6E1D6;font-size:12px;
                     color:#6B6458;font-family:Arial,Helvetica,sans-serif">
            {_html(claim.get("section") or "")}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #E6E1D6;font-size:13px;
                     line-height:1.45">{_html(preview)}</td>
        </tr>""")
        if len(rows) >= 8:
            break
    if not rows:
        return ""
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="width:100%;margin:0 0 28px">
      <tr><td style="padding:0 0 10px">
        <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                    color:#8A7A5A;font-weight:600;font-family:Arial,Helvetica,sans-serif">
          Evidence</div>
        <h3 style="color:#1F3864;font-size:16px;margin:4px 0 0;font-weight:700">
          Claims that did not pass grounding</h3>
      </td></tr>
      <tr><td>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;border-collapse:collapse">
          <tr style="background:#1F3864;color:#ffffff">
            <th style="padding:10px 12px;text-align:left;font-size:11px;letter-spacing:0.06em;
                       text-transform:uppercase;font-family:Arial,Helvetica,sans-serif">Status</th>
            <th style="padding:10px 12px;text-align:left;font-size:11px;letter-spacing:0.06em;
                       text-transform:uppercase;font-family:Arial,Helvetica,sans-serif">Section</th>
            <th style="padding:10px 12px;text-align:left;font-size:11px;letter-spacing:0.06em;
                       text-transform:uppercase;font-family:Arial,Helvetica,sans-serif">Claim</th>
          </tr>
          {"".join(rows)}
        </table>
      </td></tr>
    </table>"""


def _draft_toc_html(items: list[tuple[str, str]]) -> str:
    if len(items) < 2:
        return ""
    links = []
    for i, (anchor, heading) in enumerate(items):
        sep = '<span style="color:#C4A35A;padding:0 6px">·</span>' if i else ""
        links.append(
            f'{sep}<a href="#{_html(anchor)}" style="color:#1F3864;text-decoration:underline;'
            f'font-size:13px">{_html(heading)}</a>'
        )
    return (
        '<p style="margin:0 0 18px;line-height:1.7;font-family:Arial,Helvetica,sans-serif">'
        f'{"".join(links)}</p>'
    )


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
        {_named_anchor("c-budget")}
        <div style="font-size:10px;letter-spacing:0.14em;text-transform:uppercase;
                    color:#8A6A12;font-weight:600;margin-bottom:6px">Internal only</div>
        <h3 style="color:#1F3864;font-size:16px;margin:0 0 8px;font-weight:700">
            Internal financial working
        </h3>
        <p style="margin:0 0 14px;font-size:13px;color:#856404;line-height:1.5">
            {_html(stage_note)} Figures below do not copy into the technical/EOI Word file.
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


def _count(value) -> int:
    """Non-negative whole number for counts shown in the review dashboard."""
    number = _number(value, 0)
    return max(0, int(number))


def _whole(value) -> str:
    """Display scores and percentages without a trailing .0."""
    return str(int(round(_number(value, 0))))


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


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


def _frequency_section_html(title: str, window) -> str:
    from intelligence.market_trends import INSUFFICIENT_TREND_PHRASE, TREND_MIN_N

    heading = (
        f"{title} — trailing {window.days} days "
        f"({window.start.isoformat()} to {window.end.isoformat()}, "
        f"n={window.sample_size} dated rows)"
    )
    if not window.is_trend:
        return f"""
        <h3 style="margin:20px 0 8px;font-size:16px">{_html(heading)}</h3>
        <p style="margin:0 0 12px">{_html(INSUFFICIENT_TREND_PHRASE)}
        {_html(f'(n={window.sample_size} < {TREND_MIN_N}; {window.sample_clause()}).')}
        Percentages and charts are withheld. Evidence: INSUFFICIENT DATA — not a trend.</p>
        """
    rows_html = ""
    for item in window.counts:
        pct = (100.0 * item.count / window.sample_size) if window.sample_size else 0
        rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd">{_html(item.label)}</td>
            <td style="padding:8px;border:1px solid #ddd">{_html(item.count)}</td>
            <td style="padding:8px;border:1px solid #ddd">
                {_html(f'{pct:.0f}% of n={window.sample_size}')}
            </td>
        </tr>"""
    return f"""
    <h3 style="margin:20px 0 8px;font-size:16px">{_html(heading)}</h3>
    <p style="margin:0 0 8px;font-size:13px;color:#555">{_html(window.message)}</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="background:#1F3864;color:white">
            <th style="padding:8px;text-align:left">Observed label</th>
            <th style="padding:8px;text-align:left">Count</th>
            <th style="padding:8px;text-align:left">Share (sample size inline)</th>
        </tr>
        {rows_html}
    </table>
    """


def _donor_section_html(window) -> str:

    heading = (
        f"Donor posting counts — trailing {window.days} days "
        f"({window.start.isoformat()} to {window.end.isoformat()}, "
        f"n={window.sample_size} dated rows)"
    )
    if window.status != "VERIFIED" or not window.donors:
        return f"""
        <h3 style="margin:20px 0 8px;font-size:16px">{_html(heading)}</h3>
        <p style="margin:0 0 12px">{_html(window.message)}</p>
        """
    rows_html = ""
    for item in window.donors:
        rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd">{_html(item.canonical_name)}</td>
            <td style="padding:8px;border:1px solid #ddd">{_html(item.count)}</td>
            <td style="padding:8px;border:1px solid #ddd">
                {_html(f'{item.count} of n={window.sample_size} dated rows')}
            </td>
            <td style="padding:8px;border:1px solid #ddd">{_html(item.match_status)}</td>
        </tr>"""
    return f"""
    <h3 style="margin:20px 0 8px;font-size:16px">{_html(heading)}</h3>
    <p style="margin:0 0 8px;font-size:13px;color:#555">{_html(window.message)}</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="background:#1F3864;color:white">
            <th style="padding:8px;text-align:left">Donor (organizations table)</th>
            <th style="padding:8px;text-align:left">Postings</th>
            <th style="padding:8px;text-align:left">Sample</th>
            <th style="padding:8px;text-align:left">Match</th>
        </tr>
        {rows_html}
    </table>
    """


def _cited_awards_html(digest) -> str:
    """Optional Assortis award-winner facts. Honest gap note when empty."""
    note = getattr(digest, "cited_award_note", "") or (
        "No cited Assortis award-winner rows in store."
    )
    rows = getattr(digest, "cited_awards", ()) or ()
    items = []
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        name = row.get("observed_name") or ""
        url = row.get("source_url") or ""
        excerpt = row.get("excerpt") or ""
        title = row.get("opportunity_title") or ""
        if url.startswith("https://") or url.startswith("http://"):
            href = (
                f'<a href="{_html(url)}" style="color:#1F3864">{_html(url)}</a>'
            )
        else:
            href = _html(url)
        items.append(
            "<li style='margin-bottom:8px'>"
            f"<strong>{_html(name)}</strong>"
            f"{' — ' + _html(title) if title else ''}"
            f"<br>{href}"
            f"<br><span style='font-size:12px;color:#555'>{_html(excerpt[:240])}</span>"
            "</li>"
        )
    list_html = (
        f"<ul style='margin:0;padding-left:18px;font-size:13px'>{''.join(items)}</ul>"
        if items
        else ""
    )
    return f"""
        <h3 style="margin:20px 0 8px;font-size:16px">Cited award winners</h3>
        <p style="margin:0 0 8px;font-size:13px;color:#555">{_html(note)}</p>
        {list_html}
    """


def build_market_digest_html(digest) -> str:
    """Internal observed-data digest. Labels from the store are HTML-escaped."""
    from intelligence.market_trends import INSUFFICIENT_TREND_PHRASE, TREND_MIN_N

    notes = "".join(
        f'<p style="margin:0 0 8px">{_html(note)}</p>' for note in (digest.notes or ())
    )
    sources = ", ".join(digest.sources) if digest.sources else "none"
    theme_html = "".join(
        _frequency_section_html("Thematic areas", digest.themes[days])
        for days in (30, 90)
        if days in digest.themes
    )
    geo_html = "".join(
        _frequency_section_html("Geography", digest.geography[days])
        for days in (30, 90)
        if days in digest.geography
    )
    shift = digest.geo_shift
    if shift is None or shift.status != "VERIFIED" or not shift.rows:
        shift_html = f"""
        <h3 style="margin:20px 0 8px;font-size:16px">Geography distribution shift</h3>
        <p style="margin:0 0 12px">{_html((shift.message if shift else INSUFFICIENT_TREND_PHRASE))}</p>
        """
    else:
        shift_rows = ""
        n30 = digest.geography[30].sample_size
        n90 = digest.geography[90].sample_size
        w30 = digest.geography[30]
        w90 = digest.geography[90]
        for row in shift.rows:
            shift_rows += f"""
            <tr>
                <td style="padding:8px;border:1px solid #ddd">{_html(row.label)}</td>
                <td style="padding:8px;border:1px solid #ddd">
                    {_html(f'{row.count_30} ({row.share_30 * 100:.0f}% of n={n30}, {w30.start.isoformat()} to {w30.end.isoformat()})')}
                </td>
                <td style="padding:8px;border:1px solid #ddd">
                    {_html(f'{row.count_90} ({row.share_90 * 100:.0f}% of n={n90}, {w90.start.isoformat()} to {w90.end.isoformat()})')}
                </td>
                <td style="padding:8px;border:1px solid #ddd">{_html(f'{row.delta_pp:+.1f} pp')}</td>
            </tr>"""
        shift_html = f"""
        <h3 style="margin:20px 0 8px;font-size:16px">Geography distribution shift</h3>
        <p style="margin:0 0 8px;font-size:13px;color:#555">{_html(shift.message)}</p>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#1F3864;color:white">
                <th style="padding:8px;text-align:left">Observed location</th>
                <th style="padding:8px;text-align:left">30d share</th>
                <th style="padding:8px;text-align:left">90d share</th>
                <th style="padding:8px;text-align:left">Delta</th>
            </tr>
            {shift_rows}
        </table>
        """
    donor_html = "".join(
        _donor_section_html(digest.donor_windows[days])
        for days in (30, 90)
        if days in digest.donor_windows
    )
    org_note = (
        f"Organizations index: {digest.donor_org_count} canonical rows "
        f"({'available' if digest.donor_orgs_available else 'unavailable/empty'})."
    )
    overall = (
        "Window n meets the trend minimum."
        if digest.overall_is_trend()
        else f"{INSUFFICIENT_TREND_PHRASE} (need n>={TREND_MIN_N} dated rows in a window)."
    )
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;color:#333">
    <div style="background:#1F3864;color:white;padding:20px;border-radius:8px 8px 0 0">
        <h1 style="margin:0;font-size:22px">Observed-data market digest</h1>
        <p style="margin:8px 0 0;opacity:0.85">
            Internal. Counts are stored opportunities already discovered —
            not external market research.
        </p>
    </div>
    <div style="padding:20px">
        <p><strong>As of {_html(digest.as_of.isoformat())}.</strong>
        Store rows: {_html(digest.store_row_count)}
        (dated: {_html(digest.dated_row_count)}; sources: {_html(sources)}).
        {_html(overall)}</p>
        <p style="font-size:13px;color:#555">{_html(org_note)}
        Trend minimum: n&gt;={_html(TREND_MIN_N)} dated rows per window.
        Undated rows are excluded from windows (dates are never invented).</p>
        {notes}
        {theme_html}
        {geo_html}
        {shift_html}
        {donor_html}
        {_cited_awards_html(digest)}
        <p style="font-size:12px;color:#666;margin-top:16px">
            Human review only. Nothing in this digest submits or acts externally.
            Categories not present in the store are not shown.
        </p>
    </div>
    </body>
    </html>
    """


def send_market_digest_email(digest) -> None:
    """Email the observed-data digest. Tests must mock SMTP via _send_email."""
    from intelligence.market_trends import INSUFFICIENT_TREND_PHRASE

    n30 = digest.themes[30].sample_size if 30 in digest.themes else 0
    flag = "trend" if digest.overall_is_trend() else INSUFFICIENT_TREND_PHRASE
    subject = _subject_text(
        f"Cortech observed-data market digest | {digest.as_of.isoformat()} | "
        f"n={n30} dated rows (30d) | {flag}"
    )
    html = build_market_digest_html(digest)
    if _send_email(subject, html):
        logger.success(
            f"Market digest email sent (store_rows={digest.store_row_count}, "
            f"dated={digest.dated_row_count})"
        )
    else:
        logger.error("Market digest email failed to send")


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
    proposal    = _as_dict(opportunity_result.get("proposal_sections"))
    budget      = _as_dict(opportunity_result.get("budget"))
    analysis    = _as_dict(opportunity_result.get("analysis"))
    matched     = _as_dict(opportunity_result.get("matched_team"))
    client_intel = _client_intelligence_payload(opportunity_result, client)
    recommendation = opportunity_result.get("recommendation", "WATCH")
    is_lightweight = proposal.get("lightweight", False)
    submission_type = proposal.get("submission_type", "FULL_PROPOSAL")
    is_eoi = submission_type == "EOI"
    doc_label = "EXPRESSION OF INTEREST" if is_eoi else "DRAFT PROPOSAL"

    opportunity = dict(_as_dict(analysis.get("opportunity")))
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

    bid_analysis   = _as_dict(analysis.get("bid_analysis"))
    key_strengths  = _as_list(bid_analysis.get("key_strengths"))
    key_gaps       = _as_list(bid_analysis.get("key_gaps"))
    team_matches   = _as_dict(matched.get("matched_team"))
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
            f"Self-assessed: <strong>{_html(_whole(quality['overall_score']))}/100</strong> — "
            f"weakest: {_html(quality.get('weakest_criterion', 'N/A'))}. "
            f"{_html(quality.get('one_improvement', ''))}"
            f"</p>"
        )
    grounding = proposal.get("claim_grounding") if isinstance(proposal.get("claim_grounding"), dict) else {}
    grounding_line = ""
    if grounding:
        nv = _count(grounding.get("not_verified"))
        ver = _count(grounding.get("verified"))
        ins = _count(grounding.get("insufficient_evidence"))
        if nv:
            strip_note = (
                f"{nv} named past-work claim"
                f"{'s' if nv != 1 else ''} tagged [NOT VERIFIED] in the draft."
            )
        elif ins:
            strip_note = (
                f"{ins} generic past-work statement"
                f"{'s' if ins != 1 else ''} recorded as INSUFFICIENT EVIDENCE "
                "(report only; prose not rewritten)."
            )
        else:
            strip_note = (
                "Named past-work claims were checked against retrieved chunks."
            )
        color = "#dc3545" if nv else "#555"
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
            <td style="padding:12px 14px;border-bottom:1px solid #E6E1D6;font-size:13px;color:{color};font-weight:700">{_whole(score_pct)}% match</td>
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
    draft_toc: list[tuple[str, str]] = []

    def section_block(heading: str, content: str, *, internal: bool = False, anchor: str = "") -> str:
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
        if not internal and anchor:
            draft_toc.append((anchor, heading))
        bar = "#8A6A12" if internal else "#C4A35A"
        label = "Internal — not in the Word file" if internal else "Draft section"
        return f"""
        {_named_anchor(anchor)}
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;margin:0 0 28px">
          <tr>
            <td style="width:4px;background:{bar};font-size:0;line-height:0">&nbsp;</td>
            <td style="padding:4px 0 8px 16px">
              <div style="font-size:10px;letter-spacing:0.12em;text-transform:uppercase;
                          color:#8A7A5A;font-weight:600;margin-bottom:4px">{label}</div>
              <h3 style="color:#1F3864;font-size:16px;margin:0 0 12px;font-weight:700">
                {_html(heading)}
              </h3>
              {body}
            </td>
          </tr>
        </table>"""

    proposal_html = ""
    if is_lightweight:
        proposal_html = (
            section_block("Cover Letter", proposal.get("cover_letter", ""), anchor="c-cover")
            + section_block(
                "Executive Summary", proposal.get("executive_summary", ""), anchor="c-exec"
            )
        )
    else:
        for idx, (key, heading, content) in enumerate(iter_client_sections(proposal)):
            proposal_html += section_block(
                heading, content, anchor=f"c-draft-{idx}"
            )

    lock_text = proposal.get("document_lock") or ""
    lock_block = ""
    if isinstance(lock_text, str) and lock_text.strip():
        lock_block = section_block(
            "Assignment lock from the ToR/RFP/REOI (internal — not in the Word file)",
            lock_text,
            internal=True,
        )
    strategy_text = proposal.get("win_strategy") or ""
    strategy_block = ""
    if isinstance(strategy_text, str) and strategy_text.strip():
        strategy_block = section_block(
            "Win strategy (internal — not in the client Word file)",
            strategy_text,
            internal=True,
        )

    if not strengths_html:
        strengths_html = (
            "<li style='margin-bottom:4px'>None recorded in the analysis.</li>"
        )
    if not gaps_html:
        gaps_html = (
            "<li style='margin-bottom:4px'>None recorded in the analysis.</li>"
        )

    named, total_roles, team_gaps = _team_coverage(team_matches)
    nv = _count(grounding.get("not_verified")) if grounding else 0
    ver = _count(grounding.get("verified")) if grounding else 0
    ins = _count(grounding.get("insufficient_evidence")) if grounding else 0

    review_items: list[tuple[str, str]] = []
    if is_lightweight:
        review_items.append((
            "Decide",
            "WATCH preview only. Decide whether to commission a full technical draft.",
        ))
    elif is_eoi:
        review_items.append((
            "Stage",
            "Submit as an Expression of Interest only. Do not attach a financial "
            "proposal unless invited to RFP.",
        ))
    else:
        review_items.append((
            "Stage",
            "Technical file only. Confirm the financial envelope is a separate "
            "submission. Nothing has been sent to the client.",
        ))
    for gap in team_gaps[:4]:
        review_items.append(("Team", gap))
    if nv:
        review_items.append((
            "Claims",
            f"{nv} named past-work claim{'s' if nv != 1 else ''} tagged "
            "[NOT VERIFIED] in the draft. Check each against the corpus "
            "before anything is sent to a client.",
        ))
    improvement = quality.get("one_improvement") if quality else None
    if improvement:
        review_items.append(("Draft", str(improvement)))
    missing = (
        budget.get("missing_inputs")
        if isinstance(budget.get("missing_inputs"), list)
        else []
    )
    client_lines = reviewer_sentences(client_intel)
    if client_lines:
        review_items.append(("Client", client_lines[1] if len(client_lines) > 1 else client_lines[0]))
    for item in missing[:3]:
        review_items.append(("Budget", str(item)))
    omitted = proposal.get("omitted_financial") if isinstance(proposal, dict) else None
    if isinstance(omitted, list) and omitted:
        review_items.append((
            "Format",
            "The ToR listed a separate financial envelope ("
            + "; ".join(str(x) for x in omitted[:4])
            + "). It is not in this technical/EOI file.",
        ))
    format_audit = proposal.get("format_compliance") if isinstance(proposal, dict) else None
    if not isinstance(format_audit, dict) or not format_audit.get("rows"):
        try:
            format_audit = build_format_compliance(
                proposal if isinstance(proposal, dict) else {},
                (proposal or {}).get("submission_outline") or {},
            )
        except Exception:
            format_audit = {}
    if not isinstance(format_audit, dict):
        format_audit = {}
    if format_audit.get("prescribed"):
        review_items.append((
            "Format",
            "Draft follows the ToR written-section list. Confirm page limits, "
            "Gantt if required, and that CVs/forms are attached separately.",
        ))
    attach_bits = []
    for label in (format_audit.get("required_attachments") or [])[:4]:
        attach_bits.append(str(label))
    for label in (format_audit.get("required_forms") or [])[:3]:
        attach_bits.append(str(label))
    if attach_bits:
        review_items.append((
            "Attach",
            "Not drafted as chapters: " + "; ".join(attach_bits),
        ))
    for row in format_audit.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if row.get("status") == "over":
            review_items.append(("Length", str(row.get("note") or "Section over page limit")))
        elif row.get("status") == "under":
            review_items.append(("Length", str(row.get("note") or "Section under page limit")))
        elif row.get("gantt") == "missing":
            review_items.append((
                "Gantt",
                f"{row.get('heading') or 'Work plan'} is missing the required Gantt chart.",
            ))

    score_color = (
        "#28a745" if score >= 70
        else "#f0a500" if score >= 50
        else "#dc3545"
    )
    if total_roles:
        team_value = f"{named}/{total_roles}"
        team_sub = _html("named consultants")
        team_color = "#dc3545" if named < total_roles else "#28a745"
    else:
        team_value = "None"
        team_sub = _html("no consultants matched")
        team_color = "#1F3864"
    if grounding:
        if nv:
            g_label, g_value, g_color = "Claims tagged", str(nv), "#dc3545"
            g_sub = _html(f"{ver} verified · {ins} insufficient")
        else:
            g_label, g_value, g_color = "Claims verified", str(ver), "#28a745"
            g_sub = _html("no named past-work claims tagged [NOT VERIFIED]")
    else:
        g_label, g_value, g_color = "Ceiling (internal)", _html(budget_cap_str), "#1F3864"
        g_sub = "do not copy into the technical/EOI"

    header_title = (
        "Review this EOI before shortlisting"
        if is_eoi
        else "WATCH — decide whether to bid"
        if is_lightweight
        else "Review this technical draft"
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
    attach_note = (
        "The Word file is attached — no fees, rates, or budgets in it."
        if docx_path
        else "No Word attachment was generated this cycle; review the draft below."
    )
    if is_eoi:
        action_copy = (
            "<strong>EOI STAGE:</strong> This document asks for an Expression of "
            "Interest only. The draft below is a complete shortlisting file — not a "
            "full technical/financial proposal. Nothing has been sent to the client. "
            f"{_html(attach_note)}"
        )
        action_bg, action_bar = "#E8F1F8", "#2e86c1"
    elif is_lightweight:
        action_copy = (
            "<strong>WATCH — QUICK FLAG ONLY:</strong> Cover letter plus executive "
            "summary. No full proposal was generated. Decide whether to pursue a "
            "full bid. Nothing has been sent to the client."
        )
        action_bg, action_bar = "#FFF4D6", "#f0a500"
    else:
        action_copy = (
            "<strong>ACTION REQUIRED:</strong> Review the draft, confirm team "
            "availability, verify the internal budget, then approve. "
            "<strong>Nothing has been sent to the client.</strong> "
            f"{_html(attach_note)}"
        )
        action_bg, action_bar = "#FFF4D6", "#C4A35A"
    action_banner = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;margin:0 0 8px;background:{action_bg}">
        <tr>
          <td style="width:4px;background:{action_bar};font-size:0">&nbsp;</td>
          <td style="padding:16px 20px;font-size:14px;line-height:1.55;color:#1C1914">
            {action_copy}
          </td>
        </tr>
        </table>"""
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
    if source_href:
        source_line = (
            f'<a href="{source_href}" style="color:#C4A35A;text-decoration:underline">'
            f"{_html(source_url)}</a>"
        )
    else:
        source_line = "Not provided"
    footer_tor = (
        f'<a href="{source_href}" style="color:#C4A35A">View original TOR</a>'
        if source_href
        else "No TOR link on file"
    )
    stage_label = (
        "Expression of Interest"
        if is_eoi
        else "Quick flag"
        if is_lightweight
        else "Technical proposal"
    )
    deadline_bit = str(deadline)[:10] if deadline else "TBD"
    if is_eoi:
        preheader = (
            f"EOI · {client} · deadline {deadline_bit} · "
            f"{named}/{total_roles or 0} roles named · not sent"
        )
    elif is_lightweight:
        preheader = (
            f"WATCH flag · {client} · deadline {deadline_bit} · score {_whole(score)}/100"
        )
    else:
        preheader = (
            f"Technical draft · {client} · deadline {deadline_bit} · "
            f"score {_whole(score)}/100 · not sent"
        )
    nav_items = [
        ("c-decision", "Decision"),
        ("c-client", "Client"),
        ("c-budget", "Budget"),
        ("c-format", "Format"),
        ("c-team", "Team"),
    ]
    client_history_html = _client_intelligence_html(client_intel)
    relationship_html = _relationship_facts_html(
        _relationship_facts_payload(opportunity_result)
    )
    if relationship_html:
        nav_items.insert(2, ("c-partners", "Partners"))
    if lock_block or strategy_block:
        nav_items.append(("c-strategy", "Strategy"))
    nav_items.append(("c-draft", "Draft"))
    strategy_anchor = (
        _named_anchor("c-strategy") if (lock_block or strategy_block) else ""
    )
    grounding_table = _grounding_table_html(grounding) if grounding else ""
    checklist = _checklist_html(review_items)
    format_block = _format_compliance_html(proposal)
    toc = _draft_toc_html(draft_toc)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>
    <body style="margin:0;padding:0;background:#EDE9E0;color:#1C1914;
                 font-family:Georgia,'Times New Roman',serif;font-size:14px">
    {_preheader_html(preheader)}
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="width:100%;background:#EDE9E0">
    <tr>
      <td align="center" style="padding:28px 12px">
      <!--[if mso]><table role="presentation" width="680" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
      <table role="presentation" width="680" cellpadding="0" cellspacing="0"
             style="width:100%;max-width:680px;background:#ffffff">

    <tr>
      <td style="background:#1F3864;padding:28px 32px 24px">
        <div style="font-size:10px;letter-spacing:0.18em;text-transform:uppercase;
                    color:#C4A35A;font-weight:700;margin-bottom:10px;
                    font-family:Arial,Helvetica,sans-serif">
          Cortech BD Intelligence · {_html(stage_label)} · not sent
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
    <tr><td>{_jump_nav_html(nav_items)}</td></tr>

    <tr>
      <td style="padding:28px 32px 8px">
        <h2 style="margin:0 0 6px;color:#1F3864;font-size:22px;line-height:1.3;
                   font-weight:700">{_html(title)}</h2>
        <p style="margin:0 0 18px;color:#6B6458;font-size:14px;
                  font-family:Arial,Helvetica,sans-serif">
          {_html(client)}
        </p>
        {_kpi_grid([
            _kpi_tile(
                "Deadline",
                _html(str(deadline)[:10] if deadline else "TBD"),
                _html(urgency.get("prefix") or "Review timing"),
                _safe_color(urgency.get("color"), "#1F3864"),
            ),
            _kpi_tile(
                "Fit score",
                f"{_whole(score)}/100",
                _html(recommendation),
                score_color,
            ),
            _kpi_tile(g_label, g_value, g_sub, g_color),
            _kpi_tile("Team", team_value, team_sub, team_color),
            _kpi_tile(
                "Structure",
                "ToR format" if format_audit.get("prescribed") else "House format",
                _html(
                    "written sections from the documents"
                    if format_audit.get("prescribed")
                    else "no format stated in the documents"
                ),
                "#28a745" if format_audit.get("prescribed") else "#1F3864",
            ),
            _kpi_tile(
                "Page / Gantt",
                (
                    "Limits OK"
                    if format_audit.get("page_limits_ok") and format_audit.get("gantt_ok")
                    else "Check draft"
                ),
                _html(
                    "Gantt missing"
                    if not format_audit.get("gantt_ok")
                    else (
                        "page limits vs actual"
                        if format_audit.get("limits_checked")
                        else "no page limits stated"
                    )
                ),
                (
                    "#dc3545"
                    if not format_audit.get("gantt_ok") or not format_audit.get("page_limits_ok")
                    else "#28a745"
                ),
            ),
        ])}
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
        {checklist}

        {client_history_html}

        {relationship_html}

        {format_block}

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

        {_named_anchor("c-team")}
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

        {grounding_table}

        {strategy_anchor}
        {lock_block}

        {strategy_block}

        {_named_anchor("c-draft")}
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="width:100%;margin:12px 0 20px">
          <tr>
            <td style="border-top:2px solid #1F3864;padding-top:20px">
              <div style="font-size:10px;letter-spacing:0.14em;text-transform:uppercase;
                          color:#C4A35A;font-weight:700;margin-bottom:6px">
                Client-facing file · no fees, rates, or budgets
              </div>
              <h2 style="color:#1F3864;margin:0 0 12px;font-size:20px;font-weight:700;
                         font-family:Georgia,'Times New Roman',serif">{draft_heading}</h2>
              {toc}
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
            {footer_tor}
        </p>
      </td>
    </tr>

      </table>
      <!--[if mso]></td></tr></table><![endif]-->
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
            f"Score: {_whole(score)}/100 | WATCH"
        )
    elif is_eoi:
        subject = (
            f"{doc_label}: {_subject_text(title)[:50]} | "
            f"Deadline: {safe_deadline_subject} | "
            f"Score: {_whole(score)}/100 | REVIEW REQUIRED"
        )
    else:
        subject = (
            f"{doc_label}: {_subject_text(title)[:50]} | "
            f"Deadline: {safe_deadline_subject} | "
            f"Score: {_whole(score)}/100 | REVIEW REQUIRED"
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
