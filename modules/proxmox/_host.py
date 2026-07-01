"""Host-services facade: dual-mode (isolated worker vs in_process).

The one place that knows whether we run sandboxed or in the host process:

  - ISOLATED (AGD_BRIDGE_URL set): every outbound Proxmox call goes through the
    host `http.request` bridge. The worker names the operator-consented endpoint
    id + a relative path; the HOST owns the base URL, the token, and the TLS
    policy, and makes the authenticated call. The token never enters the worker.
  - in_process (default install): there is no bridge, so this file replicates the
    bridge's request logic locally — it reads its own manifest for the endpoint
    config and resolves the token via the host secret store (backend.config).

Either way, callers use `http_request(...)` and get the same
`{status, headers, body, truncated}` shape. This facade is Proxmox-agnostic; any
future homelab module reuses it verbatim.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

ISOLATED = bool(os.environ.get("AGD_BRIDGE_URL"))
_BRIDGE_URL = os.environ.get("AGD_BRIDGE_URL", "").rstrip("/")
_BRIDGE_TOKEN = os.environ.get("AGD_BRIDGE_TOKEN", "")
_TIMEOUT = 30.0
_MAX_BYTES = 5_000_000

# Kept in sync with the host bridge (backend/modules/_runtime/bridge.py).
_FORBIDDEN_REQ_HEADERS = {
    "authorization", "host", "cookie", "content-length",
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
_RESP_HEADER_ALLOW = {
    "content-type", "content-length", "content-encoding",
    "etag", "last-modified", "retry-after",
}


class HostError(RuntimeError):
    """A host bridge / host-call failure with an operator-facing message."""


# ── isolated transport (bridge) ───────────────────────────────────────────────


async def _http_via_bridge(payload: dict) -> dict:
    headers = {"authorization": f"Bearer {_BRIDGE_TOKEN}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_BRIDGE_URL}/api/_host/http/request", json=payload, headers=headers)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = r.text[:200]
        raise HostError(f"http.request failed (HTTP {r.status_code}): {detail}")
    return r.json()


# ── in_process transport (replicate the bridge locally) ───────────────────────

_MANIFEST_CACHE: dict[str, Any] | None = None


def _endpoint_config(endpoint_id: str) -> dict:
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is None:
        _MANIFEST_CACHE = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
    endpoints = (
        _MANIFEST_CACHE.get("capabilities", {}).get("host", {}).get("http", {}).get("endpoints", [])
    )
    for ep in endpoints:
        if ep.get("id") == endpoint_id:
            return ep
    raise HostError(f"unknown or undeclared endpoint {endpoint_id!r}")


def _validate_rel_path(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    if "\x00" in p or "\\" in p or "://" in p or p.startswith("//"):
        raise HostError("invalid path")
    path_part = p.split("?", 1)[0].split("#", 1)[0]
    if "@" in path_part or any(seg == ".." for seg in path_part.split("/")):
        raise HostError("invalid path")
    return p


def _inject_auth(auth: dict, headers: dict, url: str) -> str:
    atype = (auth.get("type") or "").lower()
    if not atype:
        return url
    from backend.config import decrypt_value  # in_process only; imported lazily when auth is present

    value = decrypt_value(f"${auth.get('secret_ref', '')}") if auth.get("secret_ref") else ""
    if atype == "bearer":
        headers["Authorization"] = f"Bearer {value}"
    elif atype == "header":
        fmt = auth.get("format") or "{value}"
        headers[auth.get("header") or "Authorization"] = fmt.replace("{value}", value)
    elif atype == "basic":
        import base64
        user = decrypt_value(f"${auth['user_ref']}") if auth.get("user_ref") else auth.get("user", "")
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{value}".encode()).decode()
    elif atype == "query":
        import urllib.parse
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode({auth.get('param') or 'token': value})}"
    return url


async def _http_in_process(payload: dict) -> dict:
    import urllib.parse

    ep = _endpoint_config(payload["endpoint"])
    method = (payload.get("method") or "GET").upper()
    allowed = {m.upper() for m in (ep.get("methods") or ["GET", "HEAD"])}
    if method not in allowed:
        raise HostError(f"method {method} not permitted on endpoint {ep.get('id')!r}")

    base_url = (ep.get("base_url") or "").rstrip("/")
    rel = _validate_rel_path(payload.get("path", ""))
    url = base_url + ("/" + rel.lstrip("/") if rel else "")
    if payload.get("query"):
        qs = urllib.parse.urlencode({str(k): str(v) for k, v in payload["query"].items()})
        if qs:
            url += ("&" if "?" in url else "?") + qs

    headers = {
        str(k): str(v)
        for k, v in (payload.get("headers") or {}).items()
        if str(k).lower() not in _FORBIDDEN_REQ_HEADERS
    }
    url = _inject_auth(ep.get("auth") or {}, headers, url)

    body = payload.get("body")
    content = None
    if isinstance(body, str):
        content = body.encode("utf-8")
    elif body is not None:
        content = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    try:
        async with httpx.AsyncClient(
            verify=bool(ep.get("verify_tls", True)), timeout=_TIMEOUT, follow_redirects=False
        ) as c:
            resp = await c.request(method, url, headers=headers, content=content)
            raw = resp.content
    except httpx.HTTPError as e:
        raise HostError(f"upstream request failed: {type(e).__name__}")

    truncated = len(raw) > _MAX_BYTES
    raw = raw[:_MAX_BYTES]
    try:
        body_out: str = raw.decode("utf-8")
    except UnicodeDecodeError:
        import base64
        body_out = base64.b64encode(raw).decode("ascii")
    out_headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in _RESP_HEADER_ALLOW}
    return {"status": resp.status_code, "headers": out_headers, "body": body_out, "truncated": truncated}


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
    rejection (unknown endpoint, disallowed method, bad path).
    """
    payload = {"endpoint": endpoint, "method": method, "path": path,
               "query": query, "headers": headers, "body": body}
    if ISOLATED:
        return await _http_via_bridge(payload)
    return await _http_in_process(payload)
