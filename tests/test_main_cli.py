"""CLI parsing must not fall through to the 6-hour scheduler."""
import pytest

from main import CliError, parse_main_argv

DRIVE = (
    "https://drive.google.com/file/d/1yEJQkvzDvK5wrf6969DOlMAx_RV54Ldz/"
    "view?usp=sharing"
)


def test_submit_dash_url_does_not_start_scheduler():
    parsed = parse_main_argv(["main.py", "--submit", "-url", DRIVE])
    assert parsed == {"mode": "submit", "url": DRIVE}


def test_submit_url_flag():
    parsed = parse_main_argv(["main.py", "--submit-url", DRIVE])
    assert parsed["mode"] == "submit"
    assert parsed["url"] == DRIVE


def test_submit_url_equals():
    parsed = parse_main_argv(["main.py", f"--submit-url={DRIVE}"])
    assert parsed == {"mode": "submit", "url": DRIVE}


def test_submit_then_positional_url():
    parsed = parse_main_argv(["main.py", "--submit", DRIVE])
    assert parsed == {"mode": "submit", "url": DRIVE}


def test_incomplete_submit_raises_instead_of_scheduler():
    with pytest.raises(CliError, match="--submit-url"):
        parse_main_argv(["main.py", "--submit"])
    with pytest.raises(CliError, match="--submit-url"):
        parse_main_argv(["main.py", "--submit", "-url"])
    with pytest.raises(CliError, match="--submit-url"):
        parse_main_argv(["main.py", "--submit-url"])


def test_unknown_flag_does_not_start_scheduler():
    with pytest.raises(CliError, match="Unknown option"):
        parse_main_argv(["main.py", "--bogus"])


def test_empty_argv_is_scheduler():
    assert parse_main_argv(["main.py"]) == {"mode": "scheduler"}


def test_once_and_help():
    assert parse_main_argv(["main.py", "--once"]) == {"mode": "once"}
    assert parse_main_argv(["main.py", "--help"]) == {"mode": "help"}
