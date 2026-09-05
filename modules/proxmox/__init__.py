"""Proxmox - an AgeniusDesk community module.

Connect a Proxmox VE cluster and get a read-first control surface: nodes, VMs,
and LXCs with live status; node + cluster health; and gated start/stop/reboot on
guests. The cluster API token never enters module code: every call goes through
the host-owned http.request bridge in every isolation mode. See router.py;
self-protection lives in guard.py.
"""

from .router import router  # noqa: F401
