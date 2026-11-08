"""The shared pending state uses the authentic logo and reports real states."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

STATIC = Path("dashboard/static")
TEMPLATES = Path("dashboard/templates")
CSS = (STATIC / "pending.css").read_text()
JS = (STATIC / "pending.js").read_text()


def _strip(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_pending_assets_are_shipped_and_every_page_loads_them():
    for name in ("base.html", "login.html", "denied.html"):
        text = (TEMPLATES / name).read_text()
        assert "pending.css" in text and "pending.js" in text, name
    app = Path("dashboard/app.py").read_text()
    assert "pending.css" in app and "pending.js" in app


def test_pending_surface_matches_dark_emerald_design_tokens():
    css = _strip(CSS)
    assert "background: rgba(5, 11, 8, .84)" in css
    assert "linear-gradient(145deg, #18221d, #111914)" in css
    assert "var(--line" in css and "rgba(53, 207, 134" in css
    assert "background: #ffffff" not in css
    assert "conic-gradient" not in css and "spinner" not in css


def test_logo_uses_authentic_asset_without_distorting_its_aspect_ratio():
    assert "brand/cortech-logo.png" in JS
    assert "brand/cortech-logo.png" in (TEMPLATES / "_macros.html").read_text()
    logo_rule = re.search(r"\.pend__logo \{(.*?)\}", _strip(CSS), flags=re.S).group(1)
    assert "width: 132px" in logo_rule and "height: 49px" in logo_rule
    for banned in ("filter", "object-fit", "mix-blend", "clip", "mask"):
        assert banned not in logo_rule


def test_double_beat_uses_only_subtle_transform_and_opacity():
    css = _strip(CSS)
    assert ".pend.is-on .pend__logo" in css
    assert "animation: pend-beat 1.7s ease-in-out infinite" in css
    beat = re.search(r"@keyframes pend-beat \{(.*?)@keyframes", css, flags=re.S).group(1)
    assert "6%" in beat and "12%" in beat and "20%" in beat
    props = set(re.findall(r"([a-z-]+):", beat))
    assert props == {"transform", "opacity"}
    assert "animation: pend-halo 1.7s ease-in-out infinite" in css


def test_reduced_motion_keeps_static_logo_and_status():
    css = _strip(CSS)
    block = css[css.index("prefers-reduced-motion") :]
    assert "animation: none !important" in block
    assert "transform: none" in block and "opacity: 1" in block


def test_status_accessibility_and_failsafes_are_preserved():
    assert 'setAttribute("role", "status")' in JS
    assert 'setAttribute("aria-live", "polite")' in JS
    assert 'setAttribute("aria-atomic", "true")' in JS
    assert 'setAttribute("aria-busy", "true")' in JS
    assert re.search(r"DELAY\s*=\s*150\b", JS)
    assert re.search(r"FAILSAFE\s*=\s*30000\b", JS)
    assert "pageshow" in JS and '"error"' in JS and "Escape" in JS
    assert 'textContent = label || "Opening page"' in JS
    assert 'data-pending-label") || "Submitting request"' in JS


def test_handoff_fades_out_without_lingering_busy_state():
    css = _strip(CSS)
    assert re.search(r"\.pend\.is-off \{[^}]*transition: opacity \.22s ease", css)
    assert "_busyTarget.removeAttribute(\"aria-busy\")" in JS
    assert "function unmount(node)" in JS and "function hide(instant)" in JS


def test_search_inputs_do_not_trigger_pending_and_existing_actions_remain():
    assert 'f.querySelector(\'input[type="search"]\')' in JS
    assert 'f.getAttribute("role") === "search"' in JS
    assert "data-no-pending" in JS and 'hasAttribute("download")' in JS
    for name in ("queue.html", "library.html", "track_record.html"):
        text = (TEMPLATES / name).read_text()
        for tag in re.findall(r"<input[^>]*type=\"search\"[^>]*>", text):
            assert "aria-busy" not in tag and "data-pending" not in tag
    job = (TEMPLATES / "job.html").read_text()
    assert 'name="csrf_token"' in job and "Cancel this run" in job
    lib = (STATIC / "library.js").read_text()
    assert 'label: "Uploading files to the library"' in lib
    assert "P.hide(true)" in lib


def test_server_rendered_pending_states_distinguish_queued_running_and_terminal():
    app = (Path("dashboard/app.py").read_text())
    job = (TEMPLATES / "job.html").read_text()
    assert "def _job_pending_view" in app
    assert '"state": "queued"' in app and '"state": "processing"' in app and '"state": "stale"' in app
    assert "m.pending_state(pending_view.state" in job
    assert "aria-busy=\"true\"" in job
    assert "m.pending_state(\"processing\"" in (TEMPLATES / "detail.html").read_text()
    assert "under a minute" not in (TEMPLATES / "detail.html").read_text()


def test_job_pending_state_tracks_real_heartbeat_freshness():
    from dashboard.app import _job_pending_view

    now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
    queued = _job_pending_view({"status": "queued"}, now=now)
    assert queued["state"] == "queued"
    recent = _job_pending_view(
        {"status": "running", "last_heartbeat_at": (now - timedelta(seconds=5)).isoformat()}, now=now
    )
    assert recent["state"] == "processing"
    waiting = _job_pending_view(
        {"status": "running", "started_at": (now - timedelta(seconds=5)).isoformat(), "last_heartbeat_at": None}, now=now
    )
    assert waiting["state"] == "waiting" and "not confirmed" in waiting["detail"]
    stale = _job_pending_view(
        {"status": "running", "last_heartbeat_at": (now - timedelta(minutes=2)).isoformat()}, now=now
    )
    assert stale["state"] == "stale"
    assert "not confirmed" in stale["detail"]
    assert _job_pending_view({"status": "failed"}, now=now) is None
    assert _job_pending_view({"status": "succeeded"}, now=now) is None


def test_pending_files_have_no_em_dash_or_emoji():
    for f in (STATIC / "pending.css", STATIC / "pending.js"):
        text = f.read_text()
        assert "\u2014" not in text and "\u2013" not in text
        assert not re.search("[\U0001F300-\U0001FAFF\u2600-\u27BF]", text)
