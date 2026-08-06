# utils/claude_helpers.py
"""
Shared helpers for working with Claude API responses.
"""


def get_text(response) -> str:
    """
    Safely extract the text content from a Claude response.

    response.content[0] is not always a text block — if extended thinking
    is active, content[0] can be a ThinkingBlock, which has no .text
    attribute and raises AttributeError on blind indexing. This walks the
    content list and returns the first real text block instead.
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""
