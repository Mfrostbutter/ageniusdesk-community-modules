"""Self-protection guard: read-only mode and self-guest refusal, server-side."""

import asyncio

from proxmox import guard, state


def test_read_only_blocks_all_actions():
    async def go():
        await state.save_settings(read_only=True, self_node="", self_vmid=None, self_type="")
        for action in ("start", "stop", "shutdown", "reboot"):
            allowed, reason = await guard.check_power("pve1", "qemu", 100, action)
            assert not allowed
            assert "read-only" in reason
    asyncio.run(go())


def test_self_guest_blocks_destructive_allows_start():
    async def go():
        await state.save_settings(read_only=False, self_node="pve1", self_vmid=213, self_type="lxc")
        for action in ("stop", "shutdown", "reboot"):
            allowed, reason = await guard.check_power("pve1", "lxc", 213, action)
            assert not allowed
            assert "own guest" in reason
        # start on the self-guest is harmless
        allowed, _ = await guard.check_power("pve1", "lxc", 213, "start")
        assert allowed
    asyncio.run(go())


def test_other_guest_is_allowed():
    async def go():
        await state.save_settings(read_only=False, self_node="pve1", self_vmid=213, self_type="lxc")
        # different vmid, same node → not the self-guest
        allowed, _ = await guard.check_power("pve1", "qemu", 108, "stop")
        assert allowed
        # same vmid, DIFFERENT node → not the self-guest (identity is node+type+vmid)
        allowed, _ = await guard.check_power("pve2", "lxc", 213, "stop")
        assert allowed
    asyncio.run(go())


def test_annotate_self_marks_guest_and_node():
    async def go():
        settings = await state.save_settings(read_only=False, self_node="pve1", self_vmid=213, self_type="lxc")
        nodes = [{"name": "pve1"}, {"name": "pve2"}]
        guests = [{"node": "pve1", "type": "lxc", "vmid": 213}, {"node": "pve1", "type": "qemu", "vmid": 108}]
        await guard.annotate_self(settings, nodes, guests)
        assert guests[0]["is_self"] is True
        assert guests[1]["is_self"] is False
        assert nodes[0]["is_self_host"] is True
        assert nodes[1]["is_self_host"] is False
    asyncio.run(go())
