"""FastAPI routes for the Proxmox module.

  GET    /api/proxmox/cluster                          roll-up (nodes+guests+totals+stats), + settings
  GET    /api/proxmox/settings                         self-guest declaration + read-only + caps_denied
  POST   /api/proxmox/settings                         set self-guest / read-only / clear_denied
  POST   /api/proxmox/guests/{node}/{gtype}/{vmid}/{action}   gated power action
  GET    /api/proxmox/nextid                           next free vmid
  GET    /api/proxmox/nodes/{node}/provision-options   storage/iso/template/bridge pickers
  POST   /api/proxmox/provision/{node}/qemu            create VM from scratch
  POST   /api/proxmox/provision/{node}/lxc             create LXC from template
  POST   /api/proxmox/provision/{node}/{gtype}/{vmid}/clone   clone a guest
  DELETE /api/proxmox/guests/{node}/{gtype}/{vmid}      delete (type-to-confirm, stopped-only)
  GET    /api/proxmox/tasks/{node}/{upid}              poll an async PVE task
  GET    /api/proxmox/audit                            recent action audit rows
  GET    /api/proxmox/fleet-health                     contribution-API rows {rows:[...]}

Read-first. Every mutating action passes the SERVER-SIDE guard (read-only,
per-capability denial, self-guest protection) before any bridge call; the guard
refusal AND the upstream result are both audited. Identity comes from the host's
trusted X-AGD-User / X-AGD-Role headers (the host strips anything a browser sends
and authorizes by route class before this code runs); the role check here is
defense-in-depth, never the gate. An upstream 403 marks only that
action's capability (power vs allocate) as denied, so a partial-privilege token
degrades gracefully instead of locking the whole module.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import _host, client, guard, state

logger = logging.getLogger(__name__)

# No host auth dependency here: the host authenticates, CSRF-checks, and
# authorizes every /api/proxmox/* request by route class BEFORE it reaches this
# router, in every isolation mode, and stamps the trusted identity headers.
router = APIRouter(prefix="/api/proxmox", tags=["proxmox"])

_GTYPES = {"qemu", "lxc"}
_ACTIONS = {"start", "stop", "shutdown", "reboot"}
_ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}


def _actor(request: Request) -> str:
    # Trusted, host-stamped identity (a browser cannot set this header).
    return request.headers.get("x-agd-user") or "unknown"


def _require_operator(request: Request) -> None:
    """Defense-in-depth: the host already refused a viewer at the route class;
    refuse again here so a misconfigured host can never reach a mutation."""
    role = (request.headers.get("x-agd-role") or "").lower()
    if role and _ROLE_ORDER.get(role, 0) < _ROLE_ORDER["operator"]:
        raise HTTPException(status_code=403, detail="operator role required")


async def _finish_mutation(
    actor: str, node: str, gtype: str, vmid: int | None, action: str, result: dict,
):
    """Shared tail for every mutating action (power + provisioning). Audits the
    outcome, invalidates the cluster cache on success, and maps a 403 to a
    per-capability denial instead of a global read-only flip. On success returns
    `{ok, upid?}` (PVE mutations are async; `upid` is the task to poll)."""
    if result["ok"]:
        await state.add_audit(actor=actor, node=node, vmid=vmid, gtype=gtype, action=action, result="ok")
        client.invalidate_cache()
        out: dict = {"ok": True}
        if result.get("data"):
            out["upid"] = result["data"]
        return out

    if result["status"] == 403:
        cap = guard.CAP_OF.get(action, "power")
        await state.add_denied_cap(cap)
        await state.add_audit(actor=actor, node=node, vmid=vmid, gtype=gtype,
                              action=action, result="refused", reason=f"token lacks {cap} rights (403)")
        raise HTTPException(
            status_code=403,
            detail=f"this Proxmox token lacks {cap} rights; those controls are now hidden",
        )

    reason = (result.get("detail") or f"HTTP {result['status']}")[:200]
    await state.add_audit(actor=actor, node=node, vmid=vmid, gtype=gtype,
                          action=action, result="failed", reason=reason)
    if result["status"] == 0:
        # Host policy refusal (e.g. read-only endpoint grant), not a Proxmox answer.
        raise HTTPException(status_code=502, detail=f"host refused the {action}: {reason}")
    raise HTTPException(status_code=502, detail=f"Proxmox rejected the {action} (HTTP {result['status']})")


async def _settings_with_grant() -> dict:
    """Settings plus the host grant summary, so the UI can hide controls the
    operator did not grant (a read-only endpoint grant hides power/provision)."""
    settings = await state.get_settings()
    settings["grant"] = await _host.http_grant(client.ENDPOINT)
    return settings


@router.get("/cluster")
async def get_cluster(request: Request):
    cluster = await client.get_cluster_cached()
    settings = await _settings_with_grant()
    await guard.annotate_self(settings, cluster.get("nodes", []), cluster.get("guests", []))
    return {"cluster": cluster, "settings": settings}


@router.get("/settings")
async def get_settings():
    return await _settings_with_grant()


class SettingsPayload(BaseModel):
    self_node: str | None = None
    self_vmid: int | None = None
    self_type: str | None = None
    read_only: bool | None = None
    clear_denied: bool | None = None  # forget learned capability denials (re-probe on next action)


@router.post("/settings")
async def set_settings(payload: SettingsPayload, request: Request):
    _require_operator(request)
    if payload.self_type is not None and payload.self_type not in ("", *_GTYPES):
        raise HTTPException(status_code=400, detail="self_type must be 'qemu', 'lxc', or empty")
    return await state.save_settings(
        self_node=payload.self_node, self_vmid=payload.self_vmid,
        self_type=payload.self_type, read_only=payload.read_only,
        caps_denied=[] if payload.clear_denied else None,
    )


@router.post("/guests/{node}/{gtype}/{vmid}/{action}")
async def power_guest(node: str, gtype: str, vmid: int, action: str, request: Request):
    if gtype not in _GTYPES or action not in _ACTIONS:
        raise HTTPException(status_code=400, detail="invalid guest type or action")
    _require_operator(request)
    actor = _actor(request)

    allowed, reason = await guard.check_power(node, gtype, vmid, action)
    if not allowed:
        await state.add_audit(actor=actor, node=node, vmid=vmid, gtype=gtype,
                              action=action, result="refused", reason=reason)
        raise HTTPException(status_code=403, detail=reason)

    result = await client.guest_power(node, gtype, vmid, action)
    return await _finish_mutation(actor, node, gtype, vmid, action, result)


# ── provisioning: discovery, create, clone, delete, tasks ─────────────────────


@router.get("/nextid")
async def next_id():
    try:
        return {"vmid": await client.get_nextid()}
    except (client.ProxmoxError, _host.HostError) as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/nodes/{node}/provision-options")
async def provision_options(node: str):
    return await client.get_provision_options(node)


class VMCreate(BaseModel):
    vmid: int = Field(..., ge=100, le=999999999)
    name: str | None = None
    cores: int = Field(1, ge=1, le=1024)
    sockets: int = Field(1, ge=1, le=16)
    memory: int = Field(2048, ge=16)  # MiB
    storage: str = Field(..., min_length=1)
    disk_gb: int = Field(..., ge=1, le=65536)
    iso: str | None = None
    bridge: str = "vmbr0"
    vlan: int | None = Field(None, ge=1, le=4094)
    ostype: str = "l26"
    start: bool = False


class LXCCreate(BaseModel):
    vmid: int = Field(..., ge=100, le=999999999)
    hostname: str | None = None
    ostemplate: str = Field(..., min_length=1)
    storage: str = Field(..., min_length=1)
    disk_gb: int = Field(..., ge=1, le=65536)
    cores: int = Field(1, ge=1, le=1024)
    memory: int = Field(512, ge=16)
    swap: int = Field(512, ge=0)
    bridge: str = "vmbr0"
    vlan: int | None = Field(None, ge=1, le=4094)
    ip: str = "dhcp"
    password: str | None = None
    ssh_public_keys: str | None = None
    unprivileged: bool = True
    start: bool = False


class CloneReq(BaseModel):
    newid: int | None = Field(None, ge=100, le=999999999)
    name: str | None = None
    target: str | None = None
    full: bool = False


class DeleteConfirm(BaseModel):
    confirm_vmid: int


async def _refuse(actor, node, gtype, vmid, action, reason, code):
    await state.add_audit(actor=actor, node=node, vmid=vmid, gtype=gtype,
                          action=action, result="refused", reason=reason)
    raise HTTPException(status_code=code, detail=reason)


@router.post("/provision/{node}/qemu")
async def create_vm(node: str, payload: VMCreate, request: Request):
    _require_operator(request)
    actor = _actor(request)
    allowed, reason = await guard.check_action(node, "qemu", payload.vmid, "create")
    if not allowed:
        await _refuse(actor, node, "qemu", payload.vmid, "create", reason, 403)
    result = await client.create_vm(node, payload.model_dump())
    return await _finish_mutation(actor, node, "qemu", payload.vmid, "create", result)


@router.post("/provision/{node}/lxc")
async def create_lxc(node: str, payload: LXCCreate, request: Request):
    _require_operator(request)
    actor = _actor(request)
    allowed, reason = await guard.check_action(node, "lxc", payload.vmid, "create")
    if not allowed:
        await _refuse(actor, node, "lxc", payload.vmid, "create", reason, 403)
    result = await client.create_lxc(node, payload.model_dump())
    return await _finish_mutation(actor, node, "lxc", payload.vmid, "create", result)


@router.post("/provision/{node}/{gtype}/{vmid}/clone")
async def clone_guest(node: str, gtype: str, vmid: int, payload: CloneReq, request: Request):
    if gtype not in _GTYPES:
        raise HTTPException(status_code=400, detail="invalid guest type")
    _require_operator(request)
    actor = _actor(request)
    allowed, reason = await guard.check_action(node, gtype, None, "clone")
    if not allowed:
        await _refuse(actor, node, gtype, vmid, "clone", reason, 403)
    newid = payload.newid
    if newid is None:
        try:
            newid = await client.get_nextid()
        except (client.ProxmoxError, _host.HostError) as e:
            raise HTTPException(status_code=502, detail=f"could not allocate a vmid: {e}")
    result = await client.clone_guest(
        node, gtype, vmid, newid, name=payload.name, target=payload.target, full=payload.full,
    )
    return await _finish_mutation(actor, node, gtype, newid, "clone", result)


@router.delete("/guests/{node}/{gtype}/{vmid}")
async def delete_guest(node: str, gtype: str, vmid: int, payload: DeleteConfirm, request: Request):
    if gtype not in _GTYPES:
        raise HTTPException(status_code=400, detail="invalid guest type")
    _require_operator(request)
    actor = _actor(request)
    if payload.confirm_vmid != vmid:
        await _refuse(actor, node, gtype, vmid, "delete", "confirm_vmid does not match the target vmid", 400)
    allowed, reason = await guard.check_action(node, gtype, vmid, "delete")
    if not allowed:
        await _refuse(actor, node, gtype, vmid, "delete", reason, 403)
    # Enforce stopped-only against LIVE state, not the (up to 10s stale) cluster cache.
    live = await client.get_guest_status(node, gtype, vmid)
    if live.get("error"):
        await _refuse(actor, node, gtype, vmid, "delete", f"could not verify guest state: {live['error']}", 502)
    if live.get("status") and live["status"] != "stopped":
        await _refuse(actor, node, gtype, vmid, "delete", f"guest is {live['status']}; stop it before deleting", 409)
    result = await client.delete_guest(node, gtype, vmid)
    return await _finish_mutation(actor, node, gtype, vmid, "delete", result)


@router.get("/tasks/{node}/{upid}")
async def task_status(node: str, upid: str):
    return await client.task_status(node, upid)


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
