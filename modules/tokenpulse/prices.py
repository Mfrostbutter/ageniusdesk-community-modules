"""Derived per-model cost price table.

The provider COST endpoints report a total, not a per-model breakdown; only their
USAGE endpoints break down by model (tokens). So for Anthropic and OpenAI the
per-model cost here is DERIVED (tokens x these prices) and always labelled
estimated, reconciled in the UI against the provider's actual total. OpenRouter
reports real per-model spend and never uses this table.

Prices are USD per MILLION tokens, keyed by a model-name substring. Longest
matching key wins so 'claude-opus-5' beats 'opus' and 'gpt-4o-mini' beats
'gpt-4o'. An unknown model costs zero rather than being guessed at (its tokens
still show, flagged unpriced). Cache-aware: Anthropic prices cache reads at 0.1x
input and splits 5-minute (1.25x) vs 1-hour (2x) cache writes; OpenAI cached
input is its own rate and has no separate cache-write charge.

Prices drift, the estimate is a signal not an invoice. Verified against the
public price pages 2026-09-09. Override by editing this file.
"""

from __future__ import annotations

from typing import Dict, Optional

# ── Anthropic (input, output, cache_read, cache_write_5m, cache_write_1h) ────────
# Cache: read 0.1x input, 5m write 1.25x, 1h write 2x (0.025x read on Fable 5.1).
_A_OPUS = {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write_5m": 6.25, "cache_write_1h": 10.0}
_A_OPUS_LEGACY = {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write_5m": 18.75, "cache_write_1h": 30.0}
_A_SONNET5 = {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write_5m": 2.5, "cache_write_1h": 4.0}
_A_SONNET4 = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write_5m": 3.75, "cache_write_1h": 6.0}
_A_HAIKU45 = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0}
_A_HAIKU35 = {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write_5m": 1.0, "cache_write_1h": 1.6}
_A_FABLE51 = {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write_5m": 12.5, "cache_write_1h": 20.0}
_A_FABLE5 = {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write_5m": 12.5, "cache_write_1h": 20.0}

# ── OpenAI (input, output, cache_read). No separate cache-write charge. ──────────
_O_GPT51 = {"input": 1.25, "output": 10.0, "cache_read": 0.125}
_O_GPT5 = {"input": 1.25, "output": 10.0, "cache_read": 0.125}
_O_GPT5_MINI = {"input": 0.25, "output": 2.0, "cache_read": 0.025}
_O_GPT5_NANO = {"input": 0.05, "output": 0.4, "cache_read": 0.005}
_O_GPT41 = {"input": 2.0, "output": 8.0, "cache_read": 0.5}
_O_GPT41_MINI = {"input": 0.4, "output": 1.6, "cache_read": 0.1}
_O_GPT41_NANO = {"input": 0.1, "output": 0.4, "cache_read": 0.025}
_O_GPT4O = {"input": 2.5, "output": 10.0, "cache_read": 1.25}
_O_GPT4O_MINI = {"input": 0.15, "output": 0.6, "cache_read": 0.075}
_O_O3 = {"input": 2.0, "output": 8.0, "cache_read": 0.5}
_O_O4_MINI = {"input": 1.1, "output": 4.4, "cache_read": 0.275}
_O_O1 = {"input": 15.0, "output": 60.0, "cache_read": 7.5}
_O_O1_MINI = {"input": 1.1, "output": 4.4}

PRICES: Dict[str, Dict[str, float]] = {
    # Anthropic — dash-versioned ids as the usage report returns them.
    "claude-opus-5": _A_OPUS,
    "claude-opus-4-8": _A_OPUS, "claude-opus-4-7": _A_OPUS,
    "claude-opus-4-6": _A_OPUS, "claude-opus-4-5": _A_OPUS,
    "claude-opus-4-1": _A_OPUS_LEGACY, "claude-opus-4": _A_OPUS_LEGACY,
    "claude-sonnet-5": _A_SONNET5,
    "claude-sonnet-4-6": _A_SONNET4, "claude-sonnet-4-5": _A_SONNET4, "claude-sonnet-4": _A_SONNET4,
    "claude-haiku-4-5": _A_HAIKU45, "claude-haiku-3-5": _A_HAIKU35,
    "claude-fable-5-1": _A_FABLE51, "claude-fable-5": _A_FABLE5,
    # Bare family fallbacks (shortest keys, last resort) -> current generation.
    "opus": _A_OPUS, "sonnet": _A_SONNET4, "haiku": _A_HAIKU45,
    # OpenAI.
    "gpt-5.1": _O_GPT51, "gpt-5-mini": _O_GPT5_MINI, "gpt-5-nano": _O_GPT5_NANO, "gpt-5": _O_GPT5,
    "gpt-4.1-mini": _O_GPT41_MINI, "gpt-4.1-nano": _O_GPT41_NANO, "gpt-4.1": _O_GPT41,
    "gpt-4o-mini": _O_GPT4O_MINI, "gpt-4o": _O_GPT4O,
    "o4-mini": _O_O4_MINI, "o3": _O_O3, "o1-mini": _O_O1_MINI, "o1": _O_O1,
    "gpt-4-turbo": {"input": 10.0, "output": 30.0}, "gpt-3.5-turbo": {"input": 0.5, "output": 1.5},
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
    """Estimated USD for one model given a token breakdown {input, output,
    cache_read, cache_write_5m, cache_write_1h}. A legacy single 'cache_write'
    key is priced at the 5-minute rate. None when the model is unpriced, so the
    caller can flag it rather than report a false zero."""
    prices = price_for(model)
    if prices is None:
        return None

    def per_m(kind: str) -> float:
        return float(tokens.get(kind, 0.0) or 0.0) / 1_000_000.0

    cw5 = prices.get("cache_write_5m", prices.get("cache_write", 0.0))
    cw1 = prices.get("cache_write_1h", cw5)
    cost = (per_m("input") * prices.get("input", 0.0)
            + per_m("output") * prices.get("output", 0.0)
            + per_m("cache_read") * prices.get("cache_read", 0.0)
            + per_m("cache_write_5m") * cw5
            + per_m("cache_write_1h") * cw1
            + per_m("cache_write") * cw5)  # legacy single bucket -> 5m rate
    return round(cost, 6)
