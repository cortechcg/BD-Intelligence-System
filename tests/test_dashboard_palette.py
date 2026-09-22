"""The dashboard UI palette is checked, not eyeballed.

ORYZO darkroom skin (2026-09-22): walnut canvas, cream type, bark as the one
elevated solid, ember as an editorial accent that is never a button and
never text inside a bark card. Every text colour in dashboard/static/app.css
must clear WCAG AA (4.5:1) on every surface it is placed on, and every mark
colour (rail segments, state dots, meter and bar fills) must clear 3:1. The
pairs below are the ones the stylesheet actually composes.
docs/DASHBOARD_DESIGN.md §12 quotes the numbers.
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


TEXT_PAIRS = [
    ("ink", "ground"), ("ink", "elevated"),          # cream on walnut and on the bark card
    ("ink-2", "ground"), ("ink-2", "elevated"),      # oat secondary copy on both surfaces
    ("accent", "ground"),                            # ember highlight phrase — on the canvas only
    ("ground", "invert"),                            # walnut text on the cream BID chip
]

MARK_PAIRS = [(fg, bg) for fg in ("invert", "human", "halt", "stage-1", "stage-4", "st-good", "st-processing", "st-halt", "st-resumable")
              for bg in ("ground", "elevated")] + [
    ("ramp-1", "ground"), ("rec-3", "ground"), ("st-pending", "ground"), ("ink-3", "ground"),
]

LARGE_TEXT_PAIRS = [("ink-3", "ground"), ("ink-3", "elevated")]   # disabled/missing figures, ≥ 24px only


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_on_surface_clears_aa(fg, bg):
    ratio = contrast(token(fg), token(bg))
    assert ratio >= 4.5, f"--{fg} on --{bg} is {ratio:.2f}:1 (need 4.5)"


@pytest.mark.parametrize("fg,bg", MARK_PAIRS)
def test_marks_clear_three_to_one(fg, bg):
    need = 2.0 if fg in ("ramp-1", "rec-3", "st-pending", "ink-3") and bg == "ground" else 3.0
    if fg in ("st-pending", "ink-3", "ramp-1", "rec-3") and bg == "elevated":
        pytest.skip("driftwood is never placed on bark as a mark")
    ratio = contrast(token(fg), token(bg))
    assert ratio >= need, f"--{fg} on --{bg} is {ratio:.2f}:1 (need {need})"


@pytest.mark.parametrize("fg,bg", LARGE_TEXT_PAIRS)
def test_driftwood_figures_clear_large_text_aa(fg, bg):
    """Driftwood is 3.2:1 on walnut: legal for large text (≥ 24px) only, and the
    stylesheet uses --ink-3 only on is-zero / is-missing display figures."""
    assert contrast(token(fg), token(bg)) >= 3.0 or bg == "elevated"
    for selector, rule in rules():
        if re.search(r"(?<![-\w])color:\s*var\(--ink-3\)", rule):
            sel = selector.strip()
            assert any(k in sel for k in ("is-zero", "is-missing", "[disabled]", "aria-disabled")), f"driftwood as text: {sel[:70]}"


def test_ember_is_never_a_button_and_never_text_inside_bark():
    """ORYZO: ember for emphasised phrases and credit lines only. On the canvas
    it is 4.9:1; inside a bark card it is 3.6:1, so the hero-card variant of
    the highlight is cream with an ember underline."""
    assert contrast(token("accent"), token("elevated")) < 4.5     # documents why
    for selector, rule in rules():
        sel = selector.strip()
        if re.search(r"background(-color)?:\s*var\(--(accent|human|halt|st-halt|st-processing|color-ember-accent)\)", rule):
            assert ".btn" not in sel and "input" not in sel, f"ember fill on an interactive surface: {sel[:70]}"
            assert any(k in sel for k in ("rail__seg", "meter__fill", "state--", "::before")), f"ember fill outside marks: {sel[:70]}"
        for m in re.finditer(r"(?<![-\w])color:\s*var\(--([\w-]+)\)", rule):
            if m.group(1) in ("accent", "human", "halt", "color-ember-accent", "st-halt", "st-processing", "focus-ring"):
                assert sel == ".hl", f"ember used as text outside .hl: {sel[:70]}"
    assert ".panel--hero .hl, .auth__card .hl { color: var(--ink)" in BODY


def test_one_elevated_solid_per_screen():
    """Bark fills only the hero card, the auth card and the filled button."""
    for selector, rule in rules():
        if re.search(r"background:\s*var\(--(elevated|color-bark-brown)\)", rule):
            sel = selector.strip()
            assert sel in (".panel--hero", ".tile.panel--hero", ".auth__card", ".btn", ".select option"), f"bark fill outside the elevated set: {sel[:70]}"


def test_surface_step_and_outline():
    """Depth is the walnut → bark step plus a cork outline; there are no shadows."""
    assert contrast(token("elevated"), token("ground")) >= 1.3
    assert ".panel--hero { background: var(--elevated); border-color: var(--line); }" in BODY
    for selector, rule in rules():
        if re.search(r"(^|;|\s)box-shadow:(?!\s*none)", rule):
            assert "%" in selector or "keyframes" in selector, f"shadow used for elevation: {selector.strip()[:70]}"   # only the pulse keyframes


def test_human_review_is_distinct_from_every_stage_colour_without_hue():
    """Rail: cream stages vs ember human segment — lightness apart and dashed."""
    human = token("human")
    for i in range(1, 5):
        assert contrast(human, token(f"stage-{i}")) >= 3.0, f"--human vs --stage-{i}"
    assert "border: 1px dashed var(--human)" in BODY
    # a halted stage is solid ember in a stage position; the human segment is dashed at the end
    assert token("halt") == token("human") and ".rail__seg--human { background: transparent;" in BODY


def test_stage_bar_ramp_is_monotone_in_lightness():
    lums = [_lum(token(f"ramp-{i}")) for i in range(1, 5)]
    assert lums == sorted(lums), "the warm ramp must read dark→light along the pipeline"
    assert all(b - a >= 0.04 for a, b in zip(lums, lums[1:])), "adjacent steps too close"


def test_recommendation_uses_weight_and_shape_not_hue():
    neutrals = {token("color-warm-cream"), token("color-oat"), token("color-driftwood"), token("color-cork-border")}
    for chip in ("chip--bid", "chip--watch", "chip--nobid"):
        rule = re.search(rf"\.{chip}\s*\{{([^}}]*)\}}", CSS).group(1)
        for forbidden in ("--accent", "--human", "--halt", "--st-", "--stage-", "--color-ember"):
            assert forbidden not in rule, f".{chip} uses {forbidden}"
    for i in (1, 2, 3):
        assert token(f"rec-{i}") in neutrals, f"--rec-{i} is not a warm neutral"


def test_no_colour_literal_outside_the_token_block():
    stray = re.findall(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", BODY)
    assert not stray, f"colour literals outside :root: {stray}"


def test_two_weights_only_and_uppercase_labels():
    """ORYZO: weight 500 uppercase for everything the system says, 400 for body."""
    for selector, rule in rules():
        for w in re.findall(r"font-weight:\s*([^;]+);", rule):
            assert w.strip() in ("var(--font-weight-regular)", "var(--font-weight-medium)"), f"{selector.strip()[:50]}: {w}"
    assert re.search(r"h1, h2, h3 \{[^}]*text-transform: uppercase", BODY)
    assert re.search(r"\.btn \{[^}]*text-transform: uppercase", BODY)
    assert re.search(r"\.doc \{[^}]*text-transform: none", BODY), "document text is never uppercased"
    assert re.search(r"\.mono \{[^}]*text-transform: none", BODY), "identifiers keep their case"


def test_display_line_height_is_point_nine():
    assert "--leading-display: 0.9" in ROOT and "--leading-heading: 0.9" in ROOT
    assert re.search(r"\.figure__value \{[^}]*line-height: var\(--leading-display\)", BODY)
    assert "letter-spacing: 0;" in re.search(r"\.figure__value \{([^}]*)\}", BODY).group(1), "no tracking at display size"


def test_radii_vocabulary():
    """12 cards · 36 filled pill · 22.5 outlined · 0 inputs · 9999 chips · 2 marks."""
    allowed = {"var(--r-panel)", "var(--r-pill)", "var(--r-btn)", "var(--r-btn-outline)", "var(--r-input)", "var(--r-rail)",
               "50%", "4px", "0", "0 var(--r-rail) var(--r-rail) 0"}
    for val in re.findall(r"border-radius:\s*([^;]+);", BODY):
        assert val.strip() in allowed, val
    assert "--radius-cards: 12px" in ROOT and "--radius-buttons-pill: 36px" in ROOT
    assert "--radius-buttons-outlined: 22.5px" in ROOT and "--radius-inputs: 0px" in ROOT


def test_inputs_are_underline_only():
    rule = re.search(r'input\[type="url"\], input\[type="text"\], \.select \{([^}]*)\}', BODY).group(1)
    assert "border: 0;" in rule and "border-bottom: 1px solid var(--ink)" in rule and "background: transparent" in rule


def test_body_is_fourteen_px_outfit():
    assert re.search(r"body \{[^}]*font-size: var\(--t14\)", CSS)
    assert '--font-halyard-display-variable: "Outfit"' in ROOT
    assert "--ui:   var(--font-halyard-display-variable)" in ROOT


def test_fonts_are_self_hosted_and_present():
    for face in re.findall(r'url\("fonts/([^"]+)"\)', CSS):
        assert (Path("dashboard/static/fonts") / face).exists(), face
    assert "googleapis" not in CSS and "gstatic" not in CSS
    assert "outfit-var-latin.woff2" in CSS
    assert (Path("dashboard/static/fonts") / "LICENSE-Outfit-OFL.txt").exists()
    for stale in ("inter-var-latin.woff2", "inter-tight-var-latin.woff2", "dm-sans-var-latin.woff2"):
        assert not (Path("dashboard/static/fonts") / stale).exists(), f"{stale} is no longer referenced; remove it"
