"""Budget persistence: roundtrip, clamping, merge."""

import tokenpulse.state as state


def test_default_budgets_are_zero():
    b = state.get_budgets()
    assert set(b) == {"anthropic", "openai", "openrouter"}
    assert all(v == 0.0 for v in b.values())


def test_set_and_get_roundtrip():
    state.set_budgets({"anthropic": 100, "openai": 50})
    b = state.get_budgets()
    assert b["anthropic"] == 100.0 and b["openai"] == 50.0
    # A partial update leaves the others intact.
    state.set_budgets({"openrouter": 25})
    b = state.get_budgets()
    assert b["anthropic"] == 100.0 and b["openrouter"] == 25.0


def test_negative_or_garbage_clears():
    state.set_budgets({"anthropic": -5, "openai": "x"})
    b = state.get_budgets()
    assert b["anthropic"] == 0.0 and b["openai"] == 0.0
