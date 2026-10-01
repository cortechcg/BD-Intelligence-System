from utils.observability import estimate_cost_usd
from config import CLAUDE_MODEL


def test_unknown_model_cost_is_none():
    assert estimate_cost_usd("not-a-real-model", 1000, 1000) is None


def test_known_model_cost_is_estimated_not_zero():
    cost = estimate_cost_usd(CLAUDE_MODEL, 1_000_000, 0)
    assert cost is not None
    assert cost == 1.00
