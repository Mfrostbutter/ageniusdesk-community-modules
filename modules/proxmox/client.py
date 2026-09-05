"""Thin Proxmox VE API wrapper over the host http.request bridge.

Every call goes through `_host.http_request` to the operator-consented `proxmox`
endpoint (base URL + token are host-side). Reads parse the PVE `{"data": [...]}`
envelope into a compact UI roll-up. Nothing here raises on a degraded cluster: an
unreachable or rejecting cluster comes back as `reachable=False` + an error string,
mirroring the host's own n8n `_instance_health` contract.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

from . import _host

ENDPOINT = "proxmox"


class ProxmoxError(RuntimeError):
    """A non-2xx or malformed response from the Proxmox API."""


async def _get(path: str, query: dict | None = None) -> Any:
    """GET a PVE path and return the unwrapped `data`. Raises ProxmoxError on a
    non-2xx status or an unparseable body."""
    resp = await _host.http_request(ENDPOINT, method="GET", path=path, query=query)
    status = resp.get("status", 0)
    if status < 200 or status >= 300:
        raise ProxmoxError(f"GET {path} -> HTTP {status}")
    try:
        return json.loads(resp.get("body") or "{}").get("data")
    except (ValueError, AttributeError) as e:
        raise ProxmoxError(f"GET {path}: bad response body ({e})")


def _pct(v: Any) -> int:
    try:
        return round(float(v) * 100)
    except (TypeError, ValueError):
        return 0


def _mem(used: Any, total: Any) -> dict:
    def _int(x: Any) -> int:
        try:
            return int(x)
        except (TypeError, ValueError):
            return 0
    u, t = _int(used), _int(total)
    return {"used": u, "total": t, "pct": round(u / t * 100) if t else 0}


async def get_cluster() -> dict:
    """One-shot cluster roll-up: nodes, guests, quorum, totals. Degraded-not-fatal.

    Shape:
      {reachable, error, quorate, nodes:[...], guests:[...],
       totals:{nodes, nodes_online, guests, running, stopped}}
    """
    out: dict[str, Any] = {
        "reachable": False, "error": "", "quorate": None,
        "nodes": [], "guests": [],
        "totals": {"nodes": 0, "nodes_online": 0, "guests": 0, "running": 0, "stopped": 0},
    }
    try:
        resources = await _get("/cluster/resources", {"type": "vm"}) or []
        nodes_raw = await _get("/nodes") or []
    except _host.HostError as e:
        out["error"] = str(e)
        return out
    except ProxmoxError as e:
        out["error"] = str(e)
        return out

    # Cluster status is optional (a single-node PVE returns a thin list); never fatal.
    try:
        status_raw = await _get("/cluster/status") or []
        for item in status_raw:
            if item.get("type") == "cluster":
                out["quorate"] = bool(item.get("quorate"))
    except (_host.HostError, ProxmoxError):
        pass

    nodes = []
    # Cluster-wide accumulators, online nodes only so a downed node doesn't skew
    # utilization. cpu is weighted by core count; mem/disk are plain byte sums.
    cpu_weighted = 0.0
    tot_cores = tot_mem_u = tot_mem_t = tot_disk_u = tot_disk_t = 0
    for n in nodes_raw:
        online = n.get("status") == "online"
        cores = int(n.get("maxcpu") or 0)
        mem = _mem(n.get("mem"), n.get("maxmem"))
        disk = _mem(n.get("disk"), n.get("maxdisk"))
        nodes.append({
            "name": n.get("node", ""),
            "online": online,
            "cores": cores,
            "cpu_pct": _pct(n.get("cpu")),
            "mem": mem,
            "disk": disk,
            "uptime": int(n.get("uptime") or 0),
        })
        if online:
            try:
                cpu_weighted += float(n.get("cpu") or 0) * cores
            except (TypeError, ValueError):
                pass
            tot_cores += cores
            tot_mem_u += mem["used"]
            tot_mem_t += mem["total"]
            tot_disk_u += disk["used"]
            tot_disk_t += disk["total"]
    guests = []
    running = 0
    for r in resources:
        gtype = r.get("type", "")  # 'qemu' | 'lxc'
        if gtype not in ("qemu", "lxc"):
            continue
        st = r.get("status", "")
        if st == "running":
            running += 1
        guests.append({
            "vmid": r.get("vmid"),
            "name": r.get("name", ""),
            "type": gtype,
            "node": r.get("node", ""),
            "status": st,
            "cpu_pct": _pct(r.get("cpu")),
            "mem": _mem(r.get("mem"), r.get("maxmem")),
            "uptime": int(r.get("uptime") or 0),
        })

    out["reachable"] = True
    out["nodes"] = sorted(nodes, key=lambda x: x["name"])
    out["guests"] = sorted(guests, key=lambda x: (x["node"], x["vmid"] or 0))
    out["totals"] = {
        "nodes": len(nodes),
        "nodes_online": sum(1 for n in nodes if n["online"]),
        "guests": len(guests),
        "running": running,
        "stopped": len(guests) - running,
        "cores": tot_cores,
        "cpu_pct": round(cpu_weighted / tot_cores * 100) if tot_cores else 0,
        "mem": {"used": tot_mem_u, "total": tot_mem_t,
                "pct": round(tot_mem_u / tot_mem_t * 100) if tot_mem_t else 0},
        "disk": {"used": tot_disk_u, "total": tot_disk_t,
                 "pct": round(tot_disk_u / tot_disk_t * 100) if tot_disk_t else 0},
    }
    return out


# Short TTL cache so a rapid poll (and the cheap fleet-health route) share one
# upstream fetch rather than hammering the PVE API on every request.
_cache: dict[str, Any] = {"t": 0.0, "v": None}


async def get_cluster_cached(ttl: float = 10.0) -> dict:
    now = time.monotonic()
    if _cache["v"] is not None and (now - _cache["t"]) < ttl:
        return _cache["v"]
    value = await get_cluster()
    # Only cache a reachable roll-up; a transient error should retry next call.
    if value.get("reachable"):
        _cache["t"] = now
        _cache["v"] = value
    return value


_GUEST_KIND = {"qemu": "qemu", "lxc": "lxc"}
_ACTIONS = {"start", "stop", "shutdown", "reboot"}


async def guest_power(node: str, gtype: str, vmid: int, action: str) -> dict:
    """POST a guest power action. Returns {ok, status, detail}. Self-protection +
    role gating happen in the router BEFORE this is called."""
    if gtype not in _GUEST_KIND or action not in _ACTIONS:
        return {"ok": False, "status": 400, "detail": f"invalid guest/action {gtype}/{action}"}
    path = f"/nodes/{node}/{_GUEST_KIND[gtype]}/{vmid}/status/{action}"
    try:
        resp = await _host.http_request(ENDPOINT, method="POST", path=path)
    except _host.HostError as e:
        return _host_refusal(e)
    status = resp.get("status", 0)
    ok = 200 <= status < 300
    detail = "" if ok else (resp.get("body") or "")[:300]
    return {"ok": ok, "status": status, "detail": detail}


# ── provisioning: discovery, create, clone, delete, task status ───────────────
#
# Create/clone/delete are ASYNC in PVE: a 2xx returns a UPID (task id) in `data`,
# and the work runs in the background. Callers surface the UPID and poll
# `task_status` to completion. Mutating results share the {ok,status,detail,data}
# shape; `data` is the UPID on success.


def _host_refusal(e: _host.HostError) -> dict:
    """The HOST refused the call (method not granted, endpoint pending, bad
    path). status=0 marks it as a host policy result, not an upstream status."""
    return {"ok": False, "status": 0, "detail": str(e)[:300], "data": None}


def _result(resp: dict) -> dict:
    status = resp.get("status", 0)
    ok = 200 <= status < 300
    data = None
    if ok:
        try:
            data = json.loads(resp.get("body") or "{}").get("data")
        except (ValueError, AttributeError):
            data = None
    detail = "" if ok else (resp.get("body") or "")[:300]
    return {"ok": ok, "status": status, "detail": detail, "data": data}


async def _post_form(path: str, form: dict) -> dict:
    """POST a PVE create/clone endpoint form-encoded. Empty/None values are
    dropped so PVE applies its own defaults. Form-encoded (not JSON) is the
    portable PVE contract and forwards verbatim through both transports."""
    payload = {k: v for k, v in form.items() if v is not None and v != ""}
    body = urllib.parse.urlencode(payload)
    try:
        resp = await _host.http_request(
            ENDPOINT, method="POST", path=path,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body,
        )
    except _host.HostError as e:
        return _host_refusal(e)
    return _result(resp)


async def get_nextid() -> int:
    """Next free vmid from the cluster (`/cluster/nextid`)."""
    data = await _get("/cluster/nextid")
    return int(data)


async def get_provision_options(node: str) -> dict:
    """Node-scoped pickers for the create wizard: storages (+ their content
    classes), install ISOs, LXC templates, and network bridges. Degraded-not-
    fatal: a failed sub-call yields an empty list plus a `warnings` entry."""
    out: dict[str, Any] = {"storages": [], "isos": [], "templates": [], "bridges": [], "warnings": []}
    try:
        storages = await _get(f"/nodes/{node}/storage") or []
    except (ProxmoxError, _host.HostError) as e:
        out["warnings"].append(f"storage: {e}")
        storages = []

    iso_stores: list[str] = []
    tmpl_stores: list[str] = []
    for s in storages:
        content = {c for c in (s.get("content") or "").split(",") if c}
        name = s.get("storage", "")
        active = bool(s.get("active", 1))
        out["storages"].append({
            "name": name,
            "content": sorted(content),
            "avail": int(s.get("avail") or 0),
            "total": int(s.get("total") or 0),
            "active": active,
        })
        if active and "iso" in content:
            iso_stores.append(name)
        if active and "vztmpl" in content:
            tmpl_stores.append(name)

    async def _content(store: str, kind: str) -> list[dict]:
        items = await _get(f"/nodes/{node}/storage/{store}/content", {"content": kind}) or []
        return [{"volid": it.get("volid"), "size": int(it.get("size") or 0), "storage": store}
                for it in items]

    for store in iso_stores:
        try:
            out["isos"].extend(await _content(store, "iso"))
        except (ProxmoxError, _host.HostError) as e:
            out["warnings"].append(f"iso@{store}: {e}")
    for store in tmpl_stores:
        try:
            out["templates"].extend(await _content(store, "vztmpl"))
        except (ProxmoxError, _host.HostError) as e:
            out["warnings"].append(f"vztmpl@{store}: {e}")

    try:
        nets = await _get(f"/nodes/{node}/network", {"type": "any_bridge"}) or []
        out["bridges"] = [{"iface": nw.get("iface", ""), "active": bool(nw.get("active", 1))}
                          for nw in nets if nw.get("iface")]
    except (ProxmoxError, _host.HostError) as e:
        out["warnings"].append(f"bridges: {e}")

    return out


def _net0_vm(spec: dict) -> str:
    n = f"virtio,bridge={spec.get('bridge', 'vmbr0')}"
    if spec.get("vlan"):
        n += f",tag={int(spec['vlan'])}"
    return n


def _net0_lxc(spec: dict) -> str:
    n = f"name=eth0,bridge={spec.get('bridge', 'vmbr0')},ip={spec.get('ip', 'dhcp')}"
    if spec.get("vlan"):
        n += f",tag={int(spec['vlan'])}"
    return n


async def create_vm(node: str, spec: dict) -> dict:
    """Create a VM from scratch (`POST /nodes/{node}/qemu`). `spec` is the
    validated request; the PVE param strings are assembled here."""
    form = {
        "vmid": spec["vmid"],
        "name": spec.get("name") or None,
        "cores": spec.get("cores", 1),
        "sockets": spec.get("sockets", 1),
        "memory": spec.get("memory", 2048),
        "ostype": spec.get("ostype", "l26"),
        "scsihw": "virtio-scsi-single",
        "scsi0": f"{spec['storage']}:{spec['disk_gb']}",
        "net0": _net0_vm(spec),
        "boot": "order=scsi0" + (";ide2" if spec.get("iso") else ""),
        "onboot": 0,
    }
    if spec.get("iso"):
        form["ide2"] = f"{spec['iso']},media=cdrom"
    return await _post_form(f"/nodes/{node}/qemu", form)


async def create_lxc(node: str, spec: dict) -> dict:
    """Create an LXC from a template (`POST /nodes/{node}/lxc`). Secret material
    (password, ssh keys) flows to PVE but is never returned or logged."""
    form = {
        "vmid": spec["vmid"],
        "hostname": spec.get("hostname") or None,
        "ostemplate": spec["ostemplate"],
        "storage": spec["storage"],
        "rootfs": f"{spec['storage']}:{spec['disk_gb']}",
        "cores": spec.get("cores", 1),
        "memory": spec.get("memory", 512),
        "swap": spec.get("swap", 512),
        "net0": _net0_lxc(spec),
        "unprivileged": 1 if spec.get("unprivileged", True) else 0,
    }
    if spec.get("password"):
        form["password"] = spec["password"]
    if spec.get("ssh_public_keys"):
        form["ssh-public-keys"] = spec["ssh_public_keys"]
    return await _post_form(f"/nodes/{node}/lxc", form)


async def clone_guest(
    node: str, gtype: str, vmid: int, newid: int,
    name: str | None = None, target: str | None = None, full: bool = False,
) -> dict:
    """Clone a guest to `newid` (`POST /nodes/{node}/{gtype}/{vmid}/clone`). LXC
    is always a full clone; qemu honors `full`."""
    if gtype not in _GUEST_KIND:
        return {"ok": False, "status": 400, "detail": f"invalid guest type {gtype}", "data": None}
    form: dict[str, Any] = {"newid": newid}
    if gtype == "qemu":
        form["full"] = 1 if full else 0
        if name:
            form["name"] = name
    elif name:
        form["hostname"] = name
    if target:
        form["target"] = target
    return await _post_form(f"/nodes/{node}/{gtype}/{vmid}/clone", form)


async def delete_guest(node: str, gtype: str, vmid: int) -> dict:
    """Destroy a guest (`DELETE /nodes/{node}/{gtype}/{vmid}`), purging its
    config and disks. Stopped-state + self-guard are enforced by the router
    BEFORE this is called."""
    if gtype not in _GUEST_KIND:
        return {"ok": False, "status": 400, "detail": f"invalid guest type {gtype}", "data": None}
    query: dict[str, Any] = {"purge": 1}
    if gtype == "qemu":
        query["destroy-unreferenced-disks"] = 1
    try:
        resp = await _host.http_request(
            ENDPOINT, method="DELETE", path=f"/nodes/{node}/{gtype}/{vmid}", query=query,
        )
    except _host.HostError as e:
        return _host_refusal(e)
    return _result(resp)


async def get_guest_status(node: str, gtype: str, vmid: int) -> dict:
    """Current run-state of one guest (`.../status/current`). Used to enforce
    the stopped-only delete precondition against live state, not a cached poll.
    Returns {status, error}."""
    if gtype not in _GUEST_KIND:
        return {"status": "", "error": f"invalid guest type {gtype}"}
    try:
        data = await _get(f"/nodes/{node}/{gtype}/{vmid}/status/current") or {}
    except (ProxmoxError, _host.HostError) as e:
        return {"status": "", "error": str(e)}
    return {"status": data.get("status", ""), "error": ""}


async def task_status(node: str, upid: str) -> dict:
    """Poll a PVE task to completion. The UPID contains `@` and `:`, both of
    which the host path validator rejects raw, so it is percent-encoded into a
    single path segment. Returns {status, exitstatus, done, ok, error}."""
    enc = urllib.parse.quote(upid, safe="")
    try:
        data = await _get(f"/nodes/{node}/tasks/{enc}/status") or {}
    except (ProxmoxError, _host.HostError) as e:
        return {"status": "", "exitstatus": "", "done": False, "ok": False, "error": str(e)}
    status = data.get("status", "")
    exitstatus = data.get("exitstatus", "")
    done = status == "stopped"
    return {"status": status, "exitstatus": exitstatus, "done": done,
            "ok": done and exitstatus == "OK", "error": ""}


def invalidate_cache() -> None:
    """Drop the cached cluster roll-up so the next poll re-fetches upstream."""
    _cache["v"] = None
