"""The pending state: one white plate with the orbiting Cortech logo, and none on search bars.

Static checks on the shipped files. The behaviour (150 ms delay, orbit frames, handoff fade, reduced
motion) is exercised in a real browser by dashboard/design/motion_check.py.
"""

from __future__ import annotations

import re
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
    assert "pending.css" in app and "pending.js" in app, "asset version must cover the new files"


def test_overlay_is_plain_white_with_no_spinner_ring():
    css = _strip(CSS)
    assert re.search(r"\.pend \{[^}]*background:\s*#ffffff", css)
    assert "border-top" not in css and "conic-gradient" not in css and "spinner" not in css


def test_orbit_is_a_fast_linear_transform_only_loop():
    css = _strip(CSS)
    assert css.count("0.8s linear infinite") == 2
    props = set(re.findall(r"([a-z-]+):", " ".join(re.findall(r"@keyframes pend-[a-z]+ \{(.*?\}.*?)\}", css, flags=re.S))))
    assert props <= {"transform"}, props
    radius = int(re.search(r"translateX\((\d+)px\)", css).group(1))
    assert 18 <= radius <= 28


def test_reduced_motion_stops_the_orbit():
    css = _strip(CSS)
    block = css[css.index("prefers-reduced-motion"):]
    assert "animation: none" in block and "transform: none" in block


def test_logo_is_the_unaltered_official_file_and_at_most_150px_wide():
    assert "brand/cortech-logo.png" in JS
    widths = [int(w) for w in re.findall(r"width:\s*(\d+)px", _strip(CSS)) if int(w) > 3]
    assert widths and max(widths) <= 150
    logo_rule = re.search(r"\.pend__logo \{(.*?)\}", _strip(CSS), flags=re.S).group(1)
    assert "border-radius: 9px" in logo_rule  # the same plate treatment as .brand-logo in glass.css
    for banned in ("filter", "opacity", "object-fit", "mix-blend", "clip", "mask"):
        assert banned not in logo_rule, banned


def test_accessible_status_label_and_hard_failsafes():
    assert '"role", "status"' in JS and '"aria-live", "polite"' in JS and '"Loading"' in JS
    assert re.search(r"DELAY\s*=\s*150\b", JS)
    assert re.search(r"FAILSAFE\s*=\s*30000\b", JS)
    assert "pageshow" in JS and '"error"' in JS and "Escape" in JS
    assert "persisted" in JS


def test_handoff_fades_out_in_200_to_300ms():
    css = _strip(CSS)
    ms = float(re.search(r"\.pend\.is-off \{[^}]*transition: opacity ([\d.]+)s", css).group(1)) * 1000
    assert 200 <= ms <= 300


def test_search_inputs_never_trigger_or_show_a_pending_state():
    assert '"input"' not in JS and '"keyup"' not in JS and '"keypress"' not in JS
    assert 'f.querySelector(\'input[type="search"]\')' in JS and 'role") === "search"' in JS  # search forms stay silent
    queue = (TEMPLATES / "queue.html").read_text()
    assert "url-status-pill" not in queue
    for text in re.findall(r"Starting…|Uploading…|Running…|File selected", queue):
        pytest.fail(f"busy text left in queue.html: {text}")
    lib = (STATIC / "library.js").read_text()
    assert "Uploading" not in lib
    assert "intake-status" not in (STATIC / "app.css").read_text()
    for name in ("queue.html", "library.html", "track_record.html"):
        for tag in re.findall(r"<input[^>]*type=\"search\"[^>]*>", (TEMPLATES / name).read_text()):
            assert "aria-busy" not in tag and "data-pending" not in tag


def test_busy_states_use_the_shared_component():
    lib = (STATIC / "library.js").read_text()
    assert "CortechPending" in lib and "scope:" in lib
    assert "CortechPending" in (TEMPLATES / "base.html").read_text()
    assert "CortechPending" in (TEMPLATES / "job.html").read_text()
    assert "CortechPending" in (TEMPLATES / "queue.html").read_text() or "triggerSubmit" in (TEMPLATES / "queue.html").read_text()


def test_status_chips_and_outcome_labels_are_data_and_unchanged():
    lib = (TEMPLATES / "library.html").read_text()
    assert re.search(r"[Pp]ending", lib), "the Won/Lost/Pending outcome control keeps its wording"
    css = (STATIC / "app.css").read_text()
    assert ".state--pending" in css and ".state--processing" in css


def test_no_em_dashes_or_emoji_in_the_pending_files():
    for f in (STATIC / "pending.css", STATIC / "pending.js"):
        text = f.read_text()
        assert "\u2014" not in text and "\u2013" not in text, f
        assert not re.search("[\U0001F300-\U0001FAFF\u2600-\u27BF]", text), f
