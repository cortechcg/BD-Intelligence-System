"""The dashboard UI palette is checked, not eyeballed.

Auros reskin (2026-09-22): a dark-on-dark system. Every text colour used in
dashboard/static/app.css must clear WCAG AA (4.5:1) on every surface it is
placed on, and every mark colour (rail segments, state dots, meter and bar
fills) must clear 3:1. The pairs below are the ones the stylesheet actually
composes; if a token is retuned, this test says whether it still reads.
docs/DASHBOARD_DESIGN.md §9 quotes the same numbers.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path("dashboard/static/app.css").read_text()
ROOT = CSS[CSS.index(":root {"): CSS.index("}", CSS.index(":root {"))]
BODY = CSS[CSS.index("}", CSS.index(":root {")):]


def token(name: str, _depth: int = 0) -> str:
    """Resolve --name to a hex, following var(--alias) chains."""
    m = re.search(rf"--{re.escape(name)}:\s*([^;]+);", ROOT)
    assert m, f"--{name} is not defined in :root"
    val = m.group(1).strip()
    if val.startswith("var(--"):
        assert _depth < 5, f"--{name}: alias chain too deep"
        return token(val[6:-1], _depth + 1)
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", val), f"--{name} is not a hex colour: {val}"
    return val


def _lum(h: str) -> float:
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


SURFACES = ("ground", "surface", "recess")

TEXT_PAIRS = [(fg, bg) for fg in ("ink", "ink-2", "ink-em", "stat", "halt", "aqua") for bg in SURFACES] + [
    ("ground", "color-aurora-gradient"),      # abyss text on the aurora gradient's cyan end (button)
    ("ground", "halt"),                       # abyss text on the aurora gradient's pink end (button)
    ("ground", "rec-1"),                      # abyss text on the filled BID chip
]

MARK_PAIRS = [(fg, bg) for fg in ("teal", "aqua", "stage-1", "stage-2", "stage-3", "stage-4", "human", "halt",
                                   "st-good", "st-processing", "st-halt", "st-resumable") for bg in ("surface", "ground")]


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_on_surface_clears_aa(fg, bg):
    ratio = contrast(token(fg), token(bg))
    assert ratio >= 4.5, f"--{fg} on --{bg} is {ratio:.2f}:1 (need 4.5)"


@pytest.mark.parametrize("fg,bg", MARK_PAIRS)
def test_marks_clear_three_to_one(fg, bg):
    ratio = contrast(token(fg), token(bg))
    assert ratio >= 3.0, f"--{fg} on --{bg} is {ratio:.2f}:1 (need 3.0)"


def test_slate_deep_is_never_used_as_text():
    """#707777 is 2.9:1 on kelp — a surface tint and ring colour only."""
    assert contrast(token("color-slate-deep"), token("surface")) < 4.5  # documents why
    for rule in re.findall(r"\{[^}]*\}", BODY):
        if re.search(r"(?<![-\w])color:\s*var\(--(color-slate-deep|st-pending|rec-3)\)", rule):
            pytest.fail(f"slate-deep used as text: {rule.strip()[:80]}")


def test_lavender_phosphor_is_used_only_for_statistics():
    """The reference: large statistics and emphasis figures only — never body
    text, never a UI control. Every rule that paints with --stat must be one
    of the statistic value classes."""
    allowed = {".figure__value", ".hero__value", ".stat__value", ".tile__value", ".meter__value"}
    for selector, rule in re.findall(r"([^{}]+)\{([^}]*)\}", BODY):
        if "var(--stat)" in rule or "var(--color-lavender-phosphor)" in rule:
            sel = selector.strip()
            assert sel in allowed, f"lavender-phosphor used outside the statistic classes: {sel}"
    assert "var(--color-lavender-phosphor)" not in BODY.replace("var(--stat)", "")


def test_human_review_is_distinct_from_every_stage_colour_without_hue():
    """The rail's human segment (white, dashed) must be told from the four agent
    stages by lightness alone, and by shape — the brass/navy pair had both."""
    human = token("human")
    for i in range(1, 5):
        ratio = contrast(human, token(f"stage-{i}"))
        assert ratio >= 1.3, f"--human vs --stage-{i} is only {ratio:.2f}:1 in lightness"
    assert "border: 1px dashed var(--human)" in BODY, "the human segment must also differ by shape"
    # and the stopped colour is told from the human colour by shape (solid fill) and label
    assert contrast(token("halt"), token("stage-3")) >= 1.2


def test_stage_ramp_is_monotone_in_lightness():
    lums = [_lum(token(f"stage-{i}")) for i in range(1, 5)]
    assert lums == sorted(lums), "the rail ramp must read dark→light along the pipeline"
    assert all(b - a >= 0.05 for a, b in zip(lums, lums[1:])), "adjacent rail steps too close"


def test_recommendation_uses_weight_and_shape_not_hue():
    """BID/WATCH/NO-BID chips must not borrow a machine-state or halt colour."""
    for chip in ("chip--bid", "chip--watch", "chip--nobid"):
        rule = re.search(rf"\.{chip}\s*\{{([^}}]*)\}}", CSS).group(1)
        for forbidden in ("--st-", "--halt", "--teal", "--aqua", "--stage-", "--stat"):
            assert forbidden not in rule, f".{chip} uses {forbidden}"
    for i in (1, 2, 3):
        h = token(f"rec-{i}")
        r, g, b = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
        assert max(r, g, b) - min(r, g, b) <= 16, f"--rec-{i} {h} is not achromatic"


def test_no_colour_literal_outside_the_token_block():
    """Every colour is a token: no hex or rgb() past :root."""
    stray = re.findall(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", BODY)
    assert not stray, f"colour literals outside :root: {stray}"


def test_no_box_shadow_for_elevation():
    """Auros: depth is the teal surface stack, never a shadow. The only
    box-shadow allowed is the focus ring and the live pulse."""
    for selector, rule in re.findall(r"([^{}]+)\{([^}]*)\}", BODY):
        if re.search(r"(^|;|\s)box-shadow:", rule):
            assert ":focus" in selector or "pulse" in selector or "%" in selector, selector.strip()


def test_radii_are_16_and_6_except_thin_marks():
    for val in re.findall(r"border-radius:\s*([^;]+);", BODY):
        v = val.strip()
        assert v in ("var(--r-panel)", "var(--r-ctl)", "var(--r-rail)", "var(--radius-small)",
                     "var(--radius-buttons)", "50%", "4px", "1px", "0 var(--r-rail) var(--r-rail) 0"), v
    assert "--r-panel: var(--radius-cards)" in ROOT and "--radius-cards: 16px" in ROOT
    assert "--r-ctl: var(--radius-small)" in ROOT and "--radius-small: 6px" in ROOT


def test_display_weights_are_medium_only():
    """Auros: headings and figures at weight 500 — no bold, no light."""
    for selector, rule in re.findall(r"([^{}]+)\{([^}]*)\}", BODY):
        for w in re.findall(r"font-weight:\s*([^;]+);", rule):
            w = w.strip()
            assert w in ("var(--font-weight-regular)", "var(--font-weight-medium)"), f"{selector.strip()}: {w}"


def test_fonts_are_self_hosted_and_present():
    for face in re.findall(r'url\("fonts/([^"]+)"\)', CSS):
        assert (Path("dashboard/static/fonts") / face).exists(), face
    assert "googleapis" not in CSS and "gstatic" not in CSS
    assert "dm-sans-var-latin.woff2" in CSS, "DM Sans is the Matter substitute"
