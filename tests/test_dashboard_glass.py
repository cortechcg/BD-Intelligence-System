"""v3 glass, atmosphere and motion: restrained, accessible, and optional.

Glass is allowed on a short list of surfaces, always with a solid fallback and
always over a quiet teal and copper backdrop. Text on glass is checked against
the worst-case backdrop, not the average one. Motion is progressive: nothing
the page needs is hidden unless a script has run, and reduced motion gets the
finished page.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_dashboard_palette import contrast, token

GLASS = Path("dashboard/static/glass.css").read_text()
MOTION = Path("dashboard/static/motion.js").read_text()
STATIC = Path("dashboard/static")
TEMPLATES = Path("dashboard/templates")
CODE = re.sub(r"/\*.*?\*/", "", GLASS, flags=re.S)


def var(name: str) -> str:
    m = re.search(rf"--{re.escape(name)}:\s*([^;]+);", CODE)
    assert m, f"--{name} missing from glass.css"
    return m.group(1).strip()


def rgba(text: str) -> tuple[int, int, int, float]:
    m = re.fullmatch(r"rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", text)
    assert m, text
    return int(m[1]), int(m[2]), int(m[3]), float(m[4])


def over(top, bottom):
    r, g, b, a = top
    return tuple(round(a * t + (1 - a) * u) for t, u in zip((r, g, b), bottom))


def hexed(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


def worst_backdrop() -> tuple[int, int, int]:
    """Darkest backdrop the page can show: limestone, both washes stacked at
    peak alpha, the faint third wash, and a 3 percent allowance for the grain."""
    base = tuple(int(token("ground")[i:i + 2], 16) for i in (1, 3, 5))
    for wash in (rgba(var("wash-teal")), rgba(var("wash-copper")), (27, 73, 80, 0.08)):
        base = over(wash, base)
    return tuple(round(c * 0.97) for c in base)


def glass_over_worst(rgb, alpha):
    return over((*rgb, alpha), worst_backdrop())


def test_alphas_are_in_the_restrained_band():
    for name in ("glass-a-panel", "glass-a-rail", "glass-a-bar"):
        assert 0.55 <= float(var(name)) <= 0.75, name


def test_washes_are_teal_and_copper_only_and_quiet():
    teal, copper = rgba(var("wash-teal")), rgba(var("wash-copper"))
    assert teal[:3] == (27, 73, 80) and copper[:3] == (176, 85, 43)
    assert teal[3] <= 0.2 and copper[3] <= 0.15


def test_only_radial_washes_on_the_backdrop_and_no_purple_or_blue():
    assert "linear-gradient" not in CODE and "conic-gradient" not in CODE
    assert re.findall(r"([\w:.#\- ]+)\{[^{}]*radial-gradient", CODE) == ["body::before "]
    for r, g, b in re.findall(r"rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)", CODE):
        r, g, b = int(r), int(g), int(b)
        assert not (b > r + 40 and b > g + 40), f"blue-ish colour {(r, g, b)}"
        assert not (r > g + 30 and b > g + 30), f"purple-ish colour {(r, g, b)}"


@pytest.mark.parametrize("ink", ["ink", "ink-soft", "ink-2", "fill", "accent", "color-err-text"])
@pytest.mark.parametrize("kind", ["panel", "rail", "bar"])
def test_text_on_glass_clears_aa_on_the_worst_backdrop(ink, kind):
    alpha = float(var(f"glass-a-{kind}"))
    rgb = (232, 228, 217) if kind == "rail" else tuple(int(x) for x in var("glass-paper").split(","))
    bg = hexed(glass_over_worst(rgb, alpha))
    ratio = contrast(token(ink), bg)
    assert ratio >= 4.5, f"--{ink} on {kind} glass over the worst backdrop ({bg}) is {ratio:.2f}:1"


def test_on_fill_text_is_unaffected_by_glass():
    assert contrast(token("on-fill"), token("fill")) >= 4.5


def test_every_backdrop_filter_sits_behind_a_support_check_and_a_solid_fallback():
    assert "backdrop-filter" not in re.sub(r"@supports[^{]*\{.*", "", CODE, flags=re.S).split("@supports")[0]
    supports = CODE[CODE.index("@supports"):]
    assert "prefers-reduced-transparency" in supports
    solid_head = CODE[:CODE.index("@supports")]
    for sel in (".sidenav", ".masthead", ".panel--hero", ".figures", ".tr-figures", ".lib-panel", ".signin-card"):
        assert re.search(re.escape(sel) + r"[^{]*\{[^}]*background-color:\s*(var\(--(glass-solid|ground)[\w-]*\)|#)", solid_head, flags=re.S), f"no solid fallback for {sel}"


def test_blurred_layers_are_capped_and_blur_is_modest():
    supports = CODE[CODE.index("@supports"):]
    blocks = re.findall(r"([^{}]+)\{[^{}]*backdrop-filter", supports)
    selectors = {s.strip() for blk in blocks for s in blk.split(",")}
    # Selectors across the whole UI, not layers on one page: the busiest page
    # (track record) shows five at once (rail, bar, facts, figures, one open panel).
    assert len(selectors) <= 16, selectors
    for px in re.findall(r"blur\((\d+)px", CODE):
        assert int(px) <= 20
    for banned in (".lib-card", ".table", "tr", ".panel,", ".tile,"):
        assert banned not in {s.strip() for s in selectors}, "lists and dense panels stay solid"


def test_reduced_motion_is_honoured_in_css_and_js():
    assert "@media (prefers-reduced-motion: reduce)" in GLASS
    assert "animation-duration: 0.001ms" in GLASS or "animation: none" in GLASS
    assert "prefers-reduced-motion: reduce" in MOTION
    for css in STATIC.glob("*.css"):
        text = css.read_text()
        if "@keyframes" in text:
            assert "prefers-reduced-motion" in text or css.name == "app.css", f"{css.name} animates without a reduced-motion rule"


def test_nothing_is_hidden_unless_a_script_has_run():
    # .rv is only ever added by motion.js, and the split headline has a timed fallback.
    for tpl in TEMPLATES.glob("*.html"):
        assert 'class="rv' not in tpl.read_text() and " rv " not in tpl.read_text()
    assert 'classList.add("rv")' in MOTION
    assert re.search(r"\.js \[data-split\]:not\(\.is-split\)\s*\{[^}]*animation:\s*fx-fallback", CODE)
    assert "setTimeout" in MOTION  # safety net reveals anything the observer missed
    # The only `opacity: 0` on content sits behind .js, .rv or .is-split.
    for sel, rule in re.findall(r"([^{}]+)\{([^{}]*)\}", CODE):
        if re.search(r"(^|;)\s*opacity:\s*0\s*(;|$)", rule):
            assert any(k in sel for k in (".js", ".rv", ".is-split")), sel


def test_no_external_requests_in_the_new_assets():
    for f in ("glass.css", "motion.js", "library.css", "library.js", "track.css", "track.js"):
        text = (STATIC / f).read_text()
        assert not re.search(r"https?://|@import|//cdn", text), f


def test_no_em_dashes_or_emoji_in_the_new_files():
    files = [STATIC / n for n in ("glass.css", "motion.js", "library.css", "library.js", "track.css", "track.js")]
    files += [TEMPLATES / "library.html", TEMPLATES / "track_record.html", Path("dashboard/track_record.py")]
    for f in files:
        text = f.read_text()
        assert "\u2014" not in text and "\u2013" not in text, f"dash in {f}"
        assert not re.search("[\U0001F300-\U0001FAFF\u2600-\u27BF]", text), f"emoji in {f}"


def test_every_page_loads_the_surface_layer():
    for name in ("base.html", "login.html", "denied.html"):
        text = (TEMPLATES / name).read_text()
        assert "glass.css" in text and "motion.js" in text, name


def test_every_page_links_a_local_favicon_that_exists():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "dashboard"
    assert (root / "static/brand/favicon.png").exists()
    for name in ("base", "login", "denied"):
        assert "brand/favicon.png" in (root / "templates" / f"{name}.html").read_text(), name


def test_track_record_theme_tiles_and_small_text_pass_aa():
    """Tile ramp pairs and the muted text that sits straight on the atmosphere."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "track.css").read_text(), flags=re.S)
    tile = {n: re.search(rf"\.tr-theme--{n}\s*\{{[^}}]*background:\s*([^;]+);", css).group(1).strip() for n in (1, 2, 3, 4)}
    resolve = lambda v: token(v[6:-1]) if v.startswith("var(--") else v
    paper = "#fbfaf6"
    assert contrast(token("ink"), resolve(tile[1])) >= 4.5
    assert contrast(token("ink"), resolve(tile[2])) >= 4.5
    assert contrast(paper, resolve(tile[3])) >= 4.5
    assert contrast(paper, resolve(tile[4])) >= 4.5
    # Muted ink straight on the atmosphere, for the empty theme tile and the fine
    # print. The teal wash and the faint wash do overlap near the top; the copper
    # wash sits in the opposite corner, so it is not stacked on them here. Text on
    # translucent glass is covered by the worst-case test above.
    base = tuple(int(token("ground")[i:i + 2], 16) for i in (1, 3, 5))
    for wash in (rgba(var("wash-teal")), (27, 73, 80, 0.08)):
        base = over(wash, base)
    worst = hexed(tuple(round(c * 0.97) for c in base))
    assert contrast(token("ink-2"), worst) >= 4.5
    assert contrast(token("ink-soft"), worst) >= 4.5
    # Tags sit on their own paper chip, so their accent and teal text is checked on paper.
    assert re.search(r"\.tr-tag \{[^}]*background: var\(--surface\)", css)
    assert contrast(token("accent"), token("surface")) >= 4.5
    assert contrast(token("fill"), token("surface")) >= 4.5


def test_official_logo_is_shipped_unaltered_and_never_shown_above_natural_size():
    from PIL import Image

    logo = STATIC / "brand/cortech-logo.png"
    assert Image.open(logo).size == (180, 67)
    for name in ("base", "login", "denied", "track_record"):
        text = (TEMPLATES / f"{name}.html").read_text()
        assert "brand/cortech-logo.png" in text, name
        for w in re.findall(r'class="brand-logo[^"]*"[^>]*width="(\d+)"', text):
            assert int(w) <= 180
    css = (STATIC / "app.css").read_text()
    for w in re.findall(r"\.brand-logo[^{]*\{[^}]*?width:\s*(\d+)px", css):
        assert int(w) <= 180
    fav = Image.open(STATIC / "brand/favicon.png")
    assert fav.size == (64, 64)
