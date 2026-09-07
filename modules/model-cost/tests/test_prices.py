"""Price table: longest-match wins, estimate math, unknown = no guess."""

import model_cost.prices as prices


def test_longest_key_wins():
    # 'gpt-4o-mini' must not be captured by the shorter 'gpt-4o'.
    assert prices.price_for("gpt-4o-mini-2024-07-18")["input"] == 0.15
    assert prices.price_for("gpt-4o-2024-08-06")["input"] == 2.5


def test_claude_families_match():
    assert prices.price_for("claude-opus-4-6")["output"] == 75.0
    assert prices.price_for("claude-sonnet-4-5")["output"] == 15.0
    assert prices.price_for("claude-haiku-4-5")["input"] == 0.8


def test_unknown_model_is_none():
    assert prices.price_for("some-random-model") is None
    assert prices.estimate_cost("some-random-model", {"input": 1e6, "output": 1e6}) is None


def test_estimate_cost_math():
    # 1M input @ $3 + 1M output @ $15 = $18 for a sonnet-class model.
    cost = prices.estimate_cost("claude-sonnet-4-5", {"input": 1_000_000, "output": 1_000_000})
    assert cost == 18.0


def test_estimate_includes_cache_tokens():
    # 1M cache_read @ $0.3 for sonnet = $0.30.
    cost = prices.estimate_cost("claude-sonnet-4-5", {"cache_read": 1_000_000})
    assert cost == 0.3
