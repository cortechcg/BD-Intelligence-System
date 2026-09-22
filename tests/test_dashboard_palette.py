"""The dashboard UI palette is checked, not eyeballed.

Every text colour used in dashboard/static/app.css must clear WCAG AA (4.5:1)
on every surface it is placed on, and every mark colour (rail segments, state
dots, meter fills) must clear 3:1. The pairs below are the ones the stylesheet
actually composes; if a token is retuned, this test says whether it still
reads. docs/DASHBOARD_DESIGN.md §3 quotes the same numbers.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path("dashboard/static/app.css").read_text()


def token(name: str) -> str:
    m = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", CSS)
    assert m, f"--{name} is not defined in app.css"
    return m.group(1)


def _lum(h: str) -> float:
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [
    # (foreground token, background token)
    ("ink", "ground"), ("ink", "surface"), ("ink", "navy-tint"), ("ink", "brass-tint"),
    ("ink", "good-tint"), ("ink", "warn-tint"), ("ink", "serious-tint"), ("ink", "critical-tint"),
    ("ink-2", "ground"), ("ink-2", "surface"), ("ink-2", "navy-tint"), ("ink-2", "brass-tint"),
    ("ink-2", "warn-tint"), ("ink-2", "critical-tint"),
    ("navy", "ground"), ("navy", "surface"), ("navy", "navy-tint"),
    ("brass-ink", "ground"), ("brass-ink", "surface"), ("brass-ink", "brass-tint"),
    ("good-ink", "surface"), ("good-ink", "good-tint"),
    ("warn-ink", "surface"), ("warn-ink", "warn-tint"), ("warn-ink", "ground"),
    ("serious-ink", "surface"), ("serious-ink", "serious-tint"), ("serious-ink", "ground"),
    ("critical-ink", "surface"), ("critical-ink", "critical-tint"), ("critical-ink", "ground"),
    ("navy-soft", "surface"),
]

MARK_PAIRS = [
    ("navy", "surface"), ("navy", "ground"), ("navy-soft", "surface"),
    ("brass-ink", "surface"), ("good-ink", "surface"), ("warn-ink", "surface"),
    ("serious-ink", "surface"), ("critical-ink", "surface"),
    ("stage-1", "surface"), ("rec-1", "surface"),
]


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_on_surface_clears_aa(fg, bg):
    ratio = contrast(token(fg), token(bg))
    assert ratio >= 4.5, f"--{fg} on --{bg} is {ratio:.2f}:1 (need 4.5)"


@pytest.mark.parametrize("fg,bg", MARK_PAIRS)
def test_marks_clear_three_to_one(fg, bg):
    ratio = contrast(token(fg), token(bg))
    assert ratio >= 3.0, f"--{fg} on --{bg} is {ratio:.2f}:1 (need 3.0)"


def test_white_on_primary_button_clears_aa():
    assert contrast("#FFFFFF", token("navy")) >= 4.5
    assert contrast("#FFFFFF", token("navy-hover")) >= 4.5


def test_no_hex_colour_outside_the_token_block():
    """Every colour is a token: no ad hoc hex past :root (the one allowed
    literal is the white on the primary button and the BID chip)."""
    root_end = CSS.index("}", CSS.index(":root {"))
    body = CSS[root_end:]
    stray = [h for h in re.findall(r"#[0-9a-fA-F]{3,6}\b", body) if h.lower() not in ("#fff", "#ffffff")]
    assert not stray, f"hex colours outside :root: {stray}"


def test_fonts_are_self_hosted_and_present():
    for face in re.findall(r'url\("fonts/([^"]+)"\)', CSS):
        assert (Path("dashboard/static/fonts") / face).exists(), face
    assert "googleapis" not in CSS and "http" not in CSS.split("@font-face")[1][:400]


def test_recommendation_uses_weight_and_shape_not_hue():
    """BID/WATCH/NO-BID chips must not borrow a machine-state colour."""
    for chip in ("chip--bid", "chip--watch", "chip--nobid"):
        rule = re.search(rf"\.{chip}\s*\{{([^}}]*)\}}", CSS).group(1)
        for state in ("good", "warn", "serious", "critical"):
            assert f"--{state}-" not in rule, f".{chip} uses a status colour"
