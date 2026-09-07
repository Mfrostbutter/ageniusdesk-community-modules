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


def _model_rows(provider: str, name: str, per_model: dict, basis: str) -> list[dict]:
    rows = []
    for (model, window), tok in per_model.items():
        total = tok["input"] + tok["output"] + tok["cache_read"] + tok["cache_write"]
        cost = estimate_cost(model, tok)
        rows.append({
            "source": provider, "source_name": name, "model": model, "window": window,
            "input_tokens": tok["input"], "output_tokens": tok["output"],
            "cache_tokens": tok["cache_read"] + tok["cache_write"], "tokens": total,
            "cost": cost if cost is not None else 0.0,
            "cost_basis": basis if cost is not None else "unpriced", "currency": "USD",
        })
    return rows


def _provider(provider: str, *, reachable: bool, error: str | None = None, configured: bool = True,
              spend: dict | None = None, credits: dict | None = None,
              quotas: list | None = None, models: list | None = None,
              trend: list | None = None, **extra) -> dict:
    out = {
        "id": provider, "name": _NAMES[provider], "reachable": reachable, "error": error,
        "configured": configured, "currency": "USD", "spend": spend or {},
        "credits": credits, "quotas": quotas or [], "models": models or [],
        "trend": trend or [], "budget": 0.0,
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
            creation = result.get("cache_creation") or {}
            cw = _num(creation.get("ephemeral_1h_input_tokens")) + _num(creation.get("ephemeral_5m_input_tokens"))
            inp, out = _num(result.get("uncached_input_tokens")), _num(result.get("output_tokens"))
            cr = _num(result.get("cache_read_input_tokens"))
            for w in windows:
                d = per_model.setdefault((model, w), {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0})
                d["input"] += inp
                d["output"] += out
                d["cache_read"] += cr
                d["cache_write"] += cw

    quotas = []
    if budget > 0:
        quotas.append(_quota(scope="monthly", provider="anthropic", label="Anthropic budget",
                             used=mtd, limit=budget, unit="usd", basis="budget", resets_at=_next_month_epoch()))
    return _provider("anthropic", reachable=True, spend={"today": round(today, 6), "mtd": round(mtd, 6)},
                     quotas=quotas, models=_model_rows("anthropic", "Anthropic", per_model, "estimated"),
                     trend=[{"date": d, "amount": round(v, 6)} for d, v in sorted(trend.items())])


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
            inp, out = _num(result.get("input_tokens")), _num(result.get("output_tokens"))
            cached = _num(result.get("input_cached_tokens"))
            for w in windows:
                d = per_model.setdefault((model, w), {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0})
                d["input"] += max(inp - cached, 0.0)  # input_tokens includes cached
                d["output"] += out
                d["cache_read"] += cached

    quotas = []
    if budget > 0:
        quotas.append(_quota(scope="monthly", provider="openai", label="OpenAI budget",
                             used=mtd, limit=budget, unit="usd", basis="budget", resets_at=_next_month_epoch()))
    return _provider("openai", reachable=True, spend={"today": round(today, 6), "mtd": round(mtd, 6)},
                     quotas=quotas, models=_model_rows("openai", "OpenAI", per_model, "estimated"),
                     trend=[{"date": d, "amount": round(v, 6)} for d, v in sorted(trend.items())])


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
    return _provider("openrouter", reachable=True, spend=spend, credits=credits, quotas=quotas,
                     models=models, trend=[], label=str(data.get("label") or "")[:40])


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
