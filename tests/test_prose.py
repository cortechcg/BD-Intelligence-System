from utils.prose import humanize_draft


def test_strips_hr_and_em_dashes():
    raw = (
        "Cortech will lead the evaluation.\n"
        "---\n"
        "The assignment covers Somalia — including Puntland – and Kenya.\n"
        "----------\n"
    )
    out = humanize_draft(raw)
    assert "---" not in out
    assert "—" not in out
    assert "–" not in out
    assert "Somalia, including Puntland, and Kenya" in out


def test_dash_bullets_become_numbered():
    raw = "- First point\n- Second point\n\nNext paragraph."
    out = humanize_draft(raw)
    assert "- First" not in out
    assert "1. First point" in out
    assert "2. Second point" in out


def test_keeps_word_hyphens_and_table_separators():
    raw = (
        "| Project | Client |\n"
        "| --- | --- |\n"
        "| Child-protection study | SCI |\n"
    )
    out = humanize_draft(raw)
    assert "| --- | --- |" in out
    assert "Child-protection" in out
