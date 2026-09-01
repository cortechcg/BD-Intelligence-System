from types import SimpleNamespace

from utils.llm import (
    cached_tokens,
    finish_reason,
    get_text,
    system_text,
    usage_totals,
)


def test_system_text_concatenates_blocks_and_drops_cache_control():
    blocks = [
        {"type": "text", "text": "pack", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "guidance"},
    ]
    assert system_text(blocks) == "pack\n\nguidance"
    assert system_text("plain") == "plain"
    assert system_text(None) is None


def test_get_text_and_finish_reason_from_anthropic_shape():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="internal"),
            SimpleNamespace(type="text", text="hello"),
        ],
        stop_reason="max_tokens",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=4,
            cache_read_input_tokens=3,
        ),
    )
    assert get_text(response) == "hello"
    assert finish_reason(response) == "max_tokens"
    assert usage_totals(response) == (10, 4)
    assert cached_tokens(response) == 3
