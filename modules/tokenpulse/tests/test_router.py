"""Router: card (tightest first), fleet-health thresholds, settings, operator gate."""

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


def _patch(monkeypatch, providers, quotas):
    data = {"providers": providers, "quotas": quotas, "trend": [], "budgets": {}, "generated_at": 1_700_000_000}

    async def fake(force=False):
        return data
    monkeypatch.setattr(client, "collect", fake)
    return data


P_OK = {"id": "openrouter", "name": "OpenRouter", "reachable": True, "configured": True, "error": None,
        "spend": {"mtd": 10.0}, "credits": None}
Q_HOT = {"scope": "limit", "provider": "openrouter", "provider_name": "OpenRouter", "label": "OpenRouter key limit",
         "used": 92.0, "limit": 100.0, "remaining": 8.0, "pct": 92.0, "unit": "usd", "basis": "limit", "resets_at": None}
Q_MID = {"scope": "monthly", "provider": "anthropic", "provider_name": "Anthropic", "label": "Anthropic budget",
         "used": 40.0, "limit": 100.0, "remaining": 60.0, "pct": 40.0, "unit": "usd", "basis": "budget", "resets_at": None}


def test_card_tightest_first(monkeypatch):
    _patch(monkeypatch, [P_OK], [Q_HOT, Q_MID])
    card = asyncio.run(router.dashboard_card())
    assert card["metrics"][0]["label"] == "OpenRouter key limit"
    assert card["metrics"][0]["value"] == "92%"
    assert card["rows"][0]["label"] == "Anthropic budget"
    assert card["link"] == "community:tokenpulse"


def test_card_unconfigured(monkeypatch):
    off = {**P_OK, "configured": False, "reachable": False}
    _patch(monkeypatch, [off], [])
    card = asyncio.run(router.dashboard_card())
    assert card["metrics"] == [] and "No providers configured" in card["footer"]


def test_card_no_quotas(monkeypatch):
    _patch(monkeypatch, [P_OK], [])
    card = asyncio.run(router.dashboard_card())
    assert "No quotas yet" in card["footer"]


def test_fleet_health_thresholds(monkeypatch):
    _patch(monkeypatch, [P_OK], [Q_HOT])
    out = asyncio.run(router.fleet_health())
    assert out["rows"][0]["status"] == "error"   # 92% >= 90
    _patch(monkeypatch, [P_OK], [{**Q_HOT, "pct": 80.0}])
    out = asyncio.run(router.fleet_health())
    assert out["rows"][0]["status"] == "degraded"  # 80% >= 75
    _patch(monkeypatch, [P_OK], [{**Q_HOT, "pct": 40.0}])
    out = asyncio.run(router.fleet_health())
    assert out["rows"][0]["status"] == "ok"


def test_fleet_health_unconfigured(monkeypatch):
    off = {**P_OK, "configured": False, "reachable": False}
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
    _patch(monkeypatch, [P_OK], [Q_HOT])
    monkeypatch.setattr(client, "invalidate", lambda: None)
    with pytest.raises(HTTPException):
        asyncio.run(router.refresh(_req("viewer")))
    out = asyncio.run(router.refresh(_req("operator")))
    assert out["ok"] is True
