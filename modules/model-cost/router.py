"""FastAPI routes for the Model Cost module.

  GET  /api/model-cost/summary       providers + per-model rows (own view feeds off this)
  GET  /api/model-cost/card          Main Dashboard card JSON (contribution)
  GET  /api/model-cost/fleet-health  Fleet Health contribution rows {rows:[...]}
  GET  /api/model-cost/settings      per-provider grant + configured status
  POST /api/model-cost/refresh       drop the poll cache and re-fetch (operator)

Read-only. Identity comes from the host's trusted X-AGD-User / X-AGD-Role headers
(the host strips anything a browser sends and authorizes by route class before
this code runs); the operator check on /refresh is defense-in-depth. Provider
admin keys never touch this code: client.py calls out through the host bridge.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from . import _host, client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/model-cost", tags=["model-cost"])

_ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}
# Windows a provider reports actual cost for, in card/fleet-health headline order.
_ACTUAL_WINDOWS = ("today", "mtd", "30d")


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
    """Trim a provider-qualified slug to something a narrow card can show."""
    n = str(name or "")
    if "/" in n:
        n = n.split("/", 1)[1]
    return n[:32]


def _sum_actual(providers: list[dict], window: str) -> float:
    total = 0.0
    for p in providers:
        entry = (p.get("cost") or {}).get(window)
        if entry:
            total += float(entry.get("amount") or 0.0)
    return round(total, 6)


def _primary_rows(models: list[dict]) -> list[dict]:
    """One comparable set for a glanceable card: each provider's best window
    (mtd for the estimated providers, 30d for OpenRouter's actual spend)."""
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
    """Everything the own view needs: provider summaries + all per-model rows
    across windows. The view tabs by window client-side."""
    return await client.collect()


@router.get("/settings")
async def get_settings():
    """Per-provider grant + configured status, so the UI can point the operator
    at Settings ▸ Modules for the ones still to set up."""
    out = []
    for eid in client.ENDPOINTS:
        grant = await _host.http_grant(eid)
        out.append({
            "id": eid,
            "name": client._NAMES[eid],
            "status": grant.get("status", "unknown"),
            "host": grant.get("host", ""),
        })
    return {"providers": out}


@router.post("/refresh")
async def refresh(request: Request):
    _require_operator(request)
    client.invalidate()
    data = await client.collect(force=True)
    return {"ok": True, "generated_at": data["generated_at"],
            "providers": len([p for p in data["providers"] if p.get("reachable")])}


@router.get("/card")
async def dashboard_card():
    """Main Dashboard card JSON. Headline actual spend (today + month), a small
    top-models-by-cost list, and a link into the module view."""
    data = await client.collect()
    providers = data["providers"]
    configured = [p for p in providers if p.get("configured")]

    if not configured:
        return {
            "metrics": [], "rows": [],
            "footer": "No providers configured — add keys in Settings ▸ Modules",
            "link": "community:model-cost",
        }

    today = _sum_actual(providers, "today")
    mtd = _sum_actual(providers, "mtd")
    metrics = [
        {"label": "Today", "value": _usd(today)},
        {"label": "Month", "value": _usd(mtd)},
    ]
    or_30d = next((p for p in providers if p["id"] == "openrouter" and p.get("cost", {}).get("30d")), None)
    if or_30d:
        metrics.append({"label": "OpenRouter 30d", "value": _usd(or_30d["cost"]["30d"]["amount"])})

    top = sorted(_primary_rows(data["models"]), key=lambda m: m.get("cost") or 0.0, reverse=True)[:5]
    rows = [{
        "label": _short_model(m["model"]),
        "value": _usd(m.get("cost") or 0.0),
        "sub": m["source_name"] + (" · est" if m.get("cost_basis") == "estimated" else ""),
    } for m in top if (m.get("cost") or 0.0) > 0]

    unreachable = [p["name"] for p in configured if not p.get("reachable")]
    footer = "estimated where marked" + (f" · {', '.join(unreachable)} unreachable" if unreachable else "")
    return {"metrics": metrics, "rows": rows, "footer": footer, "link": "community:model-cost"}


@router.get("/fleet-health")
async def fleet_health():
    """Fleet Health contribution: one spend row summarizing the fleet's AI cost."""
    data = await client.collect()
    providers = data["providers"]
    configured = [p for p in providers if p.get("configured")]
    reachable = [p for p in configured if p.get("reachable")]

    if not configured:
        status, err = "unconfigured", "no providers configured"
    elif not reachable:
        status, err = "down", "; ".join(p.get("error") or "" for p in configured)[:120]
    elif len(reachable) < len(configured):
        status, err = "degraded", "; ".join(p["name"] for p in configured if not p.get("reachable")) + " unreachable"
    else:
        status, err = "ok", ""

    row = {
        "id": "model-cost:spend",
        "kind": "cost",
        "label": "AI Spend",
        "reachable": bool(reachable),
        "status": status,
        "error": err,
        "metrics": [
            {"label": "today", "value": _usd(_sum_actual(providers, "today"))},
            {"label": "month", "value": _usd(_sum_actual(providers, "mtd"))},
            {"label": "models", "value": sum(p.get("model_count", 0) for p in providers)},
        ],
        "detail_url": "/modules/model-cost",
    }
    return {"rows": [row]}
