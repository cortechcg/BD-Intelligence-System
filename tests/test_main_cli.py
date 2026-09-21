"""CLI parsing must not fall through to the 6-hour scheduler."""
import pytest

from main import CliError, parse_main_argv, refuse_headless_scheduler

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


# ── headless-scheduler guard (2026-09-21 Render incident) ───────────────────

def test_scheduler_refuses_under_render_port_env():
    msg = refuse_headless_scheduler({"PORT": "10000"}, stdin_is_tty=True)
    assert msg and msg.startswith("REFUSING")
    assert "PORT" in msg
    assert "uvicorn dashboard.app:app" in msg and "python -m dashboard.worker" in msg


def test_scheduler_refuses_under_render_marker_even_with_a_tty():
    assert refuse_headless_scheduler({"RENDER": "true"}, stdin_is_tty=True)
    assert refuse_headless_scheduler({"RAILWAY_ENVIRONMENT": "production"}, stdin_is_tty=True)


def test_scheduler_refuses_without_a_terminal():
    msg = refuse_headless_scheduler({}, stdin_is_tty=False)
    assert msg and "not a TTY" in msg


def test_scheduler_allowed_only_in_an_interactive_local_terminal():
    assert refuse_headless_scheduler({}, stdin_is_tty=True) is None


def test_scheduler_override_is_explicit_and_exact():
    assert refuse_headless_scheduler({"PORT": "10000", "CORTECH_ALLOW_SCHEDULER": "1"}, stdin_is_tty=False) is None
    # "true", "yes" etc. do not count — the override must be deliberate.
    assert refuse_headless_scheduler({"PORT": "10000", "CORTECH_ALLOW_SCHEDULER": "true"}, stdin_is_tty=False)


def test_guard_does_not_touch_the_flagged_modes():
    """--once / --submit-url / --run-* are how systemd and the worker call in;
    the guard is wired only into the bare 'scheduler' branch."""
    import inspect, main
    src = inspect.getsource(main)
    tail = src[src.index('if __name__ == "__main__":'):]
    assert tail.count("refuse_headless_scheduler()") == 1
    branch = tail[tail.index("else:\n        refusal"):]
    assert "start_scheduler" in branch
    for mode in ("submit", "once", "assortis", "deadline", "winloss", "market", "relationships"):
        assert f'mode == "{mode}"' in tail
