"""Client provisioning paths: discovery, PVE param assembly, delete, task poll."""

import asyncio
import json
import urllib.parse

from proxmox import client


def _capture(monkeypatch, data=None, status=200):
    """Patch the host transport to record the outbound call and return `data`."""
    seen = {}

    async def fake(endpoint, method="GET", path="", query=None, headers=None, body=None):
        seen.update(endpoint=endpoint, method=method, path=path, query=query, headers=headers, body=body)
        return {"status": status, "headers": {}, "body": json.dumps({"data": data}), "truncated": False}

    monkeypatch.setattr(client._host, "http_request", fake)
    return seen


def _form(body):
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


def test_create_vm_assembles_pve_params(monkeypatch):
    seen = _capture(monkeypatch, data="UPID:pve1:0001:create::")
    spec = {"vmid": 9001, "name": "web", "cores": 2, "sockets": 1, "memory": 2048,
            "storage": "local-lvm", "disk_gb": 32, "iso": "local:iso/d.iso",
            "bridge": "vmbr0", "vlan": 10, "ostype": "l26"}
    r = asyncio.run(client.create_vm("pve1", spec))
    assert r["ok"] and r["data"].startswith("UPID")
    assert seen["method"] == "POST" and seen["path"] == "/nodes/pve1/qemu"
    f = _form(seen["body"])
    assert f["scsi0"] == "local-lvm:32"
    assert f["ide2"] == "local:iso/d.iso,media=cdrom"
    assert f["net0"] == "virtio,bridge=vmbr0,tag=10"
    assert f["boot"] == "order=scsi0;ide2"
    assert f["scsihw"] == "virtio-scsi-single"


def test_create_vm_without_iso_omits_cdrom(monkeypatch):
    seen = _capture(monkeypatch, data="UPID")
    spec = {"vmid": 9001, "storage": "local-lvm", "disk_gb": 8}
    asyncio.run(client.create_vm("pve1", spec))
    f = _form(seen["body"])
    assert "ide2" not in f
    assert f["boot"] == "order=scsi0"


def test_create_lxc_assembles_pve_params(monkeypatch):
    seen = _capture(monkeypatch, data="UPID")
    spec = {"vmid": 9002, "hostname": "ct", "ostemplate": "local:vztmpl/deb.tar.zst",
            "storage": "local-lvm", "disk_gb": 8, "cores": 1, "memory": 512, "swap": 256,
            "bridge": "vmbr0", "ip": "dhcp", "ssh_public_keys": "ssh-ed25519 AAA", "unprivileged": True}
    asyncio.run(client.create_lxc("pve1", spec))
    assert seen["path"] == "/nodes/pve1/lxc"
    f = _form(seen["body"])
    assert f["rootfs"] == "local-lvm:8"
    assert f["net0"] == "name=eth0,bridge=vmbr0,ip=dhcp"
    assert f["unprivileged"] == "1"
    assert f["ssh-public-keys"] == "ssh-ed25519 AAA"
    assert f["ostemplate"] == "local:vztmpl/deb.tar.zst"


def test_clone_qemu_uses_name_and_full(monkeypatch):
    seen = _capture(monkeypatch, data="UPID")
    asyncio.run(client.clone_guest("pve1", "qemu", 100, 9003, name="copy", target="pve2", full=True))
    assert seen["path"] == "/nodes/pve1/qemu/100/clone"
    f = _form(seen["body"])
    assert f["newid"] == "9003" and f["full"] == "1" and f["name"] == "copy" and f["target"] == "pve2"


def test_clone_lxc_uses_hostname(monkeypatch):
    seen = _capture(monkeypatch, data="UPID")
    asyncio.run(client.clone_guest("pve1", "lxc", 213, 9004, name="ct2"))
    f = _form(seen["body"])
    assert f["hostname"] == "ct2" and "name" not in f and "full" not in f


def test_delete_qemu_purges_and_destroys_disks(monkeypatch):
    seen = _capture(monkeypatch, data="UPID")
    r = asyncio.run(client.delete_guest("pve1", "qemu", 100))
    assert r["ok"] and seen["method"] == "DELETE"
    assert seen["path"] == "/nodes/pve1/qemu/100"
    assert seen["query"] == {"purge": 1, "destroy-unreferenced-disks": 1}


def test_delete_lxc_purges_only(monkeypatch):
    seen = _capture(monkeypatch, data="UPID")
    asyncio.run(client.delete_guest("pve1", "lxc", 213))
    assert seen["query"] == {"purge": 1}


def test_task_status_parses_and_encodes_upid(monkeypatch):
    upid = "UPID:pve1:0001:00A:0064:qmcreate:9001:root@pam:"
    seen = _capture(monkeypatch, data={"status": "stopped", "exitstatus": "OK"})
    s = asyncio.run(client.task_status("pve1", upid))
    assert s["done"] is True and s["ok"] is True
    # '@' and ':' are percent-encoded so the host path validator accepts them.
    assert "@" not in seen["path"] and "%40" in seen["path"]


def test_task_status_reports_failure(monkeypatch):
    _capture(monkeypatch, data={"status": "stopped", "exitstatus": "clone failed"})
    s = asyncio.run(client.task_status("pve1", "UPID:x:root@pam:"))
    assert s["done"] is True and s["ok"] is False


def test_get_nextid(monkeypatch):
    _capture(monkeypatch, data="9005")
    assert asyncio.run(client.get_nextid()) == 9005


def test_provision_options_parses_and_is_degraded_not_fatal(monkeypatch):
    async def fake(endpoint, method="GET", path="", query=None, headers=None, body=None):
        if path == "/nodes/pve1/storage":
            data = [
                {"storage": "local", "content": "iso,vztmpl", "avail": 5 << 30, "total": 10 << 30, "active": 1},
                {"storage": "local-lvm", "content": "images,rootdir",
                 "avail": 50 << 30, "total": 100 << 30, "active": 1},
            ]
        elif path == "/nodes/pve1/storage/local/content" and query == {"content": "iso"}:
            data = [{"volid": "local:iso/debian.iso", "size": 700 << 20}]
        elif path == "/nodes/pve1/storage/local/content" and query == {"content": "vztmpl"}:
            data = [{"volid": "local:vztmpl/deb.tar.zst", "size": 100 << 20}]
        elif path == "/nodes/pve1/network":
            raise client._host.HostError("net boom")  # degraded, not fatal
        else:
            data = []
        return {"status": 200, "headers": {}, "body": json.dumps({"data": data}), "truncated": False}

    monkeypatch.setattr(client._host, "http_request", fake)
    o = asyncio.run(client.get_provision_options("pve1"))
    assert {s["name"] for s in o["storages"]} == {"local", "local-lvm"}
    assert o["isos"][0]["volid"] == "local:iso/debian.iso"
    assert o["templates"][0]["volid"] == "local:vztmpl/deb.tar.zst"
    assert o["bridges"] == []
    assert any("bridges" in w for w in o["warnings"])
