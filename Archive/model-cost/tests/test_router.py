"""Router: card JSON, fleet-health status, settings, operator gate on refresh."""

import asyncio
import importlib
import types

import pytest
from fastapi import HTTPException

import model_cost.client as client

# The package __init__ binds the name `router` to the APIRouter object, so the
# `router` attribute shadows the submodule; import_module returns the real module.
router = importlib.import_module("model_cost.router")


def _req(role=""):
    return types.SimpleNamespace(headers={"x-agd-role": role} if role else {})


def _data(providers, models):
    return {"providers": providers, "models": models, "generated_at": 1_700_000_000}


def _patch_collect(monkeypatch, data):
    async def fake(force=False):
        return data
    monkeypatch.setattr(client, "collect", fake)


ANTH = {
    "id": "anthropic", "name": "Anthropic", "reachable": True, "error": None, "configured": True,
    "currency": "USD", "cost": {"today": {"amount": 2.0, "basis": "actual"}, "mtd": {"amount": 40.0, "basis": "actual"}},
    "tokens": {"today": 1, "mtd": 2}, "model_count": 1,
}
OR = {
    "id": "openrouter", "name": "OpenRouter", "reachable": True, "error": None, "configured": True,
    "currency": "USD", "cost": {"30d": {"amount": 12.0, "basis": "actual"}},
    "tokens": {"30d": 3}, "model_count": 1,
}
MODELS = [
    {"source": "anthropic", "source_name": "Anthropic", "model": "claude-opus-4", "window": "mtd",
     "input_tokens": 1e6, "output_tokens": 5e5, "cache_tokens": 0, "tokens": 1.5e6, "cost": 40.0,
     "cost_basis": "estimated", "currency": "USD"},
    {"source": "openrouter", "source_name": "OpenRouter", "model": "openai/gpt-4o", "window": "30d",
     "input_tokens": 1e4, "output_tokens": 2e3, "cache_tokens": 0, "tokens": 1.2e4, "cost": 12.0,
     "cost_basis": "actual", "currency": "USD"},
]


def test_card_shape(monkeypatch):
    _patch_collect(monkeypatch, _data([ANTH, OR], MODELS))
    card = asyncio.run(router.dashboard_card())
    labels = [m["label"] for m in card["metrics"]]
    assert "Today" in labels and "Month" in labels and "OpenRouter 30d" in labels
    # top models list, most expensive first, currency-formatted
    assert card["rows"][0]["value"] == "$40.00"
    assert card["rows"][0]["sub"].endswith("est")
    assert card["link"] == "community:model-cost"


def test_card_unconfigured(monkeypatch):
    off = {**ANTH, "configured": False, "reachable": False}
    _patch_collect(monkeypatch, _data([off], []))
    card = asyncio.run(router.dashboard_card())
    assert card["metrics"] == [] and card["rows"] == []
    assert "No providers configured" in card["footer"]


def test_fleet_health_ok(monkeypatch):
    _patch_collect(monkeypatch, _data([ANTH, OR], MODELS))
    out = asyncio.run(router.fleet_health())
    row = out["rows"][0]
    assert row["status"] == "ok" and row["reachable"] is True
    # month = sum of providers' actual MTD; OpenRouter reports 30d only, contributes 0 to MTD.
    assert row["metrics"][1]["value"] == "$40.00"


def test_fleet_health_degraded(monkeypatch):
    down = {**OR, "reachable": False, "error": "activity needs a provisioning key"}
    _patch_collect(monkeypatch, _data([ANTH, down], MODELS))
    out = asyncio.run(router.fleet_health())
    assert out["rows"][0]["status"] == "degraded"


def test_fleet_health_unconfigured(monkeypatch):
    off = {**ANTH, "configured": False, "reachable": False}
    _patch_collect(monkeypatch, _data([off], []))
    out = asyncio.run(router.fleet_health())
    assert out["rows"][0]["status"] == "unconfigured"
    assert out["rows"][0]["reachable"] is False


def test_refresh_requires_operator(monkeypatch):
    _patch_collect(monkeypatch, _data([ANTH], MODELS))
    monkeypatch.setattr(client, "invalidate", lambda: None)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(router.refresh(_req("viewer")))
    assert ei.value.status_code == 403
    # operator passes
    out = asyncio.run(router.refresh(_req("operator")))
    assert out["ok"] is True


def test_settings_lists_providers(monkeypatch):
    async def grant(endpoint):
        return {"status": "active" if endpoint == "anthropic" else "pending", "host": "api"}
    monkeypatch.setattr(router._host, "http_grant", grant)
    out = asyncio.run(router.get_settings())
    by_id = {p["id"]: p for p in out["providers"]}
    assert by_id["anthropic"]["status"] == "active"
    assert by_id["openai"]["status"] == "pending"
    assert len(out["providers"]) == 3
