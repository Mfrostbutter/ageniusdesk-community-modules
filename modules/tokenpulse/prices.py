"""Derived per-model cost price table.

The provider COST endpoints report a total, not a per-model breakdown; only their
USAGE endpoints break down by model (tokens). So for Anthropic and OpenAI the
per-model cost here is DERIVED (tokens x these prices) and always labelled
estimated, reconciled in the UI against the provider's actual total. OpenRouter
reports real per-model spend and never uses this table.

Prices are USD per MILLION tokens, keyed by a model-name substring. An unknown
model costs zero rather than being guessed at (its tokens still show). Longest
matching key wins so 'gpt-4o-mini' beats 'gpt-4o'. Override by editing this file;
prices drift, the estimate is a signal, not an invoice.
"""

from __future__ import annotations

from typing import Dict, Optional

# input/output always; cache_read/cache_write optional (Anthropic reports them).
PRICES: Dict[str, Dict[str, float]] = {
    # Anthropic (Claude)
    "claude-opus": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    # Bare family aliases (usage rows sometimes report just the family).
    "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "haiku": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    # OpenAI. cache_read priced at half input where the model supports cached input.
    "gpt-4o-mini": {"input": 0.15, "output": 0.6, "cache_read": 0.075},
    "gpt-4o": {"input": 2.5, "output": 10.0, "cache_read": 1.25},
    "gpt-4.1-mini": {"input": 0.4, "output": 1.6, "cache_read": 0.1},
    "gpt-4.1-nano": {"input": 0.1, "output": 0.4, "cache_read": 0.025},
    "gpt-4.1": {"input": 2.0, "output": 8.0, "cache_read": 0.5},
    "o4-mini": {"input": 1.1, "output": 4.4, "cache_read": 0.275},
    "o3-mini": {"input": 1.1, "output": 4.4, "cache_read": 0.55},
    "o3": {"input": 2.0, "output": 8.0, "cache_read": 0.5},
    "o1-mini": {"input": 1.1, "output": 4.4},
    "o1": {"input": 15.0, "output": 60.0, "cache_read": 7.5},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    "gpt-3.5-turbo": {"input": 0.5, "output": 1.5},
}

_KEYS_BY_LEN = sorted(PRICES, key=len, reverse=True)


def price_for(model: str) -> Optional[Dict[str, float]]:
    """Prices for a model, matched by the longest key that is a substring of the
    (lowercased) model name. None when unknown."""
    lowered = (model or "").lower()
    for key in _KEYS_BY_LEN:
        if key in lowered:
            return PRICES[key]
    return None


def estimate_cost(model: str, tokens: Dict[str, float]) -> Optional[float]:
    """Estimated USD for one model given a token breakdown
    {input, output, cache_read?, cache_write?}. None when the model is unpriced,
    so the caller can flag it rather than report a false zero."""
    prices = price_for(model)
    if prices is None:
        return None
    cost = 0.0
    for kind, per_million in (("input", prices.get("input", 0.0)),
                              ("output", prices.get("output", 0.0)),
                              ("cache_read", prices.get("cache_read", 0.0)),
                              ("cache_write", prices.get("cache_write", 0.0))):
        cost += float(tokens.get(kind, 0.0) or 0.0) / 1_000_000.0 * per_million
    return round(cost, 6)
