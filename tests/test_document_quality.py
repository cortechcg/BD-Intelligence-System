from processors.document_quality import assess_extraction
from utils.errors import ErrorType


def test_rejects_empty():
    result = assess_extraction("")
    assert result["ok"] is False
    assert result["error_type"] == ErrorType.DOCUMENT_ERROR


def test_rejects_short():
    result = assess_extraction("hello world")
    assert result["ok"] is False


def test_rejects_nul():
    result = assess_extraction("A" * 250 + "\x00" + "B" * 50)
    assert result["ok"] is False
    assert "NUL" in result["reason"]


def test_rejects_almost_no_letters():
    result = assess_extraction("1234567890 " * 40)
    assert result["ok"] is False


def test_accepts_real_prose():
    text = (
        "Terms of Reference for an endline evaluation of a livelihoods "
        "programme in Somalia. The consultant shall deliver an inception "
        "report, a data collection plan, and a final evaluation report "
        "addressing OECD DAC criteria."
    ) * 3
    result = assess_extraction(text)
    assert result["ok"] is True


def test_rejects_google_access_wall():
    wall = (
        "Google Accounts\n"
        "Sign in\n"
        "accounts.google.com\n"
        "You need access. Request access from the owner.\n"
    ) * 8
    result = assess_extraction(wall)
    assert result["ok"] is False
    assert "sign-in" in result["reason"] or "access" in result["reason"]


def test_blurb_min_chars_override():
    blurb = "UNICEF Somalia MEL consultancy. Deadline 1 Dec 2026. " * 2
    assert len(blurb) < 200
    assert assess_extraction(blurb, min_chars=40)["ok"] is True
    assert assess_extraction(blurb, min_chars=200)["ok"] is False
