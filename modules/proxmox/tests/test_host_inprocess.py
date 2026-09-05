"""The dual-mode _host facade.

In-process mode delegates to the HOST implementation
(`backend.modules._runtime.http_bridge`) and never touches a manifest, a secret
store, or the network itself. The isolated path is exercised host-side in the CE
repo's test_http_bridge.py; here a fake `backend` package stands in for the host
so the module's tests stay independent of it.
"""

import asyncio
import sys
import types

import pytest
from proxmox import _host


class _FakeBridgeError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


@pytest.fixture
def fake_host(monkeypatch):
    """Install a fake backend.modules._runtime.http_bridge and return its call log."""
    calls = {"request": [], "summary": []}
    mod = types.ModuleType("backend.modules._runtime.http_bridge")
    mod.HttpBridgeError = _FakeBridgeError

    async def request(module_id, payload, allowed_endpoints=None):
        calls["request"].append((module_id, payload, allowed_endpoints))
        if payload.get("path") == "/boom":
            raise _FakeBridgeError(403, "method POST is not granted on endpoint 'proxmox'")
        return {"status": 200, "headers": {"content-type": "application/json"}, "body": '{"data": []}',
                "truncated": False}

    def grant_summary(module_id, endpoint_ids=None):
        calls["summary"].append(module_id)
        return [{"id": "proxmox", "status": "active", "methods": ["GET", "HEAD"], "host": "https://pve:8006",
                 "verify_tls": False, "revision": 3, "pinned": True, "declared_methods": ["GET", "POST"]}]

    mod.request = request
    mod.grant_summary = grant_summary
    pkg_backend = types.ModuleType("backend")
    pkg_modules = types.ModuleType("backend.modules")
    pkg_runtime = types.ModuleType("backend.modules._runtime")
    pkg_runtime.http_bridge = mod
    for name, m in (("backend", pkg_backend), ("backend.modules", pkg_modules),
                    ("backend.modules._runtime", pkg_runtime), ("backend.modules._runtime.http_bridge", mod)):
        monkeypatch.setitem(sys.modules, name, m)
    monkeypatch.setattr(_host, "ISOLATED", False)
    return calls


def test_in_process_delegates_to_host_with_module_id(fake_host):
    r = asyncio.run(_host.http_request("proxmox", method="GET", path="/nodes", query={"type": "vm"}))
    assert r["status"] == 200
    module_id, payload, allowed = fake_host["request"][-1]
    assert module_id == "proxmox" and allowed is None
    assert payload == {"endpoint": "proxmox", "method": "GET", "path": "/nodes", "query": {"type": "vm"},
                       "headers": None, "body": None}


def test_in_process_policy_rejection_becomes_host_error(fake_host):
    with pytest.raises(_host.HostError, match="not granted"):
        asyncio.run(_host.http_request("proxmox", method="POST", path="/boom"))


def test_in_process_grant_summary(fake_host):
    g = asyncio.run(_host.http_grant("proxmox"))
    assert g == {"status": "active", "methods": ["GET", "HEAD"], "host": "https://pve:8006", "mutating": False}
    assert fake_host["summary"] == ["proxmox"]
    assert asyncio.run(_host.http_grant("other"))["status"] == "unknown"


def test_module_never_imports_secret_store_or_reads_manifest():
    """Acceptance criterion 9: no host secret store, no manifest parsing, no
    direct upstream client in module code."""
    import pathlib

    src = pathlib.Path(_host.__file__).read_text(encoding="utf-8")
    assert "backend.config" not in src
    assert "manifest.json" not in src
    assert "decrypt_value" not in src and "load_secrets" not in src
    assert "verify=" not in src  # no module-side TLS decision
