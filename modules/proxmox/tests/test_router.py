"""Router gating: delete confirm/precondition ordering and per-capability 403.

The route coroutines are called directly with a minimal fake Request (only
`.headers.get` is used) and a monkeypatched client, so no TestClient / host
`backend` import is needed. AGD_BRIDGE_URL (set by the test command) routes
router.py down its ISOLATED branch at import time."""

import asyncio
from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from proxmox import client, guard, state

# `from proxmox import router` (and `import proxmox.router as ...`) both resolve
# the APIRouter that __init__ re-exports, shadowing the module. Pull the module
# itself from sys.modules to reach its route coroutines.
prouter = import_module("proxmox.router")


def _req():
    return SimpleNamespace(headers={})


async def _reset():
    await state.save_settings(read_only=False, self_node="", self_vmid=None, self_type="", caps_denied=[])


def test_delete_confirm_mismatch_is_400_before_upstream(monkeypatch):
    async def go():
        await _reset()
        hit = {"del": False}

        async def fake_del(*a, **k):
            hit["del"] = True
            return {"ok": True, "status": 200, "data": "UPID"}

        monkeypatch.setattr(client, "delete_guest", fake_del)
        with pytest.raises(HTTPException) as ei:
            await prouter.delete_guest("pve1", "qemu", 100, prouter.DeleteConfirm(confirm_vmid=999), _req())
        assert ei.value.status_code == 400
        assert hit["del"] is False
    asyncio.run(go())


def test_delete_running_guest_refused_409(monkeypatch):
    async def go():
        await _reset()
        hit = {"del": False}

        async def fake_status(node, gtype, vmid):
            return {"status": "running", "error": ""}

        async def fake_del(*a, **k):
            hit["del"] = True
            return {"ok": True, "status": 200, "data": "UPID"}

        monkeypatch.setattr(client, "get_guest_status", fake_status)
        monkeypatch.setattr(client, "delete_guest", fake_del)
        with pytest.raises(HTTPException) as ei:
            await prouter.delete_guest("pve1", "qemu", 100, prouter.DeleteConfirm(confirm_vmid=100), _req())
        assert ei.value.status_code == 409
        assert hit["del"] is False
    asyncio.run(go())


def test_delete_stopped_guest_proceeds(monkeypatch):
    async def go():
        await _reset()

        async def fake_status(node, gtype, vmid):
            return {"status": "stopped", "error": ""}

        async def fake_del(node, gtype, vmid):
            return {"ok": True, "status": 200, "detail": "", "data": "UPID:del"}

        monkeypatch.setattr(client, "get_guest_status", fake_status)
        monkeypatch.setattr(client, "delete_guest", fake_del)
        out = await prouter.delete_guest("pve1", "lxc", 213, prouter.DeleteConfirm(confirm_vmid=213), _req())
        assert out["ok"] and out["upid"] == "UPID:del"
    asyncio.run(go())


def test_clone_403_denies_allocate_only(monkeypatch):
    async def go():
        await _reset()

        async def fake_clone(node, gtype, vmid, newid, name=None, target=None, full=False):
            return {"ok": False, "status": 403, "detail": "forbidden", "data": None}

        monkeypatch.setattr(client, "clone_guest", fake_clone)
        with pytest.raises(HTTPException) as ei:
            await prouter.clone_guest("pve1", "qemu", 100, prouter.CloneReq(newid=9001), _req())
        assert ei.value.status_code == 403
        s = await state.get_settings()
        assert "allocate" in s["caps_denied"] and "power" not in s["caps_denied"]
        # power is a different capability class and must remain usable.
        allowed, _ = await guard.check_action("pve1", "qemu", 100, "start")
        assert allowed
    asyncio.run(go())


def test_create_vm_success_returns_upid(monkeypatch):
    async def go():
        await _reset()

        async def fake_create(node, spec):
            assert spec["vmid"] == 9001
            return {"ok": True, "status": 200, "detail": "", "data": "UPID:create"}

        monkeypatch.setattr(client, "create_vm", fake_create)
        out = await prouter.create_vm("pve1", prouter.VMCreate(vmid=9001, storage="local-lvm", disk_gb=8), _req())
        assert out["ok"] and out["upid"] == "UPID:create"
    asyncio.run(go())
