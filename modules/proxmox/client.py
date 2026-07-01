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
    for n in nodes_raw:
        online = n.get("status") == "online"
        nodes.append({
            "name": n.get("node", ""),
            "online": online,
            "cpu_pct": _pct(n.get("cpu")),
            "mem": _mem(n.get("mem"), n.get("maxmem")),
            "uptime": int(n.get("uptime") or 0),
        })
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
    resp = await _host.http_request(ENDPOINT, method="POST", path=path)
    status = resp.get("status", 0)
    ok = 200 <= status < 300
    detail = "" if ok else (resp.get("body") or "")[:300]
    return {"ok": ok, "status": status, "detail": detail}
