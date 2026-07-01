"""The dual-mode _host SDK: in_process request building, path validation, method
gate. (The isolated path is exercised host-side in the CE repo's test_http_bridge.)"""

import asyncio

import httpx
import pytest
import respx
from proxmox import _host

_MANIFEST = {
    "capabilities": {"host": {"http": {"endpoints": [
        {"id": "t", "base_url": "https://10.0.0.9:8006/api2/json", "methods": ["GET", "POST"], "verify_tls": False},
        {"id": "ro", "base_url": "https://10.0.0.9:8006/api2/json", "methods": ["GET"], "verify_tls": False},
    ]}}}
}


@pytest.mark.parametrize("bad", ["../x", "//evil", "http://evil/x", "a\\b", "x@y", "a/../b"])
def test_validate_rel_path_rejects(bad):
    with pytest.raises(_host.HostError):
        _host._validate_rel_path(bad)


def test_validate_rel_path_accepts():
    assert _host._validate_rel_path("/nodes/pve1/qemu/100/status/start") == "/nodes/pve1/qemu/100/status/start"
    assert _host._validate_rel_path("") == ""


@respx.mock
def test_in_process_get(monkeypatch):
    monkeypatch.setattr(_host, "_MANIFEST_CACHE", _MANIFEST)
    route = respx.route(url__startswith="https://10.0.0.9:8006").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    r = asyncio.run(_host._http_in_process({"endpoint": "t", "method": "GET", "path": "/nodes"}))
    assert r["status"] == 200
    assert str(route.calls.last.request.url).endswith("/api2/json/nodes")


def test_in_process_method_gate(monkeypatch):
    monkeypatch.setattr(_host, "_MANIFEST_CACHE", _MANIFEST)
    with pytest.raises(_host.HostError, match="not permitted"):
        asyncio.run(_host._http_in_process({"endpoint": "ro", "method": "POST", "path": "/x"}))


def test_in_process_unknown_endpoint(monkeypatch):
    monkeypatch.setattr(_host, "_MANIFEST_CACHE", _MANIFEST)
    with pytest.raises(_host.HostError, match="unknown"):
        asyncio.run(_host._http_in_process({"endpoint": "nope", "method": "GET", "path": "/x"}))


@respx.mock
def test_in_process_response_header_allowlist(monkeypatch):
    monkeypatch.setattr(_host, "_MANIFEST_CACHE", _MANIFEST)
    respx.route(url__startswith="https://10.0.0.9:8006").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/json", "set-cookie": "s=1"}, json={})
    )
    r = asyncio.run(_host._http_in_process({"endpoint": "t", "method": "GET", "path": "/nodes"}))
    assert r["headers"].get("content-type") == "application/json"
    assert "set-cookie" not in r["headers"]
