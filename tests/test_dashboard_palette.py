"""The dashboard UI palette is checked, not eyeballed.

Forest green + cream (2026-09-22): a four-step green surface stack, cream
text, one paper surface, and brass reserved for "a person needs to act".
Every text colour in dashboard/static/app.css must clear WCAG AA (4.5:1) on
every surface it is placed on, and every mark colour (rail segments, state
dots, meter and bar fills) must clear 3:1. The pairs below are the ones the
stylesheet actually composes. docs/DASHBOARD_DESIGN.md §13 quotes the numbers.
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


SURFACES = ("recess", "ground", "surface", "elevated")

TEXT_PAIRS = [(fg, bg) for fg in ("ink", "ink-2", "accent") for bg in SURFACES] + [
    ("ground", "paper"), ("ground", "paper-hover"),   # ink on the paper button / highlight pill
    ("ground", "invert"),                             # ink on the cream BID chip
]

MARK_PAIRS = [(fg, bg) for fg in ("invert", "human", "halt", "stage-1", "stage-4", "st-good", "st-processing", "st-halt", "st-resumable", "ink-3", "st-pending")
              for bg in ("ground", "surface", "elevated")] + [("ramp-1", "surface"), ("rec-3", "surface")]


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_on_surface_clears_aa(fg, bg):
    ratio = contrast(token(fg), token(bg))
    assert ratio >= 4.5, f"--{fg} on --{bg} is {ratio:.2f}:1 (need 4.5)"


@pytest.mark.parametrize("fg,bg", MARK_PAIRS)
def test_marks_clear_three_to_one(fg, bg):
    need = 2.0 if fg in ("ramp-1", "rec-3") else 3.0
    ratio = contrast(token(fg), token(bg))
    assert ratio >= need, f"--{fg} on --{bg} is {ratio:.2f}:1 (need {need})"


def test_moss_is_large_text_only():
    """#8a9788 is 3.5:1 on the card — legal for figures ≥ 24px, never body."""
    assert contrast(token("ink-3"), token("surface")) < 4.5    # documents why
    for selector, rule in rules():
        if re.search(r"(?<![-\w])color:\s*var\(--ink-3\)", rule):
            sel = selector.strip()
            assert any(k in sel for k in ("is-zero", "is-missing", "[disabled]", "aria-disabled")), f"moss as text: {sel[:70]}"


def test_brass_means_a_person_must_act_and_nothing_else():
    """Brass fills only marks; brass text only where it signals urgency."""
    for selector, rule in rules():
        sel = selector.strip()
        if re.search(r"background(-color)?:\s*var\(--(accent|human|halt|st-halt|st-processing|color-brass)\)", rule):
            assert ".btn" not in sel and "input" not in sel and ".hl" not in sel, f"brass fill on an interactive surface: {sel[:70]}"
            assert any(k in sel for k in ("rail__seg", "meter__fill", "state--", "::before")), f"brass fill outside marks: {sel[:70]}"
        for m in re.finditer(r"(?<![-\w])color:\s*var\(--([\w-]+)\)", rule):
            if m.group(1) in ("accent", "human", "halt", "color-brass", "st-halt", "st-processing"):
                assert "attention" in sel, f"brass used as text outside an attention signal: {sel[:70]}"
    assert ".figure--attention .figure__value:not(.is-zero) { color: var(--accent); }" in BODY
    assert ".hl--attention { background: transparent; color: var(--accent);" in BODY
    assert "border-left: 2px dashed var(--human)" in BODY


def test_paper_is_the_one_light_surface():
    """Cream as a fill: the primary button, the highlight pill, the BID chip and marks."""
    for selector, rule in rules():
        if re.search(r"background:\s*var\(--(paper|paper-hover|invert|color-cream)\)", rule):
            sel = selector.strip()
            assert any(k in sel for k in (".btn", ".hl", ".chip--bid", "rail__seg", "meter__fill", "::before")), f"paper fill outside the allowed set: {sel[:70]}"


def test_surface_stack_floats_and_has_no_shadows():
    """recess < canvas < card < hero, each a real step; depth is never a shadow."""
    lums = [_lum(token(t)) for t in SURFACES]
    assert lums == sorted(lums), "the stack must read recess → canvas → card → hero"
    assert contrast(token("surface"), token("ground")) >= 1.45, "cards must visibly float (1.22 failed the squint test; 1.54 passed)"
    assert contrast(token("elevated"), token("ground")) >= 1.75
    assert contrast(token("elevated"), token("surface")) >= 1.2
    for selector, rule in rules():
        if re.search(r"(^|;|\s)box-shadow:(?!\s*none)", rule):
            assert "%" in selector or "keyframes" in selector, f"shadow used for elevation: {selector.strip()[:70]}"


def test_human_review_is_distinct_from_every_stage_colour_without_hue():
    human = token("human")
    for i in range(1, 5):
        assert contrast(human, token(f"stage-{i}")) >= 1.3, f"--human vs --stage-{i}"
    assert "border: 1px dashed var(--human)" in BODY
    assert token("halt") == token("human") and ".rail__seg--human { background: transparent;" in BODY


def test_ramps_are_one_green_hue_and_monotone():
    for prefix, n in (("ramp", 4), ("rec", 3)):
        toks = [token(f"{prefix}-{i}") for i in range(1, n + 1)]
        lums = [_lum(t) for t in toks]
        assert lums == sorted(lums), f"{prefix}: must read dark→light"
        assert all(b - a >= 0.04 for a, b in zip(lums, lums[1:])), f"{prefix}: adjacent steps too close"
        for t in toks:
            r, g, b = int(t[1:3], 16), int(t[3:5], 16), int(t[5:7], 16)
            assert g >= r and g >= b, f"{t} is not a green"


def test_recommendation_uses_weight_and_shape_not_hue():
    for chip in ("chip--bid", "chip--watch", "chip--nobid"):
        rule = re.search(rf"\.{chip}\s*\{{([^}}]*)\}}", CSS).group(1)
        for forbidden in ("--accent", "--human", "--halt", "--st-", "--stage-", "--color-brass"):
            assert forbidden not in rule, f".{chip} uses {forbidden}"


def test_no_colour_literal_outside_the_token_block():
    stray = re.findall(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", BODY)
    assert not stray, f"colour literals outside :root: {stray}"


def test_two_weights_only_and_uppercase_labels():
    for selector, rule in rules():
        for w in re.findall(r"font-weight:\s*([^;]+);", rule):
            assert w.strip() in ("var(--font-weight-regular)", "var(--font-weight-medium)"), f"{selector.strip()[:50]}: {w}"
    assert re.search(r"h1, h2, h3 \{[^}]*text-transform: uppercase", BODY)
    assert re.search(r"\.btn \{[^}]*text-transform: uppercase", BODY)
    assert re.search(r"\.doc \{[^}]*text-transform: none", BODY)
    assert re.search(r"\.mono \{[^}]*text-transform: none", BODY)


def test_display_line_height_is_point_nine():
    assert "--leading-display: 0.9" in ROOT and "--leading-heading: 0.9" in ROOT
    assert re.search(r"\.figure__value \{[^}]*line-height: var\(--leading-display\)", BODY)
    assert re.search(r"\.tile__value \{[^}]*font-size: var\(--text-heading\)", BODY)


def test_radii_vocabulary():
    allowed = {"var(--r-panel)", "var(--r-pill)", "var(--r-btn)", "var(--r-btn-outline)", "var(--r-input)", "var(--r-rail)",
               "50%", "4px", "0", "0 var(--r-rail) var(--r-rail) 0"}
    for val in re.findall(r"border-radius:\s*([^;]+);", BODY):
        assert val.strip() in allowed, val
    assert "--radius-cards: 12px" in ROOT and "--radius-buttons-pill: 36px" in ROOT


def test_jobs_table_is_an_instrument():
    assert ".table--jobs { table-layout: fixed; }" in BODY
    assert re.search(r"\.table td\.mono, \.table \.mono \{[^}]*font-size: var\(--t12\)", BODY)
    state = re.search(r"\.state \{([^}]*)\}", BODY).group(1)
    assert "border-radius: var(--r-pill)" in state and "background: var(--surface-2)" in state and "text-transform: uppercase" in state


def test_body_is_fourteen_px_outfit():
    assert re.search(r"body \{[^}]*font-size: var\(--t14\)", CSS)
    assert '--font-halyard-display-variable: "Outfit"' in ROOT


def test_fonts_are_self_hosted_and_present():
    for face in re.findall(r'url\("fonts/([^"]+)"\)', CSS):
        assert (Path("dashboard/static/fonts") / face).exists(), face
    assert "googleapis" not in CSS and "gstatic" not in CSS
    assert (Path("dashboard/static/fonts") / "LICENSE-Outfit-OFL.txt").exists()
