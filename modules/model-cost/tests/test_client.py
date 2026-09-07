"""Provider polling + per-model normalization, with _host.http_request mocked."""

import asyncio
import datetime as dt
import json

import model_cost.client as client


def _resp(payload, status=200):
    return {"status": status, "headers": {}, "body": json.dumps(payload), "truncated": False}


def _today():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _today_epoch():
    now = dt.datetime.now(dt.timezone.utc)
    return int(now.replace(hour=12, minute=0, second=0, microsecond=0).timestamp())


def _dispatch(table):
    async def fake(endpoint, method="GET", path="", query=None, headers=None, body=None):
        return table[path]
    return fake


def test_anthropic_normalizes_per_model(monkeypatch):
    table = {
        "/v1/organizations/cost_report": _resp({"data": [
            {"starting_at": f"{_today()}T00:00:00Z", "results": [{"amount": "12345", "currency": "USD"}]},
        ], "has_more": False}),
        "/v1/organizations/usage_report/messages": _resp({"data": [
            {"starting_at": f"{_today()}T00:00:00Z", "results": [
                {"model": "claude-opus-4", "uncached_input_tokens": 1_000_000, "output_tokens": 500_000},
            ]},
        ], "has_more": False}),
    }
    monkeypatch.setattr(client._host, "http_request", _dispatch(table))
    summary, models = asyncio.run(client._poll_anthropic())

    assert summary["reachable"] is True
    assert summary["cost"]["mtd"]["amount"] == 123.45      # cents string / 100
    assert summary["cost"]["mtd"]["basis"] == "actual"
    # today's bucket -> both windows present
    windows = {m["window"] for m in models}
    assert windows == {"mtd", "today"}
    opus = next(m for m in models if m["window"] == "mtd")
    assert opus["model"] == "claude-opus-4"
    assert opus["cost_basis"] == "estimated"
    # 1M in @ $15 + 0.5M out @ $75 = 52.5
    assert opus["cost"] == 52.5


def test_openai_splits_cached_input(monkeypatch):
    te = _today_epoch()
    table = {
        "/v1/organization/costs": _resp({"data": [
            {"start_time": te, "results": [{"amount": {"value": 1.5, "currency": "USD"}}]},
        ], "has_more": False}),
        "/v1/organization/usage/completions": _resp({"data": [
            {"start_time": te, "results": [
                {"model": "gpt-4o", "input_tokens": 1_000_000, "output_tokens": 500_000,
                 "input_cached_tokens": 200_000},
            ]},
        ], "has_more": False}),
    }
    monkeypatch.setattr(client._host, "http_request", _dispatch(table))
    summary, models = asyncio.run(client._poll_openai())

    assert summary["cost"]["mtd"]["amount"] == 1.5
    m = next(x for x in models if x["window"] == "mtd")
    # uncached 0.8M @ $2.5 + 0.5M out @ $10 + 0.2M cache_read @ $1.25 = 2.0 + 5.0 + 0.25
    assert m["cost"] == 7.25
    assert m["cache_tokens"] == 200_000


def test_openrouter_actual_per_model(monkeypatch):
    async def fake(endpoint, method="GET", path="", query=None, headers=None, body=None):
        return _resp({"data": [
            {"model": "anthropic/claude-opus", "usage": 10.0, "prompt_tokens": 1000, "completion_tokens": 2000},
            {"model": "openai/gpt-4o", "usage": 5.0, "prompt_tokens": 500, "completion_tokens": 100},
        ]})
    monkeypatch.setattr(client._host, "http_request", fake)
    summary, models = asyncio.run(client._poll_openrouter())

    assert summary["cost"]["30d"]["amount"] == 15.0
    assert {m["model"] for m in models} == {"anthropic/claude-opus", "openai/gpt-4o"}
    assert all(m["cost_basis"] == "actual" and m["window"] == "30d" for m in models)
    top = max(models, key=lambda m: m["cost"])
    assert top["cost"] == 10.0


def test_openrouter_inference_key_degrades(monkeypatch):
    # /activity without a management key returns something without a rows list.
    async def fake(endpoint, method="GET", path="", query=None, headers=None, body=None):
        return _resp({"data": {"label": "sk-...", "usage": 3.2}})  # object, not a list
    monkeypatch.setattr(client._host, "http_request", fake)
    try:
        asyncio.run(client._poll_openrouter())
        assert False, "expected ProviderError"
    except client.ProviderError as e:
        assert "provisioning key" in str(e)


def test_collect_merges_and_degrades(monkeypatch):
    client.invalidate()

    async def grant(endpoint):
        return {"status": "active", "methods": ["GET"], "host": "x", "mutating": False}
    monkeypatch.setattr(client._host, "http_grant", grant)

    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        if endpoint == "anthropic":
            raise client.ProviderError("HTTP 500: boom")
        if endpoint == "openai":
            table = {
                "/v1/organization/costs": _resp({"data": [], "has_more": False}),
                "/v1/organization/usage/completions": _resp({"data": [], "has_more": False}),
            }
            return table[path]
        return _resp({"data": [{"model": "x/y", "usage": 2.0, "prompt_tokens": 10, "completion_tokens": 5}]})
    monkeypatch.setattr(client._host, "http_request", http)

    data = asyncio.run(client.collect(force=True))
    by_id = {p["id"]: p for p in data["providers"]}
    assert by_id["anthropic"]["reachable"] is False and "boom" in by_id["anthropic"]["error"]
    assert by_id["openai"]["reachable"] is True
    assert by_id["openrouter"]["reachable"] is True
    assert any(m["source"] == "openrouter" for m in data["models"])


def test_unconfigured_provider_is_not_polled(monkeypatch):
    client.invalidate()
    calls = []

    async def grant(endpoint):
        return {"status": "pending" if endpoint == "openai" else "active"}
    monkeypatch.setattr(client._host, "http_grant", grant)

    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        calls.append(endpoint)
        return _resp({"data": []})
    monkeypatch.setattr(client._host, "http_request", http)

    data = asyncio.run(client.collect(force=True))
    by_id = {p["id"]: p for p in data["providers"]}
    assert by_id["openai"]["configured"] is False
    assert "openai" not in calls  # pending endpoint is never called


def test_collect_caches(monkeypatch):
    client.invalidate()

    async def grant(endpoint):
        return {"status": "unknown"}
    monkeypatch.setattr(client._host, "http_grant", grant)
    first = asyncio.run(client.collect(force=True))
    second = asyncio.run(client.collect())  # within TTL -> same cached object
    assert first is second
