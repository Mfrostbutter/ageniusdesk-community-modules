"""Provider polling for quotas, credit balances, and budget gauges, over the
host http.request bridge.

Companion to the model-cost module: where that breaks spend down per model, this
answers "how close am I to a limit". Sources:

  - Anthropic / OpenAI: daily cost buckets (month to date) -> a monthly budget
    gauge (actual MTD vs the operator's budget) and a per-day spend trend.
  - OpenRouter: /key (spend limit + remaining, daily/weekly/monthly usage) and,
    with a management key, /credits (account balance).

Degrade-not-fatal per provider; short-TTL cache. Admin keys stay host-side; this
code only names the operator-consented endpoint and a relative path.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from typing import Any

from . import _host, state

ENDPOINTS = ("anthropic", "openai", "openrouter")
_NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "openrouter": "OpenRouter"}
_ANTHROPIC_VERSION = "2023-06-01"
MAX_PAGES = 12
CACHE_TTL = 300.0

_CACHE: dict[str, Any] = {"at": 0.0, "data": None}


class ProviderError(RuntimeError):
    """A non-2xx or malformed response from a provider API."""


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
    """Best-effort epoch for a provider reset field (epoch number or ISO string)."""
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


# ── providers ──────────────────────────────────────────────────────────────────


async def _cost_daily(endpoint: str, headers: dict | None, path: str,
                      amount_of, day_of) -> tuple[float, float, list[dict]]:
    """(mtd_total, today_total, per-day trend) from a daily-bucketed cost report."""
    month_start = _month_start_dt()
    today_key = _today_start_dt().strftime("%Y-%m-%d")
    if endpoint == "openai":
        params = {"start_time": int(month_start.timestamp()), "bucket_width": "1d", "limit": 31}
    else:
        params = {"starting_at": _rfc3339(month_start), "bucket_width": "1d", "limit": 31}
    buckets = await _paged(endpoint, path, params, headers)
    mtd = today = 0.0
    trend: dict[str, float] = {}
    for bucket in buckets:
        day = day_of(bucket)
        for result in bucket.get("results", []):
            if not isinstance(result, dict):
                continue
            amt = amount_of(result)
            mtd += amt
            trend[day] = trend.get(day, 0.0) + amt
            if day == today_key:
                today += amt
    trend_rows = [{"date": d, "amount": round(v, 6)} for d, v in sorted(trend.items())]
    return mtd, today, trend_rows


async def _poll_anthropic(budget: float) -> tuple[dict, list[dict], list[dict]]:
    headers = {"anthropic-version": _ANTHROPIC_VERSION}
    mtd, today, trend = await _cost_daily(
        "anthropic", headers, "/v1/organizations/cost_report",
        amount_of=lambda r: _num(r.get("amount")) / 100.0,  # cents string
        day_of=lambda b: str(b.get("starting_at", ""))[:10],
    )
    summary = _summary("anthropic", reachable=True, spend={"today": today, "mtd": mtd})
    quotas = []
    if budget > 0:
        quotas.append(_quota(scope="monthly", provider="anthropic", label="Anthropic budget",
                             used=mtd, limit=budget, unit="usd", basis="budget",
                             resets_at=_next_month_epoch()))
    return summary, quotas, trend


async def _poll_openai(budget: float) -> tuple[dict, list[dict], list[dict]]:
    today_start = int(_today_start_dt().timestamp())
    mtd, today, trend = await _cost_daily(
        "openai", None, "/v1/organization/costs",
        amount_of=lambda r: _num((r.get("amount") or {}).get("value")),
        day_of=lambda b: dt.datetime.fromtimestamp(int(_num(b.get("start_time"))), dt.timezone.utc).strftime("%Y-%m-%d")
        if b.get("start_time") else "",
    )
    summary = _summary("openai", reachable=True, spend={"today": today, "mtd": mtd})
    quotas = []
    if budget > 0:
        quotas.append(_quota(scope="monthly", provider="openai", label="OpenAI budget",
                             used=mtd, limit=budget, unit="usd", basis="budget",
                             resets_at=_next_month_epoch()))
    return summary, quotas, trend


async def _poll_openrouter(budget: float) -> tuple[dict, list[dict], list[dict]]:
    key = await _get_json("openrouter", "/key")
    data = key.get("data") if isinstance(key, dict) else None
    if not isinstance(data, dict):
        raise ProviderError("unexpected /key response")

    usage_month = _num(data.get("usage_monthly"))
    spend = {
        "today": _num(data.get("usage_daily")),
        "week": _num(data.get("usage_weekly")),
        "mtd": usage_month,
        "total": _num(data.get("usage")),
    }
    summary = _summary("openrouter", reachable=True, spend=spend,
                       label=str(data.get("label") or "")[:40], free_tier=bool(data.get("is_free_tier")))
    quotas: list[dict] = []

    limit = _num(data.get("limit"))
    remaining = data.get("limit_remaining")
    if limit and limit > 0:
        used = (limit - _num(remaining)) if remaining is not None else _num(data.get("usage"))
        quotas.append(_quota(scope="limit", provider="openrouter", label="OpenRouter key limit",
                             used=max(0.0, used), limit=limit, unit="usd", basis="limit",
                             remaining=_num(remaining) if remaining is not None else None,
                             resets_at=_reset_epoch(data.get("limit_reset"))))

    # Account credit balance needs a management key; silent when unavailable.
    try:
        credits = await _get_json("openrouter", "/credits")
        cdata = credits.get("data") if isinstance(credits, dict) else None
        if isinstance(cdata, dict):
            purchased = _num(cdata.get("total_credits"))
            used = _num(cdata.get("total_usage"))
            if purchased > 0:
                summary["credits"] = {"limit": round(purchased, 4), "used": round(used, 4),
                                      "remaining": round(purchased - used, 4)}
                quotas.append(_quota(scope="credit", provider="openrouter", label="OpenRouter credits",
                                     used=used, limit=purchased, unit="usd", basis="balance"))
    except ProviderError:
        pass

    if budget > 0:
        quotas.append(_quota(scope="monthly", provider="openrouter", label="OpenRouter budget",
                             used=usage_month, limit=budget, unit="usd", basis="budget",
                             resets_at=_next_month_epoch()))
    return summary, quotas, []


_POLLERS = {"anthropic": _poll_anthropic, "openai": _poll_openai, "openrouter": _poll_openrouter}


def _summary(provider: str, *, reachable: bool, error: str | None = None, configured: bool = True,
             spend: dict | None = None, **extra) -> dict:
    out = {
        "id": provider, "name": _NAMES[provider], "reachable": reachable, "error": error,
        "configured": configured, "currency": "USD", "spend": spend or {},
        "credits": None, "budget": 0.0,
    }
    out.update(extra)
    return out


def _unconfigured(provider: str) -> dict:
    return _summary(provider, reachable=False, error="not configured", configured=False)


def _degraded(provider: str, error: str) -> dict:
    return _summary(provider, reachable=False, error=error[:200], configured=True)


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


async def _safe_poll(eid: str, status: str, budget: float) -> tuple[dict, list[dict], list[dict]]:
    if status != "active":
        return _unconfigured(eid), [], []
    try:
        summary, quotas, trend = await _POLLERS[eid](budget)
        summary["budget"] = budget
        return summary, quotas, trend
    except (_host.HostError, ProviderError) as e:
        return _degraded(eid, str(e)), [], []
    except Exception as e:  # noqa: BLE001 - one bad provider must not sink the view
        return _degraded(eid, f"unexpected: {e}"), [], []


async def collect(force: bool = False) -> dict:
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["at"]) < CACHE_TTL:
        return _CACHE["data"]
    grants = await _grants()
    budgets = state.get_budgets()
    results = await asyncio.gather(
        *[_safe_poll(eid, grants.get(eid, "unknown"), budgets.get(eid, 0.0)) for eid in ENDPOINTS]
    )
    providers = [r[0] for r in results]
    quotas: list[dict] = []
    trend_by_day: dict[str, float] = {}
    for _, qs, tr in results:
        quotas.extend(qs)
        for row in tr:
            trend_by_day[row["date"]] = trend_by_day.get(row["date"], 0.0) + row["amount"]
    # Loudest first: highest utilization at the top (unknown pct sinks to the end).
    quotas.sort(key=lambda q: (q["pct"] is None, -(q["pct"] or 0.0)))
    trend = [{"date": d, "amount": round(v, 6)} for d, v in sorted(trend_by_day.items())]
    data = {"providers": providers, "quotas": quotas, "trend": trend,
            "budgets": budgets, "generated_at": int(now)}
    _CACHE["data"] = data
    _CACHE["at"] = now
    return data


def invalidate() -> None:
    _CACHE["data"] = None
    _CACHE["at"] = 0.0
