"""Per-project / per-key breakdown: dims, actual vs estimated, degrade-safe.

The fake dispatchers branch on both `path` and the `group_by[]` query so plain
and grouped calls to the same endpoint stay distinct, mirroring the real APIs.
"""

import asyncio
import datetime as dt
import json

import tokenpulse.client as client


def _resp(payload, status=200):
    return {"status": status, "headers": {}, "body": json.dumps(payload), "truncated": False}


def _today():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _gb(query):
    return list((query or {}).get("group_by[]") or [])


def test_anthropic_workspace_actual_and_key_estimated(monkeypatch):
    day = _today()

    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        gb = _gb(query)
        if path == "/v1/organizations/cost_report":
            if gb == ["workspace_id"]:
                return _resp({"data": [{"starting_at": f"{day}T00:00:00Z", "results": [
                    {"workspace_id": "wrk_acme", "amount": "3000"},
                    {"workspace_id": "wrk_globex", "amount": "1000"}]}], "has_more": False})
            return _resp({"data": [{"starting_at": f"{day}T00:00:00Z", "results": [{"amount": "4000"}]}], "has_more": False})
        if path == "/v1/organizations/usage_report/messages":
            if gb == ["workspace_id", "model"]:
                return _resp({"data": [{"starting_at": f"{day}T00:00:00Z", "results": [
                    {"workspace_id": "wrk_acme", "model": "claude-opus-4", "uncached_input_tokens": 1_000_000, "output_tokens": 500_000}]}], "has_more": False})
            if gb == ["api_key_id", "model"]:
                return _resp({"data": [{"starting_at": f"{day}T00:00:00Z", "results": [
                    {"api_key_id": "key_ci", "model": "claude-sonnet-4-5", "uncached_input_tokens": 1_000_000, "output_tokens": 1_000_000}]}], "has_more": False})
            return _resp({"data": [], "has_more": False})
        if path == "/v1/organizations/workspaces":
            return _resp({"data": [{"id": "wrk_acme", "name": "Acme"}, {"id": "wrk_globex", "name": "Globex"}], "has_more": False})
        if path == "/v1/organizations/api_keys":
            return _resp({"data": [{"id": "key_ci", "name": "ci-runner"}], "has_more": False})
        return _resp({"data": [], "has_more": False})

    monkeypatch.setattr(client._host, "http_request", http)
    p = asyncio.run(client._poll_anthropic(budget=0.0))
    dims = {d["key"]: d for d in p["group_dims"]}
    assert dims["workspace"]["basis"] == "actual" and dims["api_key"]["basis"] == "estimated"

    ws = {g["id"]: g for g in p["groups"]["workspace"]}
    assert ws["wrk_acme"]["name"] == "Acme" and ws["wrk_acme"]["cost"] == 30.0  # actual, cents
    assert ws["wrk_acme"]["cost_basis"] == "actual"
    assert ws["wrk_acme"]["models"][0]["cost_basis"] == "estimated"  # split is token-derived

    keys = p["groups"]["api_key"]
    assert keys[0]["name"] == "ci-runner" and keys[0]["cost_basis"] == "estimated"
    assert keys[0]["cost"] == 18.0  # sonnet 1M in + 1M out = $3 + $15


def test_openai_project_and_key(monkeypatch):
    te = int(dt.datetime.now(dt.timezone.utc).replace(hour=12).timestamp())

    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        gb = _gb(query)
        if path == "/v1/organization/costs":
            if gb == ["project_id"]:
                return _resp({"data": [{"start_time": te, "results": [
                    {"project_id": "proj_web", "amount": {"value": 9.0}}]}], "has_more": False})
            return _resp({"data": [{"start_time": te, "results": [{"amount": {"value": 9.0}}]}], "has_more": False})
        if path == "/v1/organization/usage/completions":
            if gb == ["project_id", "model"]:
                return _resp({"data": [{"start_time": te, "results": [
                    {"project_id": "proj_web", "model": "gpt-4o", "input_tokens": 1_000_000, "output_tokens": 500_000}]}], "has_more": False})
            if gb == ["api_key_id", "model"]:
                return _resp({"data": [{"start_time": te, "results": [
                    {"api_key_id": "key_abc123def456ghi789", "model": "gpt-4o-mini", "input_tokens": 2_000_000, "output_tokens": 1_000_000}]}], "has_more": False})
            return _resp({"data": [], "has_more": False})
        if path == "/v1/organization/projects":
            return _resp({"data": [{"id": "proj_web", "name": "Website"}], "has_more": False})
        return _resp({"data": [], "has_more": False})

    monkeypatch.setattr(client._host, "http_request", http)
    p = asyncio.run(client._poll_openai(budget=0.0))
    assert [d["key"] for d in p["group_dims"]] == ["project", "api_key"]
    proj = p["groups"]["project"][0]
    assert proj["name"] == "Website" and proj["cost"] == 9.0 and proj["cost_basis"] == "actual"
    key = p["groups"]["api_key"][0]
    assert key["cost_basis"] == "estimated" and "…" in key["name"]  # no name endpoint -> shortened id


def test_openrouter_keys_are_actual(monkeypatch):
    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        if path == "/key":
            return _resp({"data": {"usage": 5.0, "usage_monthly": 5.0, "limit": 50.0, "limit_remaining": 45.0}})
        if path == "/keys":
            return _resp({"data": [
                {"hash": "h1", "name": "prod-acme", "usage": 40.0, "limit": 100.0},
                {"hash": "h2", "label": "ci", "usage": 8.0, "limit": None, "disabled": True}]})
        return _resp({"error": {"message": "no"}}, status=403)  # /credits, /activity

    monkeypatch.setattr(client._host, "http_request", http)
    p = asyncio.run(client._poll_openrouter(budget=0.0))
    assert [d["key"] for d in p["group_dims"]] == ["key"]
    g = p["groups"]["key"]
    assert g[0]["name"] == "prod-acme" and g[0]["cost"] == 40.0 and g[0]["limit"] == 100.0
    assert g[0]["cost_basis"] == "actual" and g[1]["disabled"] is True


def test_groups_degrade_when_grouped_calls_fail(monkeypatch):
    day = _today()

    async def http(endpoint, method="GET", path="", query=None, headers=None, body=None):
        gb = _gb(query)
        if path == "/v1/organizations/cost_report" and not gb:
            return _resp({"data": [{"starting_at": f"{day}T00:00:00Z", "results": [{"amount": "4000"}]}], "has_more": False})
        if path == "/v1/organizations/usage_report/messages" and gb == ["model"]:
            return _resp({"data": [], "has_more": False})
        return _resp({"error": {"message": "forbidden"}}, status=403)  # every grouped/name call

    monkeypatch.setattr(client._host, "http_request", http)
    p = asyncio.run(client._poll_anthropic(budget=0.0))
    assert p["reachable"] is True and p["spend"]["mtd"] == 40.0  # base poll unaffected
    assert p["group_dims"] == [] and p["groups"] == {}
