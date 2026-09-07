"""Host-services facade: dual-mode (isolated worker vs in_process).

The one place that knows whether we run sandboxed or in the host process:

  - ISOLATED (AGD_BRIDGE_URL set): every outbound Proxmox call goes through the
    host `http.request` bridge over loopback. The worker names the
    operator-consented endpoint id + a relative path; the HOST owns the base URL,
    the token, the TLS policy, and the pinned address.
  - in_process: the same host implementation is called directly
    (`backend.modules._runtime.http_bridge`). No manifest reading, no secret
    store, no direct Proxmox connection from module code.

Either way callers use `http_request(...)` and get the same
`{status, headers, body, truncated[, content_encoding]}` shape, and
`http_grant(...)` reports what the operator granted. This facade is
Proxmox-agnostic; any homelab module can reuse it verbatim.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

MODULE_ID = "model-cost"
ISOLATED = bool(os.environ.get("AGD_BRIDGE_URL"))
_BRIDGE_URL = os.environ.get("AGD_BRIDGE_URL", "").rstrip("/")
_BRIDGE_TOKEN = os.environ.get("AGD_BRIDGE_TOKEN", "")
_TIMEOUT = 35.0


class HostError(RuntimeError):
    """A host bridge / host-call failure with an operator-facing message."""


# ── isolated transport (bridge) ───────────────────────────────────────────────


def _bridge_detail(r: httpx.Response) -> str:
    try:
        return str(r.json().get("detail"))
    except Exception:
        return r.text[:200]


async def _http_via_bridge(payload: dict) -> dict:
    headers = {"authorization": f"Bearer {_BRIDGE_TOKEN}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_BRIDGE_URL}/api/_host/http/request", json=payload, headers=headers)
    if r.status_code >= 400:
        raise HostError(f"http.request failed (HTTP {r.status_code}): {_bridge_detail(r)}")
    return r.json()


async def _grant_via_bridge() -> list[dict]:
    headers = {"authorization": f"Bearer {_BRIDGE_TOKEN}"}
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{_BRIDGE_URL}/api/_host/http/endpoints", headers=headers)
    if r.status_code >= 400:
        raise HostError(f"http.endpoints failed (HTTP {r.status_code}): {_bridge_detail(r)}")
    return list(r.json().get("endpoints") or [])


# ── in_process transport (same host implementation, called directly) ─────────


async def _http_in_process(payload: dict) -> dict:
    from backend.modules._runtime import http_bridge  # host facade; the one permitted host import

    try:
        return await http_bridge.request(MODULE_ID, payload)
    except http_bridge.HttpBridgeError as e:
        raise HostError(f"http.request failed (HTTP {e.status}): {e.detail}")


def _grant_in_process() -> list[dict]:
    from backend.modules._runtime import http_bridge

    return http_bridge.grant_summary(MODULE_ID)


# ── public API ────────────────────────────────────────────────────────────────


async def http_request(
    endpoint: str,
    method: str = "GET",
    path: str = "",
    query: dict | None = None,
    headers: dict | None = None,
    body: Any = None,
) -> dict:
    """Make a host-mediated outbound HTTP call to a declared endpoint.

    Returns `{status, headers, body, truncated}` where `status` is the UPSTREAM
    HTTP status (a 404 from Proxmox is a normal return with status=404, not an
    exception). Raises HostError only on a bridge/transport failure or a policy
    rejection (unknown endpoint, method not granted, bad path, pending consent).
    """
    payload = {"endpoint": endpoint, "method": method, "path": path,
               "query": query, "headers": headers, "body": body}
    if ISOLATED:
        return await _http_via_bridge(payload)
    return await _http_in_process(payload)


async def http_grant(endpoint: str) -> dict:
    """What the operator granted on `endpoint`: `{status, methods, host, mutating}`.
    Degrades to an empty grant (never raises) so the UI can still render."""
    try:
        eps = await _grant_via_bridge() if ISOLATED else _grant_in_process()
    except Exception:
        eps = []
    for ep in eps:
        if ep.get("id") == endpoint:
            methods = [m.upper() for m in ep.get("methods") or []]
            return {
                "status": ep.get("status", "unknown"),
                "methods": methods,
                "host": ep.get("host", ""),
                "mutating": any(m in ("POST", "PUT", "PATCH", "DELETE") for m in methods),
            }
    return {"status": "unknown", "methods": [], "host": "", "mutating": False}
