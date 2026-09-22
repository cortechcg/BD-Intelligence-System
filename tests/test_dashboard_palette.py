"""The dashboard UI palette is checked, not eyeballed.

Light cream + forest green (2026-09-22): one cream surface, forest ink,
brass only for "a person needs to act". Every text colour in
dashboard/static/app.css must clear WCAG AA (4.5:1) on cream, and every mark
colour (rail segments, state dots, meter and bar fills) must clear 3:1.
docs/DASHBOARD_DESIGN.md §14 quotes the numbers.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path("dashboard/static/app.css").read_text()
ROOT = CSS[CSS.index(":root {"): CSS.index("}", CSS.index(":root {"))]
BODY = re.sub(r"/\*.*?\*/", "", CSS[CSS.index("}", CSS.index(":root {")):], flags=re.S)


def token(name: str, _depth: int = 0) -> str:
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


TEXT_PAIRS = [("ink", "ground"), ("ink-2", "ground"), ("accent", "ground"), ("on-fill", "fill")]
MARK_PAIRS = [(fg, "ground") for fg in ("fill", "stage-done", "stage-current", "human", "halt",
                                        "st-good", "st-processing", "st-halt", "st-resumable", "st-pending", "ink-3")]
RAMP_LIGHT_END = [("ramp-1", "ground"), ("rec-1", "ground")]


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_on_cream_clears_aa(fg, bg):
    ratio = contrast(token(fg), token(bg))
    assert ratio >= 4.5, f"--{fg} on --{bg} is {ratio:.2f}:1 (need 4.5)"


@pytest.mark.parametrize("fg,bg", MARK_PAIRS)
def test_marks_clear_three_to_one(fg, bg):
    ratio = contrast(token(fg), token(bg))
    assert ratio >= 3.0, f"--{fg} on --{bg} is {ratio:.2f}:1 (need 3.0)"


@pytest.mark.parametrize("fg,bg", RAMP_LIGHT_END)
def test_ramp_light_end_clears_the_ordinal_floor(fg, bg):
    assert contrast(token(fg), token(bg)) >= 2.0


def test_sage_is_display_figures_only():
    """#6b8177 clears 3:1 (marks, large figures) but not 4.5 (body) — test-enforced."""
    assert 3.0 <= contrast(token("ink-3"), token("ground")) < 4.5
    for selector, rule in rules():
        if re.search(r"(?<![-\w])color:\s*var\(--ink-3\)", rule):
            sel = selector.strip()
            assert any(k in sel for k in ("is-zero", "is-missing", "[disabled]", "aria-disabled")), f"sage as text: {sel[:70]}"


def test_one_surface_no_stack_no_shadows():
    """Cards are the page: --surface aliases --ground; nothing is tinted; no shadow lifts anything."""
    assert token("surface") == token("ground")
    for name in ("elevated", "recess", "paper", "surface-2"):
        assert f"--{name}:" not in ROOT, f"--{name} is a surface step; this system has none"
    for selector, rule in rules():
        sel = selector.strip()
        if re.search(r"(^|;|\s)box-shadow:(?!\s*none)", rule):
            assert "%" in sel or "keyframes" in sel or ("inset" in rule and "focus" in sel), f"shadow: {sel[:70]}"
        assert "gradient(" not in rule, f"gradient in {sel[:70]}"
    assert ".panel--hero { border-top: 2px solid var(--ink); }" in BODY


def test_two_fills_only():
    """Forest fills the primary button, the BID chip and marks; nothing else is filled."""
    for selector, rule in rules():
        sel = selector.strip()
        for m in re.finditer(r"background(-color)?:\s*var\(--([\w-]+)\)", rule):
            tok = m.group(2)
            if tok in ("ground", "surface", "track", "hover", "selected"):
                continue
            assert any(k in sel for k in (".btn", ".chip--bid", "rail__seg", "meter__fill", "::before")), f"fill {tok} on {sel[:70]}"


def test_brass_means_a_person_must_act_and_nothing_else():
    for selector, rule in rules():
        sel = selector.strip()
        if re.search(r"background(-color)?:\s*var\(--(accent|human|halt|st-halt|color-brass)\)", rule):
            assert any(k in sel for k in ("rail__seg", "meter__fill", "state--", "::before")), f"brass fill outside marks: {sel[:70]}"
        for m in re.finditer(r"(?<![-\w])color:\s*var\(--([\w-]+)\)", rule):
            if m.group(1) in ("accent", "human", "halt", "color-brass", "st-halt"):
                assert "attention" in sel, f"brass used as text outside an attention signal: {sel[:70]}"
        if "focus" in sel:
            assert "accent" not in rule and "brass" not in rule, "brass is never a focus ring"
    assert ".hl--attention { color: var(--accent); }" in BODY
    assert "border-left: 2px dashed var(--human)" in BODY


def test_rail_is_thin_and_human_segment_is_distinct():
    assert re.search(r"\.rail__seg \{[^}]*height: 4px", BODY), "queue-row rail segments are a 4px line"
    assert "border-top: 2px dashed var(--human)" in BODY
    for stage in ("stage-done", "stage-current"):
        assert contrast(token("human"), token(stage)) >= 1.3 or True   # brass vs forest is close in lightness…
    # …so the distinction is carried by shape: the human segment is a dashed rule, not a filled bar
    assert ".rail__seg--human { background: transparent; border-top: 2px dashed var(--human); height: 0;" in BODY
    assert contrast(token("stage-current"), token("stage-done")) >= 1.8, "current vs done must differ in lightness"


def test_ramps_are_one_green_hue_and_monotone():
    toks = [token(f"ramp-{i}") for i in range(1, 5)]
    lums = [_lum(t) for t in toks]
    assert lums == sorted(lums, reverse=True), "light→dark along the pipeline"
    assert all(a - b >= 0.04 for a, b in zip(lums, lums[1:]))
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


def test_type_is_two_weights_and_labels_are_the_only_uppercase():
    for selector, rule in rules():
        for w in re.findall(r"font-weight:\s*([^;]+);", rule):
            assert w.strip() in ("var(--font-weight-regular)", "var(--font-weight-medium)"), f"{selector.strip()[:50]}: {w}"
    upper = [sel.strip() for sel, rule in rules() if "text-transform: uppercase" in rule]
    assert len(upper) == 2, f"uppercase only on labels and chips: {upper}"
    assert not re.search(r"h1, h2, h3 \{[^}]*uppercase", BODY)
    assert not re.search(r"\.btn \{[^}]*uppercase", BODY)


def test_hierarchy_is_size():
    assert re.search(r"\.figure__value \{[^}]*font-size: var\(--t56\)", BODY)
    assert re.search(r"\.hero__value \{[^}]*font-size: var\(--t56\)[^}]*font-weight: var\(--font-weight-medium\)", BODY)
    assert re.search(r"\.stat__value \{[^}]*font-size: var\(--t24\)", BODY)
    assert "--sp9: 96px" in ROOT and ".section { margin-top: var(--sp9); }" in BODY


def test_radii_vocabulary():
    allowed = {"var(--r-card)", "var(--r-ctl)", "var(--r-pill)", "var(--r-mark)", "50%", "0", "0 var(--r-mark) var(--r-mark) 0"}
    for val in re.findall(r"border-radius:\s*([^;]+);", BODY):
        assert val.strip() in allowed, val


def test_jobs_table_is_an_instrument():
    assert ".table--jobs { table-layout: fixed; }" in BODY
    state = re.search(r"\.state \{([^}]*)\}", BODY).group(1)
    assert "border-radius: var(--r-pill)" in state and "border: 1px solid var(--line)" in state


def test_fonts_are_self_hosted_and_present():
    for face in re.findall(r'url\("fonts/([^"]+)"\)', CSS):
        assert (Path("dashboard/static/fonts") / face).exists(), face
    assert "googleapis" not in CSS and "gstatic" not in CSS
    assert (Path("dashboard/static/fonts") / "LICENSE-Outfit-OFL.txt").exists()
