from utils.observability import estimate_cost_usd
from config import CLAUDE_MODEL


def test_unknown_model_cost_is_none():
    assert estimate_cost_usd("not-a-real-model", 1000, 1000) is None


def test_known_model_cost_is_estimated_not_zero():
    # Over 100k prompt tokens: Haiku 5.5 input is $0.50 per million.
    cost = estimate_cost_usd(CLAUDE_MODEL, 1_000_000, 0)
    assert cost is not None
    assert cost == 0.50


def test_haiku_short_prompt_uses_the_lower_rate():
    # 100k prompt tokens is still the short tier: $0.10 in, $0.50 out.
    cost = estimate_cost_usd(CLAUDE_MODEL, 100_000, 1_000_000)
    assert cost == 0.51
