"""Provider polling for the consolidated TokenPulse view: spend, per-model cost,
quotas, credit balances, and daily trend, over the host http.request bridge.

Each provider poll returns one rich dict so the UI can show a tile and drill into
granular detail (per-model cost, daily trend, quotas) without a second call:

  provider = {id, name, reachable, error, configured, currency,
              spend:{today,week,mtd,"30d"}, credits, quotas:[...],
              models:[per-model rows], trend:[{date,amount}]}

Sources:
  - Anthropic/OpenAI: daily cost buckets (MTD total + today + trend) and usage
    grouped by model (per-model tokens -> derived cost, labelled estimated); plus
    a monthly budget gauge.
  - OpenRouter: /key (spend windows + key limit), /credits (balance), /activity
    (real per-model spend, 30d); plus a monthly budget gauge.

Cost honesty: OpenRouter per-model cost is actual; Anthropic/OpenAI per-model
cost is estimated from tokens x prices.py, with each provider's actual total shown
alongside. Degrade-not-fatal per provider; short-TTL cache. Admin keys stay
host-side; this code names only the operator-consented endpoint and a rel path.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from typing import Any

from . import _host, state
from .prices import estimate_cost

ENDPOINTS = ("anthropic", "openai", "openrouter")
_NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "openrouter": "OpenRouter"}
_ANTHROPIC_VERSION = "2023-06-01"
MAX_PAGES = 12
MAX_ACTIVITY_ROWS = 5000
CACHE_TTL = 300.0

_CACHE: dict[str, Any] = {"at": 0.0, "data": None}


class ProviderError(RuntimeError):
    """A non-2xx or malformed response from a provider API."""


# ── small helpers ──────────────────────────────────────────────────────────────


def _num(v: Any) -> float:
    try:
        if isinstance(v, bool):
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _err_detail(resp: dict) -> str:
    body = resp.get("body") or ""
    try:
        parsed = json.loads(body)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or "")[:200]
        if isinstance(err, str):
            return err[:200]
    except (ValueError, AttributeError):
        pass
    return str(body)[:160]


async def _get_json(endpoint: str, path: str, query: dict | None = None,
                    headers: dict | None = None) -> Any:
    resp = await _host.http_request(endpoint, method="GET", path=path, query=query, headers=headers)
    status = resp.get("status", 0)
    if status == 0:
        raise ProviderError(f"host refused the call: {resp.get('detail') or 'policy'}")
    if status < 200 or status >= 300:
        raise ProviderError(f"HTTP {status}: {_err_detail(resp)}")
    try:
        return json.loads(resp.get("body") or "{}")
    except ValueError as e:
        raise ProviderError(f"bad JSON from {path}: {e}")


async def _paged(endpoint: str, path: str, params: dict, headers: dict | None = None) -> list[dict]:
    buckets: list[dict] = []
    page: str | None = None
    for _ in range(MAX_PAGES):
        query = dict(params)
        if page:
            query["page"] = page
        payload = await _get_json(endpoint, path, query, headers)
        if isinstance(payload, list):
            buckets.extend(b for b in payload if isinstance(b, dict))
            break
        if not isinstance(payload, dict):
            raise ProviderError(f"unexpected response from {path}")
        buckets.extend(b for b in payload.get("data", []) if isinstance(b, dict))
        if not payload.get("has_more"):
            break
        page = payload.get("next_page")
        if not page:
            break
    return buckets


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _month_start_dt() -> dt.datetime:
    return _utc_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _today_start_dt() -> dt.datetime:
    return _utc_now().replace(hour=0, minute=0, second=0, microsecond=0)


def _next_month_epoch() -> int:
    now = _utc_now()
    nxt = (now.replace(day=28) + dt.timedelta(days=7)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(nxt.timestamp())


def _rfc3339(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reset_epoch(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _pct(used: float, limit: float) -> float | None:
    if not limit or limit <= 0:
        return None
    return round(min(100.0, max(0.0, used / limit * 100.0)), 1)


def _quota(*, scope: str, provider: str, label: str, used: float, limit: float,
           unit: str, basis: str, remaining: float | None = None,
           resets_at: int | None = None) -> dict:
    rem = remaining if remaining is not None else max(0.0, limit - used)
    return {
        "scope": scope, "provider": provider, "provider_name": _NAMES.get(provider, provider),
        "label": label, "used": round(used, 4), "limit": round(limit, 4),
        "remaining": round(rem, 4), "pct": _pct(used, limit), "unit": unit,
        "basis": basis, "resets_at": resets_at,
    }


def _zero_tok() -> dict:
    return {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0}


def _model_rows(provider: str, name: str, per_model: dict, basis: str) -> list[dict]:
    rows = []
    for (model, window), tok in per_model.items():
        cache = tok.get("cache_read", 0.0) + tok.get("cache_write_5m", 0.0) + tok.get("cache_write_1h", 0.0)
        total = tok.get("input", 0.0) + tok.get("output", 0.0) + cache
        cost = estimate_cost(model, tok)
        rows.append({
            "source": provider, "source_name": name, "model": model, "window": window,
            "input_tokens": tok.get("input", 0.0), "output_tokens": tok.get("output", 0.0),
            "cache_tokens": cache, "tokens": total,
            "cost": cost if cost is not None else 0.0,
            "cost_basis": basis if cost is not None else "unpriced", "currency": "USD",
        })
    return rows


def _anthropic_tok(result: dict) -> dict:
    creation = result.get("cache_creation") or {}
    return {"input": _num(result.get("uncached_input_tokens")), "output": _num(result.get("output_tokens")),
            "cache_read": _num(result.get("cache_read_input_tokens")),
            "cache_write_5m": _num(creation.get("ephemeral_5m_input_tokens")),
            "cache_write_1h": _num(creation.get("ephemeral_1h_input_tokens"))}


def _openai_tok(result: dict) -> dict:
    inp, cached = _num(result.get("input_tokens")), _num(result.get("input_cached_tokens"))
    return {"input": max(inp - cached, 0.0), "output": _num(result.get("output_tokens")),
            "cache_read": cached, "cache_write_5m": 0.0, "cache_write_1h": 0.0}


def _short_id(gid: Any) -> str:
    s = str(gid or "")
    return s if len(s) <= 14 else s[:6] + "…" + s[-6:]


def _group(dim: str, gid: Any, name: str, spend: dict, cost: float, basis: str, models: list) -> dict:
    return {"dim": dim, "id": str(gid), "name": name, "spend": spend,
            "cost": round(cost, 6), "cost_basis": basis, "models": models}


def _build_groups(dim: str, provider: str, pname: str, names: dict,
                  cost_map: dict, usage_map: dict) -> list[dict]:
    """Merge a group dimension: actual per-group cost (cost_map) where the
    provider reports it, else an estimate from that group's per-model tokens.
    Per-model rows are always estimated (the split is token-derived)."""
    out = []
    for gid in set(cost_map) | set(usage_map):
        per_model = {(m, "mtd"): tok for m, tok in (usage_map.get(gid) or {}).items()}
        models = _model_rows(provider, pname, per_model, "estimated")
        if gid in cost_map:
            cost, basis = round(cost_map[gid]["mtd"], 6), "actual"
            spend = {"mtd": cost, "today": round(cost_map[gid].get("today", 0.0), 6)}
        else:
            cost, basis = round(sum(m["cost"] for m in models), 6), "estimated"
            spend = {"mtd": cost}
        if cost <= 0 and not models:
            continue
        name = names.get(str(gid)) or names.get(gid) or _short_id(gid)
        out.append(_group(dim, gid, name, spend, cost, basis, models))
    out.sort(key=lambda g: g["cost"], reverse=True)
    return out


async def _safe_names(endpoint: str, path: str, headers: dict | None = None) -> dict:
    """Best-effort id -> display name map from a provider's admin list endpoint."""
    try:
        rows = await _paged(endpoint, path, {"limit": 100}, headers)
    except (ProviderError, _host.HostError):
        return {}
    out: dict = {}
    for r in rows:
        rid = r.get("id")
        if rid:
            out[str(rid)] = str(r.get("name") or r.get("display_name") or rid)[:60]
    return out


async def _groups_safe(coro) -> tuple[list, dict]:
    """A group dimension is a bonus, never fatal: any failure yields no dims."""
    try:
        return await coro
    except Exception:  # noqa: BLE001
        return [], {}


def _provider(provider: str, *, reachable: bool, error: str | None = None, configured: bool = True,
              spend: dict | None = None, credits: dict | None = None,
              quotas: list | None = None, models: list | None = None,
              trend: list | None = None, group_dims: list | None = None,
              groups: dict | None = None, **extra) -> dict:
    out = {
        "id": provider, "name": _NAMES[provider], "reachable": reachable, "error": error,
        "configured": configured, "currency": "USD", "spend": spend or {},
        "credits": credits, "quotas": quotas or [], "models": models or [],
        "trend": trend or [], "budget": 0.0,
        "group_dims": group_dims or [], "groups": groups or {},
    }
    out.update(extra)
    return out


def _unconfigured(provider: str) -> dict:
    return _provider(provider, reachable=False, error="not configured", configured=False)


def _degraded(provider: str, error: str) -> dict:
    return _provider(provider, reachable=False, error=error[:200], configured=True)


# ── providers ──────────────────────────────────────────────────────────────────


async def _poll_anthropic(budget: float) -> dict:
    headers = {"anthropic-version": _ANTHROPIC_VERSION}
    month_start = _month_start_dt()
    today_key = _today_start_dt().strftime("%Y-%m-%d")

    cost_buckets = await _paged("anthropic", "/v1/organizations/cost_report", {
        "starting_at": _rfc3339(month_start), "bucket_width": "1d", "limit": 31}, headers)
    mtd = today = 0.0
    trend: dict[str, float] = {}
    for bucket in cost_buckets:
        day = str(bucket.get("starting_at", ""))[:10]
        for result in bucket.get("results", []):
            if not isinstance(result, dict):
                continue
            amt = _num(result.get("amount")) / 100.0  # cents string
            mtd += amt
            trend[day] = trend.get(day, 0.0) + amt
            if day == today_key:
                today += amt

    per_model: dict = {}
    usage_buckets = await _paged("anthropic", "/v1/organizations/usage_report/messages", {
        "starting_at": _rfc3339(month_start), "bucket_width": "1d", "limit": 31,
        "group_by[]": ["model"]}, headers)
    for bucket in usage_buckets:
        day = str(bucket.get("starting_at", ""))[:10]
        windows = ["mtd"] + (["today"] if day == today_key else [])
        for result in bucket.get("results", []):
            if not isinstance(result, dict):
                continue
            model = result.get("model")
            if not model:
                continue
            t = _anthropic_tok(result)
            for w in windows:
                d = per_model.setdefault((model, w), _zero_tok())
                for k in t:
                    d[k] += t[k]

    quotas = []
    if budget > 0:
        quotas.append(_quota(scope="monthly", provider="anthropic", label="Anthropic budget",
                             used=mtd, limit=budget, unit="usd", basis="budget", resets_at=_next_month_epoch()))
    group_dims, groups = await _groups_safe(_anthropic_groups(headers, month_start, today_key))
    return _provider("anthropic", reachable=True, spend={"today": round(today, 6), "mtd": round(mtd, 6)},
                     quotas=quotas, models=_model_rows("anthropic", "Anthropic", per_model, "estimated"),
                     trend=[{"date": d, "amount": round(v, 6)} for d, v in sorted(trend.items())],
                     group_dims=group_dims, groups=groups)


async def _anthropic_grouped_cost(headers: dict, month_start: dt.datetime, today_key: str, field: str) -> dict:
    buckets = await _paged("anthropic", "/v1/organizations/cost_report", {
        "starting_at": _rfc3339(month_start), "bucket_width": "1d", "limit": 31, "group_by[]": [field]}, headers)
    out: dict = {}
    for bucket in buckets:
        day = str(bucket.get("starting_at", ""))[:10]
        for r in bucket.get("results", []):
            if not isinstance(r, dict):
                continue
            gid = r.get(field) or "untagged"
            amt = _num(r.get("amount")) / 100.0
            g = out.setdefault(gid, {"mtd": 0.0, "today": 0.0})
            g["mtd"] += amt
            if day == today_key:
                g["today"] += amt
    return out


async def _anthropic_grouped_usage(headers: dict, month_start: dt.datetime, field: str) -> dict:
    buckets = await _paged("anthropic", "/v1/organizations/usage_report/messages", {
        "starting_at": _rfc3339(month_start), "bucket_width": "1d", "limit": 31,
        "group_by[]": [field, "model"]}, headers)
    per: dict = {}
    for bucket in buckets:
        for r in bucket.get("results", []):
            if not isinstance(r, dict):
                continue
            model, gid = r.get("model"), (r.get(field) or "untagged")
            if not model:
                continue
            t = _anthropic_tok(r)
            d = per.setdefault(gid, {}).setdefault(model, _zero_tok())
            for k in t:
                d[k] += t[k]
    return per


async def _anthropic_groups(headers: dict, month_start: dt.datetime, today_key: str) -> tuple[list, dict]:
    dims: list = []
    groups: dict = {}
    ws_names = await _safe_names("anthropic", "/v1/organizations/workspaces", headers)
    ws_cost = await _anthropic_grouped_cost(headers, month_start, today_key, "workspace_id")
    ws_usage = await _anthropic_grouped_usage(headers, month_start, "workspace_id")
    ws = _build_groups("workspace", "anthropic", "Anthropic", ws_names, ws_cost, ws_usage)
    if ws:
        dims.append({"key": "workspace", "label": "Workspaces", "basis": "actual"})
        groups["workspace"] = ws
    key_names = await _safe_names("anthropic", "/v1/organizations/api_keys", headers)
    key_usage = await _anthropic_grouped_usage(headers, month_start, "api_key_id")
    keys = _build_groups("api_key", "anthropic", "Anthropic", key_names, {}, key_usage)
    if keys:
        dims.append({"key": "api_key", "label": "API keys", "basis": "estimated"})
        groups["api_key"] = keys
    return dims, groups


async def _poll_openai(budget: float) -> dict:
    month_start = int(_month_start_dt().timestamp())
    today_start = int(_today_start_dt().timestamp())

    cost_buckets = await _paged("openai", "/v1/organization/costs", {
        "start_time": month_start, "bucket_width": "1d", "limit": 31})
    mtd = today = 0.0
    trend: dict[str, float] = {}
    for bucket in cost_buckets:
        bs = int(_num(bucket.get("start_time")))
        day = dt.datetime.fromtimestamp(bs, dt.timezone.utc).strftime("%Y-%m-%d") if bs else ""
        for result in bucket.get("results", []):
            if not isinstance(result, dict):
                continue
            val = _num((result.get("amount") or {}).get("value"))
            mtd += val
            trend[day] = trend.get(day, 0.0) + val
            if bs >= today_start:
                today += val

    per_model: dict = {}
    usage_buckets = await _paged("openai", "/v1/organization/usage/completions", {
        "start_time": month_start, "bucket_width": "1d", "limit": 31, "group_by[]": ["model"]})
    for bucket in usage_buckets:
        bs = int(_num(bucket.get("start_time")))
        windows = ["mtd"] + (["today"] if bs >= today_start else [])
        for result in bucket.get("results", []):
            if not isinstance(result, dict):
                continue
            model = result.get("model")
            if not model:
                continue
            t = _openai_tok(result)  # input_tokens includes cached; _openai_tok nets it out
            for w in windows:
                d = per_model.setdefault((model, w), _zero_tok())
                for k in t:
                    d[k] += t[k]

    quotas = []
    if budget > 0:
        quotas.append(_quota(scope="monthly", provider="openai", label="OpenAI budget",
                             used=mtd, limit=budget, unit="usd", basis="budget", resets_at=_next_month_epoch()))
    group_dims, groups = await _groups_safe(_openai_groups(month_start, today_start))
    return _provider("openai", reachable=True, spend={"today": round(today, 6), "mtd": round(mtd, 6)},
                     quotas=quotas, models=_model_rows("openai", "OpenAI", per_model, "estimated"),
                     trend=[{"date": d, "amount": round(v, 6)} for d, v in sorted(trend.items())],
                     group_dims=group_dims, groups=groups)


async def _openai_grouped_cost(month_start: int, today_start: int, field: str) -> dict:
    buckets = await _paged("openai", "/v1/organization/costs", {
        "start_time": month_start, "bucket_width": "1d", "limit": 31, "group_by[]": [field]})
    out: dict = {}
    for bucket in buckets:
        bs = int(_num(bucket.get("start_time")))
        for r in bucket.get("results", []):
            if not isinstance(r, dict):
                continue
            gid = r.get(field) or "untagged"
            val = _num((r.get("amount") or {}).get("value"))
            g = out.setdefault(gid, {"mtd": 0.0, "today": 0.0})
            g["mtd"] += val
            if bs >= today_start:
                g["today"] += val
    return out


async def _openai_grouped_usage(month_start: int, field: str) -> dict:
    buckets = await _paged("openai", "/v1/organization/usage/completions", {
        "start_time": month_start, "bucket_width": "1d", "limit": 31, "group_by[]": [field, "model"]})
    per: dict = {}
    for bucket in buckets:
        for r in bucket.get("results", []):
            if not isinstance(r, dict):
                continue
            model, gid = r.get("model"), (r.get(field) or "untagged")
            if not model:
                continue
            t = _openai_tok(r)
            d = per.setdefault(gid, {}).setdefault(model, _zero_tok())
            for k in t:
                d[k] += t[k]
    return per


async def _openai_groups(month_start: int, today_start: int) -> tuple[list, dict]:
    dims: list = []
    groups: dict = {}
    proj_names = await _safe_names("openai", "/v1/organization/projects")
    proj_cost = await _openai_grouped_cost(month_start, today_start, "project_id")
    proj_usage = await _openai_grouped_usage(month_start, "project_id")
    proj = _build_groups("project", "openai", "OpenAI", proj_names, proj_cost, proj_usage)
    if proj:
        dims.append({"key": "project", "label": "Projects", "basis": "actual"})
        groups["project"] = proj
    key_usage = await _openai_grouped_usage(month_start, "api_key_id")
    keys = _build_groups("api_key", "openai", "OpenAI", {}, {}, key_usage)
    if keys:
        dims.append({"key": "api_key", "label": "API keys", "basis": "estimated"})
        groups["api_key"] = keys
    return dims, groups


async def _poll_openrouter(budget: float) -> dict:
    key = await _get_json("openrouter", "/key")
    data = key.get("data") if isinstance(key, dict) else None
    if not isinstance(data, dict):
        raise ProviderError("unexpected /key response")

    usage_month = _num(data.get("usage_monthly"))
    spend = {"today": _num(data.get("usage_daily")), "week": _num(data.get("usage_weekly")),
             "mtd": usage_month, "total": _num(data.get("usage"))}
    quotas: list[dict] = []
    credits = None

    limit = _num(data.get("limit"))
    remaining = data.get("limit_remaining")
    if limit and limit > 0:
        used = (limit - _num(remaining)) if remaining is not None else _num(data.get("usage"))
        quotas.append(_quota(scope="limit", provider="openrouter", label="OpenRouter key limit",
                             used=max(0.0, used), limit=limit, unit="usd", basis="limit",
                             remaining=_num(remaining) if remaining is not None else None,
                             resets_at=_reset_epoch(data.get("limit_reset"))))

    try:
        cred = await _get_json("openrouter", "/credits")
        cdata = cred.get("data") if isinstance(cred, dict) else None
        if isinstance(cdata, dict):
            purchased, used = _num(cdata.get("total_credits")), _num(cdata.get("total_usage"))
            if purchased > 0:
                credits = {"limit": round(purchased, 4), "used": round(used, 4), "remaining": round(purchased - used, 4)}
                quotas.append(_quota(scope="credit", provider="openrouter", label="OpenRouter credits",
                                     used=used, limit=purchased, unit="usd", basis="balance"))
    except ProviderError:
        pass

    # Per-model actual spend, last 30 completed UTC days (needs a management key).
    models: list[dict] = []
    spend_30d = 0.0
    try:
        activity = await _get_json("openrouter", "/activity")
        rows = activity.get("data") if isinstance(activity, dict) else activity
        if isinstance(rows, list):
            per_model: dict = {}
            for row in rows[:MAX_ACTIVITY_ROWS]:
                if not isinstance(row, dict):
                    continue
                used = _num(row.get("usage"))
                spend_30d += used
                model = row.get("model") or row.get("model_permaslug")
                if isinstance(model, str) and model:
                    d = per_model.setdefault(model, {"cost": 0.0, "input": 0.0, "output": 0.0})
                    d["cost"] += used
                    d["input"] += _num(row.get("prompt_tokens"))
                    d["output"] += _num(row.get("completion_tokens"))
            for model, d in per_model.items():
                models.append({
                    "source": "openrouter", "source_name": "OpenRouter", "model": model, "window": "30d",
                    "input_tokens": d["input"], "output_tokens": d["output"], "cache_tokens": 0.0,
                    "tokens": d["input"] + d["output"], "cost": round(d["cost"], 6),
                    "cost_basis": "actual", "currency": "USD",
                })
            spend["30d"] = round(spend_30d, 6)
    except ProviderError:
        pass

    if budget > 0:
        quotas.append(_quota(scope="monthly", provider="openrouter", label="OpenRouter budget",
                             used=usage_month, limit=budget, unit="usd", basis="budget", resets_at=_next_month_epoch()))
    group_dims, groups = await _groups_safe(_openrouter_groups())
    return _provider("openrouter", reachable=True, spend=spend, credits=credits, quotas=quotas,
                     models=models, trend=[], label=str(data.get("label") or "")[:40],
                     group_dims=group_dims, groups=groups)


async def _openrouter_groups() -> tuple[list, dict]:
    """Per-key actual spend, via a provisioning/management key's /keys listing.
    Lifetime usage vs the key's limit (not monthly); no per-model split here."""
    payload = await _get_json("openrouter", "/keys", {"limit": 100})
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        return [], {}
    out: list = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or r.get("label") or _short_id(r.get("hash")) or "key")[:60]
        used, limit = _num(r.get("usage")), _num(r.get("limit"))
        g = _group("key", r.get("hash") or name, name, {"usage": round(used, 6)}, used, "actual", [])
        g["limit"] = round(limit, 6) if limit > 0 else None
        g["disabled"] = bool(r.get("disabled"))
        out.append(g)
    out.sort(key=lambda x: x["cost"], reverse=True)
    return [{"key": "key", "label": "API keys", "basis": "actual"}], {"key": out}


_POLLERS = {"anthropic": _poll_anthropic, "openai": _poll_openai, "openrouter": _poll_openrouter}


# ── aggregation + cache ─────────────────────────────────────────────────────────


async def _grants() -> dict[str, str]:
    out = {}
    for eid in ENDPOINTS:
        try:
            g = await _host.http_grant(eid)
            out[eid] = g.get("status", "unknown")
        except Exception:
            out[eid] = "unknown"
    return out


async def _safe_poll(eid: str, status: str, budget: float) -> dict:
    if status != "active":
        return _unconfigured(eid)
    try:
        p = await _POLLERS[eid](budget)
        p["budget"] = budget
        return p
    except (_host.HostError, ProviderError) as e:
        return _degraded(eid, str(e))
    except Exception as e:  # noqa: BLE001 - one bad provider must not sink the view
        return _degraded(eid, f"unexpected: {e}")


async def collect(force: bool = False) -> dict:
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["at"]) < CACHE_TTL:
        return _CACHE["data"]
    grants = await _grants()
    budgets = state.get_budgets()
    providers = await asyncio.gather(
        *[_safe_poll(eid, grants.get(eid, "unknown"), budgets.get(eid, 0.0)) for eid in ENDPOINTS])

    quotas: list[dict] = []
    models: list[dict] = []
    trend_by_day: dict[str, float] = {}
    for p in providers:
        quotas.extend(p["quotas"])
        models.extend(p["models"])
        for row in p["trend"]:
            trend_by_day[row["date"]] = trend_by_day.get(row["date"], 0.0) + row["amount"]
    quotas.sort(key=lambda q: (q["pct"] is None, -(q["pct"] or 0.0)))
    trend = [{"date": d, "amount": round(v, 6)} for d, v in sorted(trend_by_day.items())]
    data = {"providers": list(providers), "quotas": quotas, "models": models, "trend": trend,
            "budgets": budgets, "generated_at": int(now)}
    _CACHE["data"] = data
    _CACHE["at"] = now
    return data


def invalidate() -> None:
    _CACHE["data"] = None
    _CACHE["at"] = 0.0
