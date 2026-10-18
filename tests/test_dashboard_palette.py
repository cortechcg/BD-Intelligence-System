"""The dashboard UI palette is checked, not eyeballed.

Parchment and moss: a warm parchment ground (#f4efe6), cream cards (#fffcf6),
ink text (#22231f), moss (#3f4a2e) for primary actions and progress, ochre
(#9a6b1f marks, #8c5f17 text) only where a person must act, and clay
(#a4553c) for errors, with a slightly deeper desk (#efe9dd) behind the index
column. Every text colour must clear WCAG AA (4.5:1) on the page, and every
mark must clear 3:1. Type: Instrument Sans for the interface, Newsreader for
headings and display figures.
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
                                        "st-processing", "ink-3")]
RAMP_LIGHT_END = [("ramp-1", "ground"), ("rec-1", "ground")]


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_on_abyss_clears_aa(fg, bg):
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
    """Missing display figures clear 3:1 (large missing figures) but not 4.5 (body)."""
    assert 3.0 <= contrast(token("ink-3"), token("ground")) < 4.5
    for selector, rule in rules():
        if re.search(r"(?<![-\w])color:\s*var\(--ink-3\)", rule):
            sel = selector.strip()
            assert any(k in sel for k in ("is-zero", "is-missing", "[disabled]", "aria-disabled")), f"sage as text: {sel[:70]}"


def test_parchment_desk_and_no_gradients():
    """Parchment desk: #f4efe6 field, #fffcf6 card, #ece5d8 track, #3f4a2e moss primary,
    #22231f ink, ochre #9a6b1f for marks a person must act on, clay #a4553c for errors."""
    assert token("ground") == "#f4efe6"
    assert token("surface") == "#fffcf6"
    assert token("track") == "#ece5d8"
    assert token("fill") == "#3f4a2e"
    assert token("ink") == "#22231f"
    assert token("human") == "#9a6b1f"
    assert token("color-err-strong") == "#a4553c"
    assert token("surface") != token("ground")
    shadow_ok = (
        "page--portfolio", ".panel", ".tile", ".table-wrap", ".auth",
        ".band", ".notice", ".empty", ".disclosure", ".sidenav", ".stageboard",
        ".intake-card",
    )
    for selector, rule in rules():
        sel = selector.strip()
        if re.search(r"(^|;|\s)box-shadow:(?!\s*none)", rule):
            assert any(k in sel for k in shadow_ok), f"shadow: {sel[:70]}"
        assert "gradient(" not in rule, f"gradient in {sel[:70]}"
    assert "gradient(" not in CSS, "flat colour only, no gradients anywhere"
    assert re.search(r"\.btn \{[^}]*background: var\(--fill\)", BODY), "the primary button is flat moss"
    assert ".panel--hero { border-top: 0; }" in BODY


def test_two_fills_only():
    """Moss fills the primary button, BID chip, step badges, and marks."""
    for selector, rule in rules():
        sel = selector.strip()
        for m in re.finditer(r"background(-color)?:\s*var\(--([\w-]+)\)", rule):
            tok = m.group(2)
            if tok in (
                "ground", "surface", "track", "hover", "selected", "glass", "line", "desk",
                "stepper-well", "color-upcoming", "color-ok-bg", "color-warn-bg", "color-err-bg",
            ):
                continue
            assert any(k in sel for k in (
                ".btn", ".chip--bid", "rail__seg", "meter__fill", "::before", "state--",
                "bar__fill", "mix__seg", "legend__swatch", ".orb", ".brand__mark",
                ".nav__dot", ".steps__num", ".masthead", "stage-progress__seg",
                ".doc-chip__icon", ".match-bar__fill", ".tile--dark",
            )), f"fill {tok} on {sel[:70]}"


def test_brass_means_a_person_must_act_and_nothing_else():
    for selector, rule in rules():
        sel = selector.strip()
        if re.search(r"background(-color)?:\s*var\(--(accent|human|halt|st-halt|color-brass)\)", rule):
            assert any(k in sel for k in (
                "rail__seg", "meter__fill", "state--", "::before", ".orb", ".match-bar__fill--gap",
            )), f"brass fill outside marks: {sel[:70]}"
        for m in re.finditer(r"(?<![-\w])color:\s*var\(--([\w-]+)\)", rule):
            if m.group(1) in ("accent", "human", "halt", "color-brass", "st-halt"):
                assert "attention" in sel, f"brass used as text outside an attention signal: {sel[:70]}"
        if "focus" in sel:
            assert "accent" not in rule and "brass" not in rule, "brass is never a focus ring"
    assert ".hl--attention { color: var(--accent); }" in BODY
    assert ".figure--attention .figure__value:not(.is-zero),\n.figure--review .figure__value:not(.is-zero) { color: var(--accent);" in BODY
    assert "border-left: 2px dashed var(--human)" in BODY


def test_rail_is_thin_and_human_segment_is_distinct():
    assert re.search(r"\.rail__seg \{[^}]*height: 4px", BODY), "queue-row rail segments are a 4px line"
    assert "border-top: 2px dashed var(--human)" in BODY
    assert ".rail__seg--human { background: transparent; border-top: 2px dashed var(--human); height: 0;" in BODY
    assert contrast(token("stage-done"), token("stage-current")) >= 1.8, "current vs done must differ in lightness"


def _hue(h: str) -> float:
    import colorsys
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360


def test_ramps_are_one_moss_hue_and_monotone():
    toks = [token(f"ramp-{i}") for i in range(1, 5)]
    lums = [_lum(t) for t in toks]
    assert lums == sorted(lums, reverse=True), "light→dark along the pipeline"
    assert all(a - b >= 0.04 for a, b in zip(lums, lums[1:]))
    assert toks[-1] == token("fill"), "the dark end of the ramp is the primary moss"
    hues = [_hue(t) for t in toks]
    for t, h in zip(toks, hues):
        assert 65 <= h <= 90, f"{t} (hue {h:.0f}) is not moss"
    assert max(hues) - min(hues) <= 15, f"ramp drifts across hues: {[round(h) for h in hues]}"


def test_recommendation_uses_weight_and_shape_not_hue():
    for chip in ("chip--bid", "chip--watch", "chip--nobid"):
        rule = re.search(rf"\.{chip}\s*\{{([^}}]*)\}}", CSS).group(1)
        for forbidden in ("--accent", "--human", "--halt", "--st-", "--stage-", "--color-brass"):
            assert forbidden not in rule, f".{chip} uses {forbidden}"


def test_no_colour_literal_outside_the_token_block():
    stray = re.findall(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", BODY)
    assert not stray, f"colour literals outside :root: {stray}"


def test_type_is_two_weights_and_captions_are_the_only_uppercase():
    """Uppercase is kept for figure captions and the BID/WATCH chips; buttons and nav read as sentences."""
    for selector, rule in rules():
        for w in re.findall(r"font-weight:\s*([^;]+);", rule):
            assert w.strip() in ("var(--font-weight-regular)", "var(--font-weight-medium)"), f"{selector.strip()[:50]}: {w}"
    upper = [sel.strip() for sel, rule in rules() if "text-transform: uppercase" in rule]
    assert len(upper) == 2, f"uppercase on captions and chips only: {upper}"
    assert any(".label" in sel for sel in upper)
    assert any(".chip" in sel for sel in upper)
    assert not any(sel.startswith(".btn") or ".nav a" in sel for sel in upper)
    assert not re.search(r"h1, h2, h3 \{[^}]*uppercase", BODY)
    assert not re.search(r"\.btn \{[^}]*text-transform: uppercase", BODY)


def test_captions_controls_and_focus_are_legible():
    """Small captions clear AA on cards, control borders and the focus ring clear 3:1 (WCAG 1.4.11)."""
    assert contrast(token("ink-2"), token("surface")) >= 4.5
    assert contrast(token("ink-2"), token("hover")) >= 4.5
    assert contrast(token("line-input"), token("surface")) >= 3.0
    assert contrast(token("line-input"), token("hover")) >= 3.0
    assert contrast(token("focus-ring"), token("surface")) >= 3.0
    assert contrast(token("focus-ring"), token("ground")) >= 3.0
    caption = re.search(r"\.label, \.eyebrow[^{]*\{([^}]*)\}", BODY).group(1)
    assert "color: var(--ink-2)" in caption, "captions use --ink-2, not mid green"
    assert not re.search(r"font-size:\s*(?:[0-9]|1[01])px", CSS), "nothing renders under 12px"


def test_hierarchy_is_size():
    assert re.search(r"\.figure__value \{[^}]*font-size: var\(--t32\)", BODY)
    assert ".figure--attention .figure__value, .figure--review .figure__value { font-size: var(--t56)" in BODY
    assert re.search(r"\.figure__value \{[^}]*min-height: var\(--t56\)", BODY), "values share one baseline"
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
    assert "border-radius: var(--r-pill)" in state and "background: var(--track)" in state, "status is a filled chip"
    assert ".state--completed           { background: var(--fill); color: var(--on-fill); }" in BODY
    assert ".state--failed, .state--dead_letter, .state--spend_cap, .state--cancelled { background: var(--accent); color: var(--on-fill); }" in BODY
    assert contrast(token("on-fill"), token("accent")) >= 4.5 and contrast(token("on-fill"), token("fill")) >= 4.5
    assert contrast(token("ink"), token("track")) >= 4.5


def test_fonts_are_self_hosted_and_present():
    for face in re.findall(r'url\("fonts/([^"]+)"\)', CSS):
        assert (Path("dashboard/static/fonts") / face).exists(), face
    assert "googleapis" not in CSS and "gstatic" not in CSS
    assert (Path("dashboard/static/fonts") / "LICENSE-Outfit-OFL.txt").exists()
    for lic in ("LICENSE-Newsreader-OFL.txt", "LICENSE-InstrumentSans-OFL.txt"):
        assert "Open Font License" in (Path("dashboard/static/fonts") / lic).read_text(), lic


def test_type_pairing_is_instrument_sans_and_newsreader():
    """Instrument Sans sets the interface; Newsreader sets headings and display figures."""
    assert re.search(r'--ui:\s*"Instrument Sans var"', ROOT)
    assert re.search(r'--doc:\s*"Newsreader var"', ROOT)
    assert re.search(r"--fig:\s*var\(--doc\)", ROOT)
    assert 'url("fonts/instrument-sans-var-latin.woff2")' in CSS
    assert "IBM Plex Sans" not in CSS, "the old interface face is fully retired"
    assert contrast(token("ink-2"), token("desk")) >= 4.5, "captions stay AA on the desk column"
    assert contrast(token("ink"), token("desk")) >= 4.5
