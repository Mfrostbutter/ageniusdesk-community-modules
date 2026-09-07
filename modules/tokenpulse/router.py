"""FastAPI routes for TokenPulse (consolidated cost + quota observability).

  GET  /api/tokenpulse/summary       providers (each with spend, per-model rows,
                                      quotas, trend) + top-level quotas/models/trend
  GET  /api/tokenpulse/card          Main Dashboard 'AI Model Spend' card JSON
  GET  /api/tokenpulse/card-quotas   Main Dashboard 'AI Quotas' card JSON
  GET  /api/tokenpulse/fleet-health  Fleet Health contribution rows {rows:[...]}
  GET  /api/tokenpulse/settings      per-provider monthly budgets + endpoint grants
  POST /api/tokenpulse/settings      set per-provider monthly budgets (operator)
  POST /api/tokenpulse/refresh       drop the poll cache and re-fetch (operator)

Read-only against the providers. Identity comes from the host's trusted
X-AGD-User / X-AGD-Role headers; operator checks here are defense-in-depth. Admin
keys never touch this code: client.py calls out through the host bridge.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import _host, client, state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tokenpulse", tags=["tokenpulse"])

_ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}


def _require_operator(request: Request) -> None:
    role = (request.headers.get("x-agd-role") or "").lower()
    if role and _ROLE_ORDER.get(role, 0) < _ROLE_ORDER["operator"]:
        raise HTTPException(status_code=403, detail="operator role required")


def _usd(amount: float) -> str:
    try:
        return "${:,.2f}".format(float(amount))
    except (TypeError, ValueError):
        return "$0.00"


def _short_model(name: str) -> str:
    n = str(name or "")
    if "/" in n:
        n = n.split("/", 1)[1]
    return n[:32]


def _sum_spend(providers: list[dict], window: str) -> float:
    return round(sum(float((p.get("spend") or {}).get(window, 0) or 0) for p in providers), 6)


def _primary_models(models: list[dict]) -> list[dict]:
    """One comparable set for a card: each provider's best window (mtd for the
    estimated providers, 30d for OpenRouter's actual spend)."""
    have_mtd = {m["source"] for m in models if m["window"] == "mtd"}
    out = []
    for m in models:
        if m["window"] == "mtd":
            out.append(m)
        elif m["window"] == "30d" and m["source"] not in have_mtd:
            out.append(m)
    return out


@router.get("/summary")
async def get_summary():
    return await client.collect()


@router.get("/settings")
async def get_settings():
    grants = []
    for eid in client.ENDPOINTS:
        g = await _host.http_grant(eid)
        grants.append({"id": eid, "name": client._NAMES[eid],
                       "status": g.get("status", "unknown"), "host": g.get("host", "")})
    return {"budgets": state.get_budgets(), "providers": grants}


class BudgetPayload(BaseModel):
    budgets: dict[str, float]


@router.post("/settings")
async def set_settings(payload: BudgetPayload, request: Request):
    _require_operator(request)
    budgets = state.set_budgets(payload.budgets or {})
    client.invalidate()
    return {"ok": True, "budgets": budgets}


@router.post("/refresh")
async def refresh(request: Request):
    _require_operator(request)
    client.invalidate()
    data = await client.collect(force=True)
    return {"ok": True, "generated_at": data["generated_at"],
            "providers": len([p for p in data["providers"] if p.get("reachable")]),
            "quotas": len(data["quotas"])}


@router.get("/card")
async def dashboard_card():
    """'AI Model Spend' card: actual spend today + month, plus the top models."""
    data = await client.collect()
    providers = data["providers"]
    configured = [p for p in providers if p.get("configured")]
    if not configured:
        return {"metrics": [], "rows": [],
                "footer": "No providers configured — add keys in Settings ▸ Modules",
                "link": "community:tokenpulse"}

    metrics = [{"label": "Today", "value": _usd(_sum_spend(providers, "today"))},
               {"label": "Month", "value": _usd(_sum_spend(providers, "mtd"))}]
    top = sorted(_primary_models(data["models"]), key=lambda m: m.get("cost") or 0.0, reverse=True)[:5]
    rows = [{"label": _short_model(m["model"]), "value": _usd(m.get("cost") or 0.0),
             "sub": m["source_name"] + (" · est" if m.get("cost_basis") == "estimated" else "")}
            for m in top if (m.get("cost") or 0.0) > 0]
    unreachable = [p["name"] for p in configured if not p.get("reachable")]
    footer = "estimated where marked" + (f" · {', '.join(unreachable)} unreachable" if unreachable else "")
    return {"metrics": metrics, "rows": rows, "footer": footer, "link": "community:tokenpulse"}


@router.get("/card-quotas")
async def dashboard_card_quotas():
    """'AI Quotas' card: the tightest quota up top, the next few below."""
    data = await client.collect()
    quotas = data["quotas"]
    configured = [p for p in data["providers"] if p.get("configured")]
    if not configured:
        return {"metrics": [], "rows": [],
                "footer": "No providers configured — add keys in Settings ▸ Modules",
                "link": "community:tokenpulse"}
    if not quotas:
        return {"metrics": [], "rows": [],
                "footer": "No quotas yet — set a monthly budget or add an OpenRouter key",
                "link": "community:tokenpulse"}

    def val(q):
        return f"{q['pct']:.0f}%" if q["pct"] is not None else _usd(q.get("used", 0.0))

    top = quotas[0]
    metrics = [{"label": top["label"], "value": val(top),
                "sub": _usd(top.get("remaining", 0.0)) + " left" if top["pct"] is not None else None}]
    rows = [{"label": q["label"], "value": val(q),
             "sub": _usd(q.get("remaining", 0.0)) + " left" if q["pct"] is not None else q["provider_name"]}
            for q in quotas[1:5]]
    return {"metrics": metrics, "rows": rows,
            "footer": f"{len(quotas)} quota{'s' if len(quotas) != 1 else ''}", "link": "community:tokenpulse"}


@router.get("/fleet-health")
async def fleet_health():
    """Fleet Health contribution: one 'AI Usage' row (spend + tightest quota)."""
    data = await client.collect()
    providers = data["providers"]
    quotas = data["quotas"]
    configured = [p for p in providers if p.get("configured")]
    reachable = [p for p in configured if p.get("reachable")]

    if not configured:
        status, err = "unconfigured", "no providers configured"
    elif not reachable:
        status, err = "down", "; ".join(p.get("error") or "" for p in configured)[:120]
    else:
        status, err = "ok", ""
        if quotas and quotas[0]["pct"] is not None:
            pct = quotas[0]["pct"]
            status = "error" if pct >= 90 else "degraded" if pct >= 75 else "ok"
        if status == "ok" and len(reachable) < len(configured):
            status = "degraded"

    metrics = [
        {"label": "today", "value": _usd(_sum_spend(providers, "today"))},
        {"label": "month", "value": _usd(_sum_spend(providers, "mtd"))},
    ]
    if quotas and quotas[0]["pct"] is not None:
        metrics.append({"label": "tightest", "value": f"{quotas[0]['pct']:.0f}%"})
    row = {"id": "tokenpulse:usage", "kind": "cost", "label": "AI Usage",
           "reachable": bool(reachable), "status": status, "error": err,
           "metrics": metrics, "detail_url": "/modules/tokenpulse"}
    return {"rows": [row]}
