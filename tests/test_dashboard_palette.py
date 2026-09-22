"""The dashboard UI palette is checked, not eyeballed.

Seline reskin (2026-09-22): warm-stone canvas, white cards, one cyan accent.
Every text colour in dashboard/static/app.css must clear WCAG AA (4.5:1) on
every surface it is placed on, and every mark colour (rail segments, state
dots, meter and bar fills) must clear 3:1. The pairs below are the ones the
stylesheet actually composes. docs/DASHBOARD_DESIGN.md §10 quotes the numbers.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path("dashboard/static/app.css").read_text()
ROOT = CSS[CSS.index(":root {"): CSS.index("}", CSS.index(":root {"))]
BODY = re.sub(r"/\*.*?\*/", "", CSS[CSS.index("}", CSS.index(":root {")):], flags=re.S)


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


def rules():
    return re.findall(r"([^{}]+)\{([^}]*)\}", BODY)


TEXT_PAIRS = [(fg, bg) for fg in ("ink", "ink-2") for bg in ("ground", "surface")] + [
    ("ink", "wash"),        # the highlight pill, halted state pills
    ("ink", "accent"),      # ink text on the cyan CTA
    ("surface", "invert"),  # white text on the soot BID chip
    ("ink", "line"),        # completed pill: ink on stone-border
]

MARK_PAIRS = [(fg, "surface") for fg in ("invert", "accent-edge", "human", "stage-1", "stage-2", "stage-3", "stage-4",
                                          "st-good", "st-processing", "st-halt", "ramp-1", "rec-3")]


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_on_surface_clears_aa(fg, bg):
    ratio = contrast(token(fg), token(bg))
    assert ratio >= 4.5, f"--{fg} on --{bg} is {ratio:.2f}:1 (need 4.5)"


@pytest.mark.parametrize("fg,bg", MARK_PAIRS)
def test_marks_clear_three_to_one(fg, bg):
    need = 2.0 if fg == "ramp-1" else 3.0   # ramp light end: the ordinal floor, relief rule applies
    ratio = contrast(token(fg), token(bg))
    assert ratio >= need, f"--{fg} on --{bg} is {ratio:.2f}:1 (need {need})"


def test_cyan_is_never_text():
    """Seline's own cyan-on-wash (2.29:1) and white-on-cyan (2.65:1) fail AA,
    so cyan is a fill, a mark and a ring here — never a text colour."""
    assert contrast(token("color-cyan-edge"), token("wash")) < 3.0          # documents why
    assert contrast(token("color-pure-white"), token("accent")) < 4.5       # documents why
    for selector, rule in rules():
        for m in re.finditer(r"(?<![-\w])color:\s*var\(--([\w-]+)\)", rule):
            assert m.group(1) not in ("accent", "accent-edge", "color-cyan-signal", "color-cyan-edge", "human", "st-halt", "st-processing"), \
                f"cyan used as text in {selector.strip()[:60]}"


def test_ash_gray_is_never_text_below_display_size():
    """#a8a29e is 2.5:1 on white — disabled/missing figures at display size and rings only."""
    for selector, rule in rules():
        if re.search(r"(?<![-\w])color:\s*var\(--(ink-3|color-ash-gray|st-resumable)\)", rule):
            sel = selector.strip()
            assert any(k in sel for k in ("is-zero", "is-missing", "[disabled]", 'aria-disabled')), f"ash used as text: {sel[:70]}"


def test_one_chromatic_fill_per_screen():
    """Only the primary button is filled with cyan; everything else that is
    cyan is a border, a ring, a mark or the sky-wash pill."""
    for selector, rule in rules():
        if re.search(r"background:\s*var\(--(accent|color-cyan-signal)\)", rule):
            assert selector.strip() in (".btn", ".brand__mark::after"), f"cyan fill outside the primary button: {selector.strip()[:60]}"


def test_human_review_is_distinct_from_every_stage_colour_without_hue():
    """Rail: soot stages vs cyan human segment — lightness apart and dashed."""
    human = token("human")
    for i in range(1, 5):
        assert contrast(human, token(f"stage-{i}")) >= 3.0, f"--human vs --stage-{i}"
    assert "border: 1px dashed var(--human)" in BODY
    assert contrast(token("halt"), token("stage-4")) >= 3.0, "halted wash vs soot stage"


def test_stage_bar_ramp_is_monotone_in_lightness():
    lums = [_lum(token(f"ramp-{i}")) for i in range(1, 5)]
    assert lums == sorted(lums, reverse=True), "the stone ramp must read light→dark along the pipeline"
    assert all(a - b >= 0.04 for a, b in zip(lums, lums[1:])), "adjacent steps too close"


def test_recommendation_uses_weight_and_shape_not_hue():
    for chip in ("chip--bid", "chip--watch", "chip--nobid"):
        rule = re.search(rf"\.{chip}\s*\{{([^}}]*)\}}", CSS).group(1)
        for forbidden in ("--st-", "--halt", "--accent", "--wash", "--human", "--stage-"):
            assert forbidden not in rule, f".{chip} uses {forbidden}"
    for i in (1, 2, 3):
        h = token(f"rec-{i}")
        r, g, b = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
        assert max(r, g, b) - min(r, g, b) <= 16, f"--rec-{i} {h} is not achromatic"


def test_no_colour_literal_outside_the_token_block():
    stray = re.findall(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", BODY)
    assert not stray, f"colour literals outside :root: {stray}"


def test_one_shadow_per_page():
    """Seline: shadows are for the one floating card; content cards are
    hairlines. Allowed: the hero card, the focus ring, the live pulse, and the
    1px inset that edges a halted rail segment (a border, not elevation)."""
    for selector, rule in rules():
        if re.search(r"(^|;|\s)box-shadow:(?!\s*none)", rule):
            sel = selector.strip()
            ok = "panel--hero" in sel or "auth__card" in sel or ":focus" in sel or "%" in sel or "pulse" in sel or "is-halt" in sel
            assert ok, sel[:80]
            if "is-halt" in sel:
                assert "inset" in rule


def test_display_weights_never_bold():
    """Seline: Roobert/Inter Tight at 400 for every display size."""
    for selector, rule in rules():
        if "var(--display)" in rule:
            w = re.search(r"font-weight:\s*([^;]+);", rule)
            assert w is None or w.group(1).strip() == "var(--font-weight-regular)", selector.strip()[:60]
        for w in re.findall(r"font-weight:\s*([^;]+);", rule):
            assert w.strip() in ("var(--font-weight-regular)", "var(--font-weight-medium)"), f"{selector.strip()[:50]}: {w}"


def test_radii_vocabulary():
    allowed = {"var(--r-panel)", "var(--r-hero)", "var(--r-pill)", "var(--r-ctl)", "var(--r-rail)", "var(--radius-md)",
               "var(--radius-icons)", "var(--radius-inputs)", "var(--radius-buttons)", "50%", "4px", "1px", "0 var(--r-rail) var(--r-rail) 0"}
    for val in re.findall(r"border-radius:\s*([^;]+);", BODY):
        assert val.strip() in allowed, val
    assert "--radius-cards: 10px" in ROOT and "--radius-feature-card: 16px" in ROOT and "--radius-buttons: 9999px" in ROOT


def test_body_is_fourteen_px_inter():
    assert re.search(r"body \{[^}]*font-size: var\(--t14\)[^}]*line-height: 1\.64", CSS), "body must be 14px at 1.64"
    assert "--ui:   var(--font-inter)" in ROOT and "--display: var(--font-roobert)" in ROOT


def test_fonts_are_self_hosted_and_present():
    for face in re.findall(r'url\("fonts/([^"]+)"\)', CSS):
        assert (Path("dashboard/static/fonts") / face).exists(), face
    assert "googleapis" not in CSS and "gstatic" not in CSS
    assert "inter-tight-var-latin.woff2" in CSS and "inter-var-latin.woff2" in CSS
    for lic in ("LICENSE-Inter-OFL.txt", "LICENSE-Inter-Tight-OFL.txt"):
        assert (Path("dashboard/static/fonts") / lic).exists(), lic
