"""Self-protection: never let the dashboard power-cycle the guest (or node) it
runs on, and honor read-only mode. Enforced SERVER-SIDE — the router calls
`check_power` before issuing any bridge POST, so a crafted request from the
same-origin iframe cannot bypass it (hiding the UI control is only cosmetic).

Proxmox has no reliable in-guest self-detection, so the "self-guest" is an
operator declaration (state.settings). When set, stop/shutdown/reboot on that
guest — and on its host node — are refused; `start` is harmless and allowed.
"""

from __future__ import annotations

from . import state

# Actions that can take the dashboard down if aimed at its own guest/node.
# `delete` joins the power-down set once provisioning lands.
DESTRUCTIVE = {"stop", "shutdown", "reboot", "delete"}

# Which PVE capability class an action needs. A learned denial (state.caps_denied)
# gates only its own class, so a power-only token still provisions nothing while a
# read-only auditor token still can't power, without one blunt global switch.
CAP_OF = {
    "start": "power", "stop": "power", "shutdown": "power", "reboot": "power",
    "create": "allocate", "clone": "allocate", "delete": "allocate",
}


def is_self_guest(settings: dict, node: str, gtype: str, vmid: int) -> bool:
    return (
        settings.get("self_vmid") is not None
        and int(settings.get("self_vmid")) == int(vmid)
        and settings.get("self_type", "") == gtype
        and settings.get("self_node", "") == node
    )


def is_self_node(settings: dict, node: str) -> bool:
    return bool(settings.get("self_node")) and settings.get("self_node") == node


async def check_action(node: str, gtype: str, vmid: int | None, action: str) -> tuple[bool, str]:
    """(allowed, reason). reason is non-empty only on refusal (for the 403 + audit).

    Covers power AND provisioning actions. create/clone are not aimed at an
    existing guest (vmid may be None), so the self-guard skips them; only
    read-only + a learned `allocate` denial gate them.
    """
    settings = await state.get_settings()
    if settings.get("read_only"):
        return False, "read-only mode is enabled"
    cap = CAP_OF.get(action)
    if cap and cap in settings.get("caps_denied", []):
        return False, f"this Proxmox token lacks {cap} rights"
    if (
        action in DESTRUCTIVE
        and vmid is not None
        and is_self_guest(settings, node, gtype, int(vmid))
    ):
        verb = "delete" if action == "delete" else "power-cycle"
        return False, f"this is the dashboard's own guest; {verb} it from the Proxmox console"
    return True, ""


async def check_power(node: str, gtype: str, vmid: int, action: str) -> tuple[bool, str]:
    """Back-compat alias for the power path."""
    return await check_action(node, gtype, vmid, action)


async def annotate_self(settings: dict, nodes: list[dict], guests: list[dict]) -> None:
    """Mark the self-guest and self-node in-place so the UI can hide their
    destructive controls (defense-in-depth; the server check above is the gate)."""
    for g in guests:
        g["is_self"] = is_self_guest(settings, g.get("node", ""), g.get("type", ""), g.get("vmid") or -1)
    for n in nodes:
        n["is_self_host"] = is_self_node(settings, n.get("name", ""))
