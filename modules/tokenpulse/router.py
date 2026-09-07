"""FastAPI routes for the TokenPulse module.

  GET  /api/tokenpulse/summary       providers + quotas + spend trend (own view)
  GET  /api/tokenpulse/quotas        just the quota list, loudest first
  GET  /api/tokenpulse/card          Main Dashboard 'AI Quotas' card JSON
  GET  /api/tokenpulse/fleet-health  Fleet Health contribution rows {rows:[...]}
  GET  /api/tokenpulse/settings      current per-provider monthly budgets + grants
  POST /api/tokenpulse/settings      set per-provider monthly budgets (operator)
  POST /api/tokenpulse/refresh       drop the poll cache and re-fetch (operator)

Read-only against the providers. Identity comes from the host's trusted
X-AGD-User / X-AGD-Role headers; the operator checks here are defense-in-depth.
Admin keys never touch this code: client.py calls out through the host bridge.
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


def _fmt_quota_value(q: dict) -> str:
    """A compact 'used / limit' or 'remaining left' for a quota row."""
    if q["pct"] is not None:
        return f"{q['pct']:.0f}%"
    return _usd(q.get("used", 0.0))


@router.get("/summary")
async def get_summary():
    return await client.collect()


@router.get("/quotas")
async def get_quotas():
    data = await client.collect()
    return {"quotas": data["quotas"], "generated_at": data["generated_at"]}


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
            "quotas": len(data["quotas"]),
            "providers": len([p for p in data["providers"] if p.get("reachable")])}


@router.get("/card")
async def dashboard_card():
    """Main Dashboard 'AI Quotas' card: the tightest quota up top, plus the next
    couple, so the operator sees what will hurt first at a glance."""
    data = await client.collect()
    quotas = data["quotas"]
    configured = [p for p in data["providers"] if p.get("configured")]

    if not configured:
        return {"metrics": [], "rows": [],
                "footer": "No providers configured — add keys in Settings ▸ Modules",
                "link": "community:tokenpulse"}
    if not quotas:
        return {"metrics": [], "rows": [],
                "footer": "No quotas yet — set a monthly budget in TokenPulse, or add an OpenRouter key",
                "link": "community:tokenpulse"}

    top = quotas[0]
    metrics = [{"label": top["label"], "value": _fmt_quota_value(top),
                "sub": _usd(top.get("remaining", 0.0)) + " left" if top["pct"] is not None else None}]
    rows = [{
        "label": q["label"],
        "value": _fmt_quota_value(q),
        "sub": _usd(q.get("remaining", 0.0)) + " left" if q["pct"] is not None else q["provider_name"],
    } for q in quotas[1:5]]
    unreachable = [p["name"] for p in configured if not p.get("reachable")]
    footer = f"{len(quotas)} quota{'s' if len(quotas) != 1 else ''}" + (
        f" · {', '.join(unreachable)} unreachable" if unreachable else "")
    return {"metrics": metrics, "rows": rows, "footer": footer, "link": "community:tokenpulse"}


@router.get("/fleet-health")
async def fleet_health():
    """Fleet Health contribution: one row for the tightest AI quota."""
    data = await client.collect()
    quotas = data["quotas"]
    configured = [p for p in data["providers"] if p.get("configured")]
    reachable = [p for p in configured if p.get("reachable")]

    metrics, status, err, top_label = [], "ok", "", "AI Quotas"
    if not configured:
        status, err = "unconfigured", "no providers configured"
    elif not reachable:
        status, err = "down", "; ".join(p.get("error") or "" for p in configured)[:120]
    elif quotas and quotas[0]["pct"] is not None:
        top = quotas[0]
        top_label = top["label"]
        pct = top["pct"]
        status = "error" if pct >= 90 else "degraded" if pct >= 75 else "ok"
        metrics = [
            {"label": "tightest", "value": f"{pct:.0f}%"},
            {"label": "left", "value": _usd(top.get("remaining", 0.0))},
            {"label": "quotas", "value": len(quotas)},
        ]
    elif len(reachable) < len(configured):
        status = "degraded"

    row = {
        "id": "tokenpulse:quotas", "kind": "quota", "label": top_label,
        "reachable": bool(reachable), "status": status, "error": err,
        "metrics": metrics, "detail_url": "/modules/tokenpulse",
    }
    return {"rows": [row]}
