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


def test_get_text_and_finish_reason_from_chat_completion_shape():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="hello"),
                finish_reason="length",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=4,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        ),
    )
    assert get_text(response) == "hello"
    assert finish_reason(response) == "length"
    assert usage_totals(response) == (10, 4)
    assert cached_tokens(response) == 3
