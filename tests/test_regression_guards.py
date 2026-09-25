"""Guards for two documented crash patterns.

1. response.content[0].text — a leading ThinkingBlock has no .text.
2. dict.get(key, default)[slice] — .get only substitutes when the key is
   missing; a present None still slices and raises TypeError.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from utils.claude_helpers import get_text

ROOT = Path(__file__).resolve().parents[1]

LLM_MODULES = (
    ROOT / "database" / "supabase_client.py",
    ROOT / "intelligence" / "analyzer.py",
    ROOT / "intelligence" / "budget_calculator.py",
    ROOT / "populate_airtable.py",
)


def test_get_text_survives_leading_thinking_block():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking"),
            SimpleNamespace(type="text", text="analysis json"),
        ]
    )
    assert get_text(response) == "analysis json"


def test_naive_content0_text_crashes_on_thinking_block():
    thinking = SimpleNamespace(type="thinking")
    response = SimpleNamespace(content=[thinking, SimpleNamespace(type="text", text="ok")])
    with pytest.raises(AttributeError):
        _ = response.content[0].text


def test_none_title_slice_uses_or_guard_not_get_default():
    result = {"title": None}
    assert (result.get("title") or "Unknown")[:50] == "Unknown"
    with pytest.raises(TypeError):
        _ = result.get("title", "Unknown")[:50]


def test_named_modules_do_not_reintroduce_crash_patterns():
    for path in LLM_MODULES:
        source = path.read_text()
        assert "response.content[0].text" not in source, path
        assert "from utils.claude_helpers import get_text" in source, path
    main = (ROOT / "main.py").read_text()
    # Discovery no longer slices a result title. The crash is the default
    # form: .get(key, default) still returns None when the key is present.
    assert "result.get('title', 'Unknown')" not in main
    assert "opp.get('title', 'Unknown')" not in main
    assert ".get('title', 'Unknown')[" not in main
