"""Router: spend card, quotas card, fleet-health, settings, operator gate."""

import asyncio
import importlib
import types

import pytest
from fastapi import HTTPException

import tokenpulse.client as client

# __init__ binds `router` to the APIRouter, shadowing the submodule.
router = importlib.import_module("tokenpulse.router")


def _req(role=""):
    return types.SimpleNamespace(headers={"x-agd-role": role} if role else {})


def _patch(monkeypatch, providers, quotas, models=None):
    data = {"providers": providers, "quotas": quotas, "models": models or [],
            "trend": [], "budgets": {}, "generated_at": 1_700_000_000}

    async def fake(force=False):
        return data
    monkeypatch.setattr(client, "collect", fake)
    return data


ANTH = {"id": "anthropic", "name": "Anthropic", "reachable": True, "configured": True, "error": None,
        "spend": {"today": 2.0, "mtd": 40.0}, "credits": None, "quotas": [], "models": [], "trend": []}
OR = {"id": "openrouter", "name": "OpenRouter", "reachable": True, "configured": True, "error": None,
      "spend": {"30d": 12.0}, "credits": None, "quotas": [], "models": [], "trend": []}
MODELS = [
    {"source": "anthropic", "source_name": "Anthropic", "model": "claude-opus-4", "window": "mtd", "cost": 40.0, "cost_basis": "estimated"},
    {"source": "openrouter", "source_name": "OpenRouter", "model": "openai/gpt-4o", "window": "30d", "cost": 12.0, "cost_basis": "actual"},
]
Q_HOT = {"scope": "limit", "provider": "openrouter", "provider_name": "OpenRouter", "label": "OpenRouter key limit",
         "used": 92.0, "limit": 100.0, "remaining": 8.0, "pct": 92.0, "unit": "usd", "basis": "limit", "resets_at": None}


def test_spend_card(monkeypatch):
    _patch(monkeypatch, [ANTH, OR], [], MODELS)
    card = asyncio.run(router.dashboard_card())
    labels = [m["label"] for m in card["metrics"]]
    assert "Today" in labels and "Month" in labels
    assert card["rows"][0]["value"] == "$40.00"
    assert card["link"] == "community:tokenpulse"


def test_quotas_card_tightest_first(monkeypatch):
    q2 = {**Q_HOT, "label": "Anthropic budget", "pct": 30.0}
    _patch(monkeypatch, [ANTH, OR], [Q_HOT, q2])
    card = asyncio.run(router.dashboard_card_quotas())
    assert card["metrics"][0]["label"] == "OpenRouter key limit" and card["metrics"][0]["value"] == "92%"
    assert card["rows"][0]["label"] == "Anthropic budget"


def test_cards_unconfigured(monkeypatch):
    off = {**ANTH, "configured": False, "reachable": False}
    _patch(monkeypatch, [off], [])
    assert "No providers configured" in asyncio.run(router.dashboard_card())["footer"]
    assert "No providers configured" in asyncio.run(router.dashboard_card_quotas())["footer"]


def test_fleet_health_thresholds_and_spend(monkeypatch):
    _patch(monkeypatch, [ANTH, OR], [Q_HOT])
    out = asyncio.run(router.fleet_health())
    row = out["rows"][0]
    assert row["label"] == "AI Usage" and row["status"] == "error"  # 92%
    assert row["metrics"][1]["value"] == "$40.00"  # month spend
    _patch(monkeypatch, [ANTH], [{**Q_HOT, "pct": 40.0}])
    assert asyncio.run(router.fleet_health())["rows"][0]["status"] == "ok"


def test_fleet_health_unconfigured(monkeypatch):
    off = {**ANTH, "configured": False, "reachable": False}
    _patch(monkeypatch, [off], [])
    out = asyncio.run(router.fleet_health())
    assert out["rows"][0]["status"] == "unconfigured" and out["rows"][0]["reachable"] is False


def test_settings_post_requires_operator(monkeypatch):
    monkeypatch.setattr(client.state, "set_budgets", lambda b: {"anthropic": 100.0})
    monkeypatch.setattr(client, "invalidate", lambda: None)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(router.set_settings(router.BudgetPayload(budgets={"anthropic": 100}), _req("viewer")))
    assert ei.value.status_code == 403
    out = asyncio.run(router.set_settings(router.BudgetPayload(budgets={"anthropic": 100}), _req("operator")))
    assert out["ok"] is True


def test_refresh_requires_operator(monkeypatch):
    _patch(monkeypatch, [ANTH], [Q_HOT])
    monkeypatch.setattr(client, "invalidate", lambda: None)
    with pytest.raises(HTTPException):
        asyncio.run(router.refresh(_req("viewer")))
    assert asyncio.run(router.refresh(_req("operator")))["ok"] is True
