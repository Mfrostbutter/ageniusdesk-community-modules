"""Client read path: PVE roll-up parsing + degraded-not-fatal."""

import asyncio
import json

from proxmox import client


def _resp(data):
    return {"status": 200, "headers": {}, "body": json.dumps({"data": data}), "truncated": False}


def test_get_cluster_parses_rollup(monkeypatch):
    async def fake(endpoint, method="GET", path="", query=None, headers=None, body=None):
        table = {
            "/cluster/resources": [
                {"type": "qemu", "vmid": 100, "name": "web", "node": "pve1",
                 "status": "running", "cpu": 0.12, "mem": 1 << 30, "maxmem": 2 << 30, "uptime": 3600},
                {"type": "lxc", "vmid": 213, "name": "agd", "node": "pve1",
                 "status": "stopped", "cpu": 0, "mem": 0, "maxmem": 1 << 30, "uptime": 0},
                {"type": "storage", "storage": "local"},  # non-guest, ignored
            ],
            "/nodes": [
                {"node": "pve1", "status": "online", "cpu": 0.2, "mem": 4 << 30, "maxmem": 8 << 30, "uptime": 9000},
            ],
            "/cluster/status": [{"type": "cluster", "quorate": 1}],
        }
        return _resp(table[path])

    monkeypatch.setattr(client._host, "http_request", fake)
    c = asyncio.run(client.get_cluster())
    assert c["reachable"] is True
    assert c["quorate"] is True
    assert c["totals"] == {"nodes": 1, "nodes_online": 1, "guests": 2, "running": 1, "stopped": 1}
    web = next(g for g in c["guests"] if g["vmid"] == 100)
    assert web["cpu_pct"] == 12
    assert web["mem"]["pct"] == 50


def test_get_cluster_degraded_not_fatal(monkeypatch):
    async def boom(*a, **k):
        raise client._host.HostError("bridge down")

    monkeypatch.setattr(client._host, "http_request", boom)
    c = asyncio.run(client.get_cluster())
    assert c["reachable"] is False
    assert "bridge down" in c["error"]
    assert c["totals"]["guests"] == 0


def test_guest_power_reports_status(monkeypatch):
    async def fake(endpoint, method="GET", path="", query=None, headers=None, body=None):
        assert method == "POST"
        assert path == "/nodes/pve1/qemu/100/status/reboot"
        return {"status": 200, "headers": {}, "body": "{}", "truncated": False}

    monkeypatch.setattr(client._host, "http_request", fake)
    r = asyncio.run(client.guest_power("pve1", "qemu", 100, "reboot"))
    assert r["ok"] is True and r["status"] == 200


def test_guest_power_rejects_bad_action(monkeypatch):
    r = asyncio.run(client.guest_power("pve1", "qemu", 100, "destroy"))
    assert r["ok"] is False and r["status"] == 400
