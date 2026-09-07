"""Provider polling: budget gauges, credit/limit quotas, trend, degrade."""

import asyncio
import datetime as dt
import json

import tokenpulse.client as client


def _resp(payload, status=200):
    return {"status": status, "headers": {}, "body": json.dumps(payload), "truncated": False}


def _today():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def test_anthropic_budget_gauge_and_trend(monkeypatch):
    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        return _resp({"data": [
            {"starting_at": f"{_today()}T00:00:00Z", "results": [{"amount": "5000", "currency": "USD"}]},
        ], "has_more": False})
    monkeypatch.setattr(client._host, "http_request", http)
    summary, quotas, trend = asyncio.run(client._poll_anthropic(budget=100.0))
    assert summary["reachable"] is True
    assert summary["spend"]["mtd"] == 50.0            # 5000 cents
    q = quotas[0]
    assert q["basis"] == "budget" and q["limit"] == 100.0 and q["used"] == 50.0 and q["pct"] == 50.0
    assert q["resets_at"] and trend and trend[0]["amount"] == 50.0


def test_anthropic_no_budget_no_quota(monkeypatch):
    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        return _resp({"data": [], "has_more": False})
    monkeypatch.setattr(client._host, "http_request", http)
    _, quotas, _ = asyncio.run(client._poll_anthropic(budget=0.0))
    assert quotas == []


def test_openrouter_key_limit_and_credits(monkeypatch):
    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        if path == "/key":
            return _resp({"data": {"label": "prod", "usage": 20.0, "usage_daily": 2.0,
                                   "usage_weekly": 10.0, "usage_monthly": 18.0,
                                   "limit": 100.0, "limit_remaining": 80.0}})
        if path == "/credits":
            return _resp({"data": {"total_credits": 200.0, "total_usage": 60.0}})
        raise AssertionError(path)
    monkeypatch.setattr(client._host, "http_request", http)
    summary, quotas, _ = asyncio.run(client._poll_openrouter(budget=0.0))

    assert summary["spend"]["mtd"] == 18.0
    assert summary["credits"]["remaining"] == 140.0
    kinds = {q["basis"]: q for q in quotas}
    assert kinds["limit"]["used"] == 20.0 and kinds["limit"]["limit"] == 100.0 and kinds["limit"]["pct"] == 20.0
    assert kinds["balance"]["limit"] == 200.0 and kinds["balance"]["used"] == 60.0 and kinds["balance"]["pct"] == 30.0


def test_openrouter_credits_optional(monkeypatch):
    # An inference key: /key works, /credits 4xxs. Credits quota simply absent.
    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        if path == "/key":
            return _resp({"data": {"usage": 5.0, "usage_monthly": 5.0, "limit": 50.0, "limit_remaining": 45.0}})
        return _resp({"error": {"message": "needs a provisioning key"}}, status=403)
    monkeypatch.setattr(client._host, "http_request", http)
    summary, quotas, _ = asyncio.run(client._poll_openrouter(budget=0.0))
    assert summary["credits"] is None
    assert [q["basis"] for q in quotas] == ["limit"]


def test_collect_sorts_loudest_first_and_degrades(monkeypatch):
    client.invalidate()

    async def grant(endpoint):
        return {"status": "active"}
    monkeypatch.setattr(client._host, "http_grant", grant)
    monkeypatch.setattr(client.state, "get_budgets", lambda: {"anthropic": 100.0, "openai": 0.0, "openrouter": 0.0})

    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        if endpoint == "anthropic":
            return _resp({"data": [{"starting_at": f"{_today()}T00:00:00Z",
                                    "results": [{"amount": "9500"}]}], "has_more": False})  # $95 of $100 -> 95%
        if endpoint == "openai":
            raise client.ProviderError("HTTP 500: boom")
        return _resp({"data": {"usage": 10.0, "usage_monthly": 10.0, "limit": 100.0, "limit_remaining": 90.0}})  # 10%
    monkeypatch.setattr(client._host, "http_request", http)

    data = asyncio.run(client.collect(force=True))
    by_id = {p["id"]: p for p in data["providers"]}
    assert by_id["openai"]["reachable"] is False and "boom" in by_id["openai"]["error"]
    # loudest first: 95% anthropic budget before 10% openrouter limit
    assert data["quotas"][0]["pct"] == 95.0
    assert data["quotas"][0]["provider"] == "anthropic"


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
    assert called == []
    assert all(not p["configured"] for p in data["providers"])
