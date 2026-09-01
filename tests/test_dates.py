from utils.dates import days_until, parse_deadline


def test_iso():
    assert parse_deadline("2026-03-15") == "2026-03-15"
    assert parse_deadline("2026-03-15T12:00:00Z") == "2026-03-15"


def test_dmy():
    assert parse_deadline("15/03/2026") == "2026-03-15"
    assert parse_deadline("15-03-2026") == "2026-03-15"


def test_named_months():
    assert parse_deadline("15 March 2026") == "2026-03-15"
    assert parse_deadline("March 15, 2026") == "2026-03-15"
    assert parse_deadline("15th March 2026") == "2026-03-15"


def test_unknown():
    assert parse_deadline(None) is None
    assert parse_deadline("TBD") is None
    assert parse_deadline("unknown") is None
    assert parse_deadline("not a date") is None
    assert parse_deadline("2026-13-99") is None


def test_days_until_known_future(monkeypatch):
    from datetime import datetime, timezone

    today = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert days_until("2026-01-11", today=today) == 10
