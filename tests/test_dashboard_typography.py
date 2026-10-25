"""Header hierarchy: two bar levels, a distinct header face, AA on every header pair."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_dashboard_palette import contrast

STATIC = Path("dashboard/static")
TEMPLATES = Path("dashboard/templates")
CSS = (STATIC / "type.css").read_text()


def _tok(name: str) -> str:
    m = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", CSS)
    assert m, name
    return m.group(1)


def test_every_page_loads_type_css_last_and_the_version_covers_it():
    for name in ("base.html", "login.html", "denied.html"):
        text = (TEMPLATES / name).read_text()
        assert "type.css" in text, name
        assert text.index("type.css") > text.index("glass.css"), name
    base = (TEMPLATES / "base.html").read_text()
    assert base.index("type.css") > base.index("head_extra"), "page sheets must load before type.css"
    assert "type.css" in Path("dashboard/app.py").read_text()


@pytest.mark.parametrize(
    "fg,bg,floor",
    [
        ("bar-1-ink", "bar-1", 7.0),
        ("bar-ink", "bar-2", 7.0),
        ("bar-ink", "bar-2-side", 7.0),
        ("bar-rule", "bar-2", 4.5),
    ],
)
def test_header_pairs_clear_wcag_aa(fg, bg, floor):
    assert contrast(_tok(fg), _tok(bg)) >= floor


def test_header_bars_clear_the_page_and_the_body_text():
    from tests.test_dashboard_palette import token

    assert contrast(_tok("bar-1"), token("color-limestone")) >= 4.5  # the bar edge is visible
    assert contrast(_tok("bar-rule"), token("color-limestone")) >= 4.5
    assert contrast(token("color-muted"), token("color-limestone")) >= 4.5  # secondary detail stays readable


def test_header_face_is_self_hosted_and_licensed_and_distinct_from_body():
    assert (STATIC / "fonts/outfit-var-latin.woff2").exists()
    assert (STATIC / "fonts/LICENSE-Outfit-OFL.txt").exists()
    assert 'url("fonts/outfit-var-latin.woff2")' in CSS
    assert "http" not in re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    hd = re.search(r"--hd:\s*([^;]+);", CSS).group(1)
    assert hd.startswith('"Outfit var"')
    app = (STATIC / "app.css").read_text()
    assert re.search(r"--ui:\s*\"Public Sans var\"", app)  # body face unchanged


def test_levels_use_the_documented_weights_and_sizes():
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    h1 = re.search(r"body h1:not\(#_\):not\(\.sr-only\) \{(.*?)\}", css, flags=re.S).group(1)
    assert "font-weight: 700" in h1 and "var(--bar-1)" in h1 and "var(--bar-1-ink)" in h1
    h23 = re.search(r"body :is\(h2, h3\):not\(#_\):not\(\.sr-only\) \{(.*?)\}", css, flags=re.S).group(1)
    assert "font-weight: 600" in h23 and "var(--bar-2)" in h23 and "border-left: 4px solid" in h23
    h2 = re.search(r"body h2:not\(#_\):not\(\.sr-only\) \{ font-size: (\d+)px", css).group(1)
    assert int(h2) >= 20
    assert "--body-size: 14.5px" in css
    body = re.search(r"body \{ font-size: var\(--body-size\); \}", css)
    assert body


def test_table_heads_and_sidebar_labels_are_barred_too():
    assert re.search(r"\.table, \.tr-table, \.lib-table", CSS)
    assert "sidenav__caption" in CSS and "sidenav__head" in CSS


def test_sr_only_headings_are_left_alone_and_nothing_is_hidden():
    assert CSS.count(":not(.sr-only)") >= 4
    assert "display: none" not in CSS and "visibility" not in CSS


def test_no_em_dashes_or_emoji_in_type_css():
    assert "\u2014" not in CSS and "\u2013" not in CSS
    assert not re.search("[\U0001F300-\U0001FAFF\u2600-\u27BF]", CSS)
