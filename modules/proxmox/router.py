"""FastAPI routes for the Proxmox module.

  GET  /api/proxmox/cluster                         cluster roll-up (nodes+guests+totals),
                                                     self-guest/self-node annotated, + settings
  GET  /api/proxmox/settings                         self-guest declaration + read-only flag
  POST /api/proxmox/settings                         set self-guest / read-only
  POST /api/proxmox/guests/{node}/{gtype}/{vmid}/{action}   gated power action
  GET  /api/proxmox/audit                            recent power-action audit rows
  GET  /api/proxmox/fleet-health                     contribution-API rows {rows:[...]}

Read-first. Every power action passes the SERVER-SIDE self-protection guard
before any bridge POST; the guard refusal AND the upstream result are both
audited. An upstream 403 (e.g. an auditor-only token) flips the install to
read-only on first hit (react-to-first-403, no privilege probe).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import _host, client, guard, state

logger = logging.getLogger(__name__)

if _host.ISOLATED:
    _ROUTE_DEPS: list = []
else:
    from backend.auth_gate import require_trusted_request
    from fastapi import Depends
    _ROUTE_DEPS = [Depends(require_trusted_request)]

router = APIRouter(prefix="/api/proxmox", tags=["proxmox"], dependencies=_ROUTE_DEPS)

_GTYPES = {"qemu", "lxc"}
_ACTIONS = {"start", "stop", "shutdown", "reboot"}


def _actor(request: Request) -> str:
    # The host proxy MAY forward a minimal role header; default to 'operator'.
    return request.headers.get("x-agd-user") or "operator"


@router.get("/cluster")
async def get_cluster(request: Request):
    cluster = await client.get_cluster_cached()
    settings = await state.get_settings()
    await guard.annotate_self(settings, cluster.get("nodes", []), cluster.get("guests", []))
    return {"cluster": cluster, "settings": settings}


@router.get("/settings")
async def get_settings():
    return await state.get_settings()


class SettingsPayload(BaseModel):
    self_node: str | None = None
    self_vmid: int | None = None
    self_type: str | None = None
    read_only: bool | None = None


@router.post("/settings")
async def set_settings(payload: SettingsPayload):
    if payload.self_type is not None and payload.self_type not in ("", *_GTYPES):
        raise HTTPException(status_code=400, detail="self_type must be 'qemu', 'lxc', or empty")
    return await state.save_settings(
        self_node=payload.self_node, self_vmid=payload.self_vmid,
        self_type=payload.self_type, read_only=payload.read_only,
    )


@router.post("/guests/{node}/{gtype}/{vmid}/{action}")
async def power_guest(node: str, gtype: str, vmid: int, action: str, request: Request):
    if gtype not in _GTYPES or action not in _ACTIONS:
        raise HTTPException(status_code=400, detail="invalid guest type or action")
    actor = _actor(request)

    allowed, reason = await guard.check_power(node, gtype, vmid, action)
    if not allowed:
        await state.add_audit(actor=actor, node=node, vmid=vmid, gtype=gtype,
                              action=action, result="refused", reason=reason)
        raise HTTPException(status_code=403, detail=reason)

    result = await client.guest_power(node, gtype, vmid, action)
    if result["ok"]:
        await state.add_audit(actor=actor, node=node, vmid=vmid, gtype=gtype, action=action, result="ok")
        client._cache["v"] = None  # invalidate so the next poll reflects the new state
        return {"ok": True}

    # Upstream refused. A 403 means the token lacks power rights → degrade the whole
    # install to read-only (react-to-first-403), so the UI stops offering dead buttons.
    if result["status"] == 403:
        await state.save_settings(read_only=True)
        await state.add_audit(actor=actor, node=node, vmid=vmid, gtype=gtype,
                              action=action, result="refused", reason="token lacks power rights (403)")
        raise HTTPException(
            status_code=403,
            detail="this Proxmox token lacks power-management rights; switched to read-only",
        )
    await state.add_audit(actor=actor, node=node, vmid=vmid, gtype=gtype,
                          action=action, result="failed", reason=f"HTTP {result['status']}")
    raise HTTPException(status_code=502, detail=f"Proxmox rejected the action (HTTP {result['status']})")


@router.get("/audit")
async def get_audit(limit: int = 100):
    return {"audit": await state.list_audit(limit)}


@router.get("/fleet-health")
async def fleet_health():
    """Contribution-API rows the host pulls. Cheap: serves the short-TTL cached
    cluster roll-up, not a fresh upstream call on every host poll."""
    c = await client.get_cluster_cached()
    t = c.get("totals", {})
    if not c.get("reachable"):
        status = "down"
    elif c.get("quorate") is False:
        status = "degraded"
    else:
        status = "ok"
    row = {
        "id": "proxmox:cluster",
        "kind": "proxmox",
        "label": "Proxmox",
        "reachable": bool(c.get("reachable")),
        "status": status,
        "error": c.get("error", ""),
        "metrics": [
            {"label": "nodes", "value": t.get("nodes", 0)},
            {"label": "running", "value": t.get("running", 0)},
            {"label": "stopped", "value": t.get("stopped", 0)},
        ],
        "detail_url": "/modules/proxmox",
    }
    return {"rows": [row]}
