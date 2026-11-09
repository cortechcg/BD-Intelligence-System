"""Palette and layout checks for the warm charcoal dashboard theme."""
from __future__ import annotations

import re
from pathlib import Path


THEME_PATH = Path("dashboard/static/theme.css")
THEME = THEME_PATH.read_text()
ROOT = re.search(r":root\s*\{([^}]*)\}", THEME, re.S).group(1)


def token(name: str) -> str:
    match = re.search(rf"--{re.escape(name)}:\s*([^;]+);", ROOT)
    assert match, f"--{name} is not defined in theme.css"
    value = match.group(1).strip()
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"--{name} is not a solid color: {value}"
    return value


def luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]

    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground: str, background: str) -> float:
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_warm_charcoal_palette_and_aa_text_pairs():
    assert token("ground") == "#5a4a42"
    assert token("surface") == "#3a3936"
    assert token("ink") == "#ece6dc"
    assert token("fill") == "#7fcfa0"
    assert token("human") == "#d9b86c"
    assert token("halt") == "#c8775a"
    assert token("focus-ring") == "#d9b86c"

    assert contrast(token("ink"), token("surface")) >= 4.5
    assert contrast(token("ink-2"), token("surface")) >= 4.5
    assert contrast(token("ink-2"), token("ground")) >= 4.5
    assert contrast("#1f2a24", token("fill")) >= 4.5
    assert contrast("#f0dfd3", "#6b5650") >= 4.5
    assert contrast("#e8cf8e", token("surface")) >= 4.5


def test_dark_shell_cards_and_responsive_grid_tokens():
    assert ".app-shell {\n  margin: 24px;" in THEME
    assert "min-height: calc(100vh - 48px)" in THEME
    assert "border-radius: 24px" in THEME
    assert "0 30px 60px rgba(0, 0, 0, .35)" in THEME
    assert "border-radius: 16px" in THEME
    assert "--sp6: 24px" in ROOT and "--sp5: 20px" in ROOT
    assert "@media (max-width: 1100px)" in THEME
    assert "@media (max-width: 700px)" in THEME
    assert "overflow-x: hidden" in THEME


def test_theme_is_used_by_shell_and_login_without_lavender_stylesheet():
    base = Path("dashboard/templates/base.html").read_text()
    login = Path("dashboard/templates/login.html").read_text()
    denied = Path("dashboard/templates/denied.html").read_text()
    assert 'static_base }}/theme.css' in base
    assert 'static_base }}/theme.css' in login
    assert 'static_base }}/theme.css' in denied
    assert "light-theme.css" not in base + login + denied
    assert not Path("dashboard/static/light-theme.css").exists()
    assert not re.search(r"lavender|purple", THEME, re.I)

