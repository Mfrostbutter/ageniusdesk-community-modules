"""Provider polling + per-model normalization, over the host http.request bridge.

Every provider call goes through `_host.http_request` to an operator-consented
endpoint (base URL + admin key live host-side). Nothing here raises on a degraded
provider: a provider that is unconfigured, unreachable, or rejecting comes back as
a summary with `reachable=False` and an error string, mirroring the host's own
degrade-not-fatal contract. Results are cached for a short TTL so the own-view
poll and the dashboard card poll do not each hammer the provider APIs.

Cost honesty: provider COST endpoints report a total, not a per-model breakdown;
their USAGE endpoints break down by model (tokens only). So Anthropic and OpenAI
per-model cost is DERIVED (tokens x prices.py) and labelled `estimated`, while the
provider's actual total is carried alongside. OpenRouter's /activity reports real
per-model spend, labelled `actual`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from typing import Any

from . import _host
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
    """Follow the {data, has_more, next_page} paging every admin usage/cost API uses."""
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


def _rfc3339(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows_from_tokens(eid: str, name: str, per_model: dict, basis: str) -> list[dict]:
    """Build normalized model rows from accumulated per-(model, window) tokens,
    deriving cost from the price table (basis 'estimated'). An unpriced model
    still reports its tokens with cost 0 and basis 'unpriced', never a guess."""
    rows = []
    for (model, window), tok in per_model.items():
        total = tok["input"] + tok["output"] + tok["cache_read"] + tok["cache_write"]
        cost = estimate_cost(model, tok)
        rows.append({
            "source": eid, "source_name": name, "model": model, "window": window,
            "input_tokens": tok["input"], "output_tokens": tok["output"],
            "cache_tokens": tok["cache_read"] + tok["cache_write"], "tokens": total,
            "cost": cost if cost is not None else 0.0,
            "cost_basis": basis if cost is not None else "unpriced",
            "currency": "USD",
        })
    return rows


def _summary(eid: str, name: str, *, reachable: bool, error: str | None,
             configured: bool, cost: dict, tokens: dict, model_count: int) -> dict:
    return {
        "id": eid, "name": name, "reachable": reachable, "error": error,
        "configured": configured, "currency": "USD",
        "cost": cost, "tokens": tokens, "model_count": model_count,
    }


def _unconfigured(eid: str, name: str) -> dict:
    return _summary(eid, name, reachable=False, error="not configured", configured=False,
                    cost={}, tokens={}, model_count=0)


def _degraded(eid: str, name: str, error: str, configured: bool) -> dict:
    return _summary(eid, name, reachable=False, error=error[:200], configured=configured,
                    cost={}, tokens={}, model_count=0)


# ── providers ──────────────────────────────────────────────────────────────────


async def _poll_anthropic() -> tuple[dict, list[dict]]:
    eid, name = "anthropic", "Anthropic"
    headers = {"anthropic-version": _ANTHROPIC_VERSION}
    month_start = _month_start_dt()
    today_key = _today_start_dt().strftime("%Y-%m-%d")

    cost_buckets = await _paged(eid, "/v1/organizations/cost_report", {
        "starting_at": _rfc3339(month_start), "bucket_width": "1d", "limit": 31,
    }, headers)
    mtd_cost = today_cost = 0.0
    for bucket in cost_buckets:
        day = str(bucket.get("starting_at", ""))[:10]
        for result in bucket.get("results", []):
            if not isinstance(result, dict):
                continue
            # cost_report amount is a decimal STRING in cents.
            amount = _num(result.get("amount")) / 100.0
            mtd_cost += amount
            if day == today_key:
                today_cost += amount

    per_model: dict = {}
    tok_totals = {"today": 0.0, "mtd": 0.0}
    usage_buckets = await _paged(eid, "/v1/organizations/usage_report/messages", {
        "starting_at": _rfc3339(month_start), "bucket_width": "1d", "limit": 31,
        "group_by[]": ["model"],
    }, headers)
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
            cw = (_num(creation.get("ephemeral_1h_input_tokens"))
                  + _num(creation.get("ephemeral_5m_input_tokens")))
            inp = _num(result.get("uncached_input_tokens"))
            out = _num(result.get("output_tokens"))
            cr = _num(result.get("cache_read_input_tokens"))
            for w in windows:
                d = per_model.setdefault((model, w), {"input": 0.0, "output": 0.0,
                                                      "cache_read": 0.0, "cache_write": 0.0})
                d["input"] += inp
                d["output"] += out
                d["cache_read"] += cr
                d["cache_write"] += cw
                tok_totals[w] += inp + out + cr + cw

    models = _rows_from_tokens(eid, name, per_model, "estimated")
    summary = _summary(
        eid, name, reachable=True, error=None, configured=True,
        cost={"today": {"amount": round(today_cost, 6), "basis": "actual"},
              "mtd": {"amount": round(mtd_cost, 6), "basis": "actual"}},
        tokens={"today": tok_totals["today"], "mtd": tok_totals["mtd"]},
        model_count=len({m for m, _ in per_model}),
    )
    return summary, models


async def _poll_openai() -> tuple[dict, list[dict]]:
    eid, name = "openai", "OpenAI"
    month_start = int(_month_start_dt().timestamp())
    today_start = int(_today_start_dt().timestamp())

    cost_buckets = await _paged(eid, "/v1/organization/costs", {
        "start_time": month_start, "bucket_width": "1d", "limit": 31,
    })
    mtd_cost = today_cost = 0.0
    for bucket in cost_buckets:
        bs = int(_num(bucket.get("start_time")))
        for result in bucket.get("results", []):
            if not isinstance(result, dict):
                continue
            amount = result.get("amount") or {}
            val = _num(amount.get("value"))
            mtd_cost += val
            if bs >= today_start:
                today_cost += val

    per_model: dict = {}
    tok_totals = {"today": 0.0, "mtd": 0.0}
    usage_buckets = await _paged(eid, "/v1/organization/usage/completions", {
        "start_time": month_start, "bucket_width": "1d", "limit": 31,
        "group_by[]": ["model"],
    })
    for bucket in usage_buckets:
        bs = int(_num(bucket.get("start_time")))
        windows = ["mtd"] + (["today"] if bs >= today_start else [])
        for result in bucket.get("results", []):
            if not isinstance(result, dict):
                continue
            model = result.get("model")
            if not model:
                continue
            inp = _num(result.get("input_tokens"))
            out = _num(result.get("output_tokens"))
            cached = _num(result.get("input_cached_tokens"))
            # OpenAI's input_tokens INCLUDES cached; split so cache is priced lower.
            uncached = max(inp - cached, 0.0)
            for w in windows:
                d = per_model.setdefault((model, w), {"input": 0.0, "output": 0.0,
                                                      "cache_read": 0.0, "cache_write": 0.0})
                d["input"] += uncached
                d["output"] += out
                d["cache_read"] += cached
                tok_totals[w] += inp + out

    models = _rows_from_tokens(eid, name, per_model, "estimated")
    summary = _summary(
        eid, name, reachable=True, error=None, configured=True,
        cost={"today": {"amount": round(today_cost, 6), "basis": "actual"},
              "mtd": {"amount": round(mtd_cost, 6), "basis": "actual"}},
        tokens={"today": tok_totals["today"], "mtd": tok_totals["mtd"]},
        model_count=len({m for m, _ in per_model}),
    )
    return summary, models


async def _poll_openrouter() -> tuple[dict, list[dict]]:
    eid, name = "openrouter", "OpenRouter"
    # /activity is account-wide per-model spend for the last 30 completed UTC days.
    # Needs a provisioning/management key; an inference key 4xxs (surfaced as error).
    payload = await _get_json(eid, "/activity")
    rows_data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows_data, list):
        raise ProviderError("activity needs a provisioning key (no per-model data returned)")

    per_model: dict = {}
    spend = tokens = 0.0
    for row in rows_data[:MAX_ACTIVITY_ROWS]:
        if not isinstance(row, dict):
            continue
        used = _num(row.get("usage"))
        spend += used
        model = row.get("model") or row.get("model_permaslug")
        if isinstance(model, str) and model:
            d = per_model.setdefault(model, {"cost": 0.0, "input": 0.0, "output": 0.0})
            d["cost"] += used
            d["input"] += _num(row.get("prompt_tokens"))
            d["output"] += _num(row.get("completion_tokens"))
            tokens += _num(row.get("prompt_tokens")) + _num(row.get("completion_tokens"))

    models = []
    for model, d in per_model.items():
        total = d["input"] + d["output"]
        models.append({
            "source": eid, "source_name": name, "model": model, "window": "30d",
            "input_tokens": d["input"], "output_tokens": d["output"], "cache_tokens": 0.0,
            "tokens": total, "cost": round(d["cost"], 6), "cost_basis": "actual",
            "currency": "USD",
        })
    summary = _summary(
        eid, name, reachable=True, error=None, configured=True,
        cost={"30d": {"amount": round(spend, 6), "basis": "actual"}},
        tokens={"30d": tokens}, model_count=len(per_model),
    )
    return summary, models


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


async def _safe_poll(eid: str, status: str) -> tuple[dict, list[dict]]:
    name = _NAMES[eid]
    if status != "active":
        return _unconfigured(eid, name), []
    try:
        return await _POLLERS[eid]()
    except (_host.HostError, ProviderError) as e:
        return _degraded(eid, name, str(e), True), []
    except Exception as e:  # noqa: BLE001 - one bad provider must not sink the view
        return _degraded(eid, name, f"unexpected: {e}", True), []


async def collect(force: bool = False) -> dict:
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["at"]) < CACHE_TTL:
        return _CACHE["data"]
    grants = await _grants()
    results = await asyncio.gather(*[_safe_poll(eid, grants.get(eid, "unknown")) for eid in ENDPOINTS])
    providers = [r[0] for r in results]
    models: list[dict] = []
    for r in results:
        models.extend(r[1])
    data = {"providers": providers, "models": models, "generated_at": int(now)}
    _CACHE["data"] = data
    _CACHE["at"] = now
    return data


def invalidate() -> None:
    _CACHE["data"] = None
    _CACHE["at"] = 0.0
