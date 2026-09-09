"""Price table: longest-match wins, estimate math, unknown = no guess."""

import tokenpulse.prices as prices


def test_longest_key_wins():
    assert prices.price_for("gpt-4o-mini-2024-07-18")["input"] == 0.15
    assert prices.price_for("gpt-4o-2024-08-06")["input"] == 2.5


def test_current_generation_prices():
    assert prices.price_for("claude-opus-5")["input"] == 5.0
    assert prices.price_for("claude-opus-5")["output"] == 25.0
    assert prices.price_for("claude-sonnet-5")["input"] == 2.0
    assert prices.price_for("claude-sonnet-5")["output"] == 10.0
    assert prices.price_for("claude-haiku-4-5-20251001")["input"] == 1.0
    # Opus 4.6 is current-gen pricing now, not the legacy $15/$75.
    assert prices.price_for("claude-opus-4-6")["output"] == 25.0
    # Retired generation keeps its higher list price.
    assert prices.price_for("claude-opus-4-1")["output"] == 75.0
    assert prices.price_for("gpt-5-mini")["input"] == 0.25
    assert prices.price_for("gpt-5")["output"] == 10.0


def test_unknown_model_is_none():
    assert prices.price_for("mystery-model") is None
    assert prices.estimate_cost("mystery-model", {"input": 1e6}) is None


def test_estimate_cost_math():
    assert prices.estimate_cost("claude-sonnet-4-5", {"input": 1_000_000, "output": 1_000_000}) == 18.0


def test_cache_aware_split():
    # Opus 5: read 0.5, 5m write 6.25, 1h write 10 per MTok.
    cost = prices.estimate_cost("claude-opus-5", {
        "input": 1_000_000, "output": 0, "cache_read": 1_000_000,
        "cache_write_5m": 1_000_000, "cache_write_1h": 1_000_000})
    assert cost == 5.0 + 0.5 + 6.25 + 10.0
    # Legacy single cache_write bucket is priced at the 5m rate.
    assert prices.estimate_cost("claude-opus-5", {"cache_write": 1_000_000}) == 6.25


def test_openai_cached_input_rate():
    # gpt-4o: input 2.5, cached input 1.25; no cache-write charge.
    assert prices.estimate_cost("gpt-4o", {"input": 1_000_000, "cache_read": 1_000_000}) == 3.75
