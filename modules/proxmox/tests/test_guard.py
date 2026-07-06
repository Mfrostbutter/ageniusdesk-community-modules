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


def test_delete_on_self_guest_refused():
    async def go():
        await state.save_settings(read_only=False, self_node="pve1", self_vmid=213, self_type="lxc", caps_denied=[])
        allowed, reason = await guard.check_action("pve1", "lxc", 213, "delete")
        assert not allowed and "own guest" in reason
    asyncio.run(go())


def test_create_and_clone_bypass_self_guard():
    async def go():
        # create/clone are not aimed at an existing guest; self-guard must not fire.
        await state.save_settings(read_only=False, self_node="pve1", self_vmid=213, self_type="lxc", caps_denied=[])
        for action in ("create", "clone"):
            allowed, _ = await guard.check_action("pve1", "lxc", None, action)
            assert allowed
    asyncio.run(go())


def test_capability_denial_is_per_class():
    async def go():
        # A learned 'allocate' denial blocks create/clone/delete but NOT power.
        await state.save_settings(read_only=False, self_node="", self_vmid=None, self_type="", caps_denied=["allocate"])
        allowed, reason = await guard.check_action("pve1", "qemu", 9001, "create")
        assert not allowed and "allocate" in reason
        allowed, _ = await guard.check_action("pve1", "qemu", 100, "start")
        assert allowed
    asyncio.run(go())


def test_add_denied_cap_is_idempotent_and_bounded():
    async def go():
        await state.save_settings(caps_denied=[])
        assert await state.add_denied_cap("power") == ["power"]
        assert await state.add_denied_cap("power") == ["power"]           # idempotent
        assert set(await state.add_denied_cap("allocate")) == {"allocate", "power"}
        assert await state.add_denied_cap("bogus") == ["allocate", "power"]  # unknown ignored
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
