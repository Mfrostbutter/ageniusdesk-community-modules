"""Price table: longest-match wins, estimate math, unknown = no guess."""

import tokenpulse.prices as prices


def test_longest_key_wins():
    assert prices.price_for("gpt-4o-mini-2024-07-18")["input"] == 0.15
    assert prices.price_for("gpt-4o-2024-08-06")["input"] == 2.5


def test_claude_families_match():
    assert prices.price_for("claude-opus-4-6")["output"] == 75.0
    assert prices.price_for("claude-sonnet-4-5")["output"] == 15.0


def test_unknown_model_is_none():
    assert prices.price_for("mystery-model") is None
    assert prices.estimate_cost("mystery-model", {"input": 1e6}) is None


def test_estimate_cost_math():
    assert prices.estimate_cost("claude-sonnet-4-5", {"input": 1_000_000, "output": 1_000_000}) == 18.0
