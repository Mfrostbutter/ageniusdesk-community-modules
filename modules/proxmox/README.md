# Proxmox — AgeniusDesk community module

Connect a Proxmox VE cluster and get a read-first control surface inside AgeniusDesk:
nodes, VMs, and LXCs with live status and health, plus **gated** start / shutdown /
force-stop / reboot on guests.

## What it does

- **Cluster roll-up** — one `/cluster/resources` call paints every node and guest
  (running / stopped, CPU, memory, uptime) with quorum status. Degraded-not-fatal:
  an unreachable cluster is shown, not a crash.
- **Gated power actions** — start / shutdown / force-stop / reboot, each behind a
  confirm and the server-side self-protection guard.
- **Self-protection** — AgeniusDesk often runs *on* the cluster it manages, and a
  Proxmox guest can't reliably detect its own vmid/node. So you declare the
  dashboard's own guest (node + vmid + type) in Settings; the module then **refuses**
  stop / shutdown / reboot on it (server-side) and hides its controls. Power-cycle
  that guest from the Proxmox console instead.
- **Read-only mode** — a toggle that disables the entire action set.
- **Audit log** — every power attempt, including refusals, is recorded.

## Credentials & isolation

The module holds **no credential in the worker**. It declares one `http.request`
endpoint (`proxmox`) in its manifest; the AgeniusDesk host injects your API token
host-side per call (the `http.request` bridge). Under container/subprocess isolation
the token never enters the module process.

**Setup:** create a Proxmox API token (Datacenter → Permissions → API Tokens) and
store the **full** string `user@realm!tokenid=secret` as the `PROXMOX_TOKEN` secret
(not just the UUID half — that is the common cause of 401s). Point the endpoint's
`base_url` at your console, e.g. `https://10.0.0.20:8006/api2/json`. Proxmox uses a
self-signed cert by default, so the endpoint ships with `verify_tls: false`.

Least-privilege: a `PVEAuditor` token gives a read-only install; add `VM.PowerMgmt`
for the action set. If the token lacks power rights, the first action's `403` flips
the install to read-only automatically.

## Auth note

v1 supports **API-token auth only**. Proxmox username/password (realm) login is a
stateful ticket + CSRF flow that does not fit the stateless single-header bridge; use
a token.

## Scope (v1)

Read + guest power-cycle. Create / clone / delete, storage, backups, and firewall are
out of scope (backups have their own AgeniusDesk roadmap item). Node power (reboot /
shutdown a whole node) is intentionally left to the console. One cluster per install.

## Fleet Health

The module serves `GET /api/proxmox/fleet-health` (`{rows:[…]}`) so that, once the
host's Fleet Health contribution API ships, the cluster roll-up appears in the shared
Fleet Health pane. Until then it renders in its own view.

## Development

```
# tests (run against the AgeniusDesk CE venv, which has httpx/respx/aiosqlite)
AGD_BRIDGE_URL=http://127.0.0.1:9/x AGD_MODULE_DATA_DIR=$(mktemp -d) \
  uv run --project ../ageniusdesk-ce --with pytest --with respx \
  python -m pytest modules/proxmox/tests -q
```

MIT licensed.
