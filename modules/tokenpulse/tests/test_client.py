"""Consolidated polling: per-provider spend, per-model cost, quotas, credits, trend."""

import asyncio
import datetime as dt
import json

import tokenpulse.client as client


def _resp(payload, status=200):
    return {"status": status, "headers": {}, "body": json.dumps(payload), "truncated": False}


def _today():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _today_epoch():
    return int(dt.datetime.now(dt.timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0).timestamp())


def _dispatch(table):
    async def fake(endpoint, method="GET", path="", query=None, headers=None, body=None):
        return table[path]
    return fake


def test_anthropic_provider_has_spend_models_quota_trend(monkeypatch):
    table = {
        "/v1/organizations/cost_report": _resp({"data": [
            {"starting_at": f"{_today()}T00:00:00Z", "results": [{"amount": "5000"}]}], "has_more": False}),
        "/v1/organizations/usage_report/messages": _resp({"data": [
            {"starting_at": f"{_today()}T00:00:00Z", "results": [
                {"model": "claude-opus-4", "uncached_input_tokens": 1_000_000, "output_tokens": 500_000}]}], "has_more": False}),
    }
    monkeypatch.setattr(client._host, "http_request", _dispatch(table))
    p = asyncio.run(client._poll_anthropic(budget=100.0))
    assert p["spend"]["mtd"] == 50.0
    assert p["quotas"][0]["basis"] == "budget" and p["quotas"][0]["pct"] == 50.0
    mtd_model = next(m for m in p["models"] if m["window"] == "mtd")
    assert mtd_model["cost"] == 52.5 and mtd_model["cost_basis"] == "estimated"
    assert p["trend"] and p["trend"][0]["amount"] == 50.0


def test_openai_splits_cached_and_emits_models(monkeypatch):
    te = _today_epoch()
    table = {
        "/v1/organization/costs": _resp({"data": [
            {"start_time": te, "results": [{"amount": {"value": 1.5}}]}], "has_more": False}),
        "/v1/organization/usage/completions": _resp({"data": [
            {"start_time": te, "results": [
                {"model": "gpt-4o", "input_tokens": 1_000_000, "output_tokens": 500_000, "input_cached_tokens": 200_000}]}], "has_more": False}),
    }
    monkeypatch.setattr(client._host, "http_request", _dispatch(table))
    p = asyncio.run(client._poll_openai(budget=0.0))
    assert p["spend"]["mtd"] == 1.5 and p["quotas"] == []
    m = next(x for x in p["models"] if x["window"] == "mtd")
    assert m["cost"] == 7.25 and m["cache_tokens"] == 200_000


def test_openrouter_key_credits_and_activity(monkeypatch):
    table = {
        "/key": _resp({"data": {"label": "prod", "usage": 20.0, "usage_daily": 2.0, "usage_weekly": 10.0,
                                "usage_monthly": 18.0, "limit": 100.0, "limit_remaining": 80.0}}),
        "/credits": _resp({"data": {"total_credits": 200.0, "total_usage": 60.0}}),
        "/activity": _resp({"data": [
            {"model": "anthropic/claude-opus", "usage": 10.0, "prompt_tokens": 1000, "completion_tokens": 2000},
            {"model": "openai/gpt-4o", "usage": 5.0, "prompt_tokens": 500, "completion_tokens": 100}]}),
    }
    monkeypatch.setattr(client._host, "http_request", _dispatch(table))
    p = asyncio.run(client._poll_openrouter(budget=0.0))
    assert p["spend"]["mtd"] == 18.0 and p["spend"]["30d"] == 15.0
    assert p["credits"]["remaining"] == 140.0
    bases = {q["basis"] for q in p["quotas"]}
    assert bases == {"limit", "balance"}
    assert {m["model"] for m in p["models"]} == {"anthropic/claude-opus", "openai/gpt-4o"}
    assert all(m["cost_basis"] == "actual" and m["window"] == "30d" for m in p["models"])


def test_openrouter_activity_optional(monkeypatch):
    async def fake(endpoint, method="GET", path="", query=None, headers=None, body=None):
        if path == "/key":
            return _resp({"data": {"usage": 5.0, "usage_monthly": 5.0, "limit": 50.0, "limit_remaining": 45.0}})
        return _resp({"error": {"message": "needs a provisioning key"}}, status=403)  # /credits and /activity
    monkeypatch.setattr(client._host, "http_request", fake)
    p = asyncio.run(client._poll_openrouter(budget=0.0))
    assert p["credits"] is None and p["models"] == []
    assert [q["basis"] for q in p["quotas"]] == ["limit"]


def test_collect_merges_sorts_and_degrades(monkeypatch):
    client.invalidate()

    async def grant(endpoint):
        return {"status": "active"}
    monkeypatch.setattr(client._host, "http_grant", grant)
    monkeypatch.setattr(client.state, "get_budgets", lambda: {"anthropic": 100.0, "openai": 0.0, "openrouter": 0.0})

    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        if endpoint == "anthropic":
            if path.endswith("cost_report"):
                return _resp({"data": [{"starting_at": f"{_today()}T00:00:00Z", "results": [{"amount": "9500"}]}], "has_more": False})
            return _resp({"data": [], "has_more": False})
        if endpoint == "openai":
            raise client.ProviderError("HTTP 500: boom")
        return _resp({"data": {"usage": 10.0, "usage_monthly": 10.0, "limit": 100.0, "limit_remaining": 90.0}}) if path == "/key" \
            else _resp({"error": {"message": "no"}}, status=403)
    monkeypatch.setattr(client._host, "http_request", http)

    data = asyncio.run(client.collect(force=True))
    by_id = {p["id"]: p for p in data["providers"]}
    assert by_id["openai"]["reachable"] is False and "boom" in by_id["openai"]["error"]
    assert data["quotas"][0]["pct"] == 95.0 and data["quotas"][0]["provider"] == "anthropic"  # loudest first


def test_unconfigured_not_polled(monkeypatch):
    client.invalidate()

    async def grant(endpoint):
        return {"status": "pending"}
    monkeypatch.setattr(client._host, "http_grant", grant)
    called = []

    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        called.append(endpoint)
        return _resp({"data": {}})
    monkeypatch.setattr(client._host, "http_request", http)
    data = asyncio.run(client.collect(force=True))
    assert called == [] and all(not p["configured"] for p in data["providers"])
