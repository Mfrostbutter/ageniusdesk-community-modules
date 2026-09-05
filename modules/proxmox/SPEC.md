# Proxmox module — v2 spec (provisioning + cluster stats)

Status: shipped. Extends the shipped v1 (read-first + guest power actions). This
doc is the v2 build contract; the README stays operator-facing. The proposed v3
inventory, health, capacity, and export work is specified in
[SPEC-v3.md](SPEC-v3.md).

Note (2026-09-05): the `http.request` host bridge shipped in CE 0.6.0. The in-process compatibility path described below is retired; see SPEC-v3 Phase 0.

## Goal

Two additions to the shipped module:

1. **Cluster resource stats** — surface aggregate cores, memory, disk, and a
   weighted utilization roll-up across the cluster. Pure read.
2. **Provisioning** — from-scratch create (VM + LXC), clone, and delete of
   guests, each gated behind the existing self-protection guard, read-only
   switch, and a per-capability permission model.

Everything mutating stays server-side-gated. The frontend never holds a
credential; all calls still go through the one host-injected `proxmox`
`http.request` endpoint.

## 1. Cluster stats (read)

`/nodes` already returns per-node `maxcpu`, `cpu`, `mem`, `maxmem`, `maxdisk`,
`disk`. v1 drops all but `cpu`/`mem`. v2 keeps them and computes cluster totals.

Node roll-up gains: `cores` (maxcpu), `disk` ({used,total,pct}).

`cluster.totals` gains (online nodes only, so a downed node doesn't skew util):

```jsonc
"totals": {
  "nodes": 4, "nodes_online": 4, "guests": 37, "running": 30, "stopped": 7,
  "cores": 96,                                  // sum(maxcpu)
  "cpu_pct": 18,                                // round(100 * sum(cpu*maxcpu)/sum(maxcpu))
  "mem":  {"used": ..., "total": ..., "pct": 61},   // sum over online nodes
  "disk": {"used": ..., "total": ..., "pct": 44}
}
```

No manifest change. Renders as a stat row above the node cards.

## 2. Discovery reads (feed the create wizard)

From-scratch create needs the operator to pick real resources, so the wizard is
populated by node-scoped discovery calls, not free text.

| Route | PVE source | Returns |
|---|---|---|
| `GET /api/proxmox/nextid` | `/cluster/nextid` | `{vmid}` — next free id |
| `GET /api/proxmox/nodes/{node}/provision-options` | several (below) | `{storages, isos, templates, bridges}` |

`provision-options` fans out (server-side) to:

- `/nodes/{node}/storage` → `storages`: `[{name, content:[...], avail, total,
  active}]`. `content` (a set) decides where each storage can be used:
  `images` = VM disk, `rootdir` = LXC rootfs, `iso` = install media, `vztmpl` =
  LXC template.
- `/nodes/{node}/storage/{s}/content?content=iso` for each iso-capable storage
  → `isos`: `[{volid, size, storage}]`.
- `/nodes/{node}/storage/{s}/content?content=vztmpl` for each vztmpl-capable
  storage → `templates`: `[{volid, size, storage}]`.
- `/nodes/{node}/network?type=any_bridge` → `bridges`: `[{iface, active}]`.

Degraded-not-fatal: any sub-call that fails contributes an empty list plus a
`warnings:[...]` entry; the wizard still opens with whatever populated.

## 3. Mutations

Namespaced under `/provision` (create/clone) to avoid colliding with the power
route `POST /guests/{node}/{gtype}/{vmid}/{action}`. Delete reuses `/guests`
with the `DELETE` verb.

### 3a. Create VM — `POST /api/proxmox/provision/{node}/qemu`

PVE `POST /nodes/{node}/qemu`. Request (validated pydantic model):

```jsonc
{
  "vmid": 9001, "name": "web-03",
  "cores": 2, "sockets": 1, "memory": 2048,      // MiB
  "storage": "local-lvm", "disk_gb": 32,
  "iso": "local:iso/debian-12.iso",              // optional; cdrom boot
  "bridge": "vmbr0", "vlan": null,
  "ostype": "l26", "start": false
}
```

Body built server-side: `scsihw=virtio-scsi-single`, `scsi0={storage}:{disk_gb}`,
`ide2={iso},media=cdrom` (only if iso), `net0=virtio,bridge={bridge}[,tag={vlan}]`,
`boot=order=scsi0;ide2`, `onboot=0`. Returns `{upid, node}` (async task).

### 3b. Create LXC — `POST /api/proxmox/provision/{node}/lxc`

PVE `POST /nodes/{node}/lxc`. Request:

```jsonc
{
  "vmid": 9002, "hostname": "ct-tools",
  "ostemplate": "local:vztmpl/debian-12-standard_amd64.tar.zst",
  "storage": "local-lvm", "disk_gb": 8,
  "cores": 1, "memory": 512, "swap": 512,
  "bridge": "vmbr0", "vlan": null, "ip": "dhcp",  // "dhcp" | CIDR
  "password": null, "ssh_public_keys": null,      // one required by PVE unless template has creds
  "unprivileged": true, "start": false
}
```

Body: `rootfs={storage}:{disk_gb}`, `net0=name=eth0,bridge={bridge},ip={ip}[,tag=]`,
`unprivileged=1`, `features=nesting=1` left OFF by default. Returns `{upid, node}`.

Password handling: never logged, never audited. `ssh_public_keys` preferred.

### 3c. Clone — `POST /api/proxmox/provision/{node}/{gtype}/{vmid}/clone`

PVE `POST /nodes/{node}/{gtype}/{vmid}/clone`. Request:
`{newid, name, target?, full}`. `newid` defaults to `/cluster/nextid` if omitted
(resolved server-side). `full=true` → full clone (linked otherwise; qemu only —
lxc is always full). Returns `{upid, node}`.

### 3d. Delete — `DELETE /api/proxmox/guests/{node}/{gtype}/{vmid}`

PVE `DELETE /nodes/{node}/{gtype}/{vmid}?purge=1&destroy-unreferenced-disks=1`.
Preconditions enforced server-side, in order:

1. read-only master switch off,
2. not the self-guest (guard),
3. guest is **stopped** (re-read status; refuse a running guest),
4. request body carries `{"confirm_vmid": <vmid>}` matching the path vmid
   (type-to-confirm; a missing/mismatched value is a 400 before any upstream
   call).

Returns `{upid, node}`. Every refusal and attempt is audited.

## 4. Async tasks

Create/clone/delete return a UPID. The UI does not block. A task-status route
lets the UI poll to completion and show success/failure instead of guessing from
the next cluster poll.

`GET /api/proxmox/tasks/{node}/{upid}` → PVE `/nodes/{node}/tasks/{upid}/status`
→ `{status: "running"|"stopped", exitstatus, done: bool, ok: bool}`. `ok` is
`exitstatus == "OK"`. UPID is opaque; it is URL-encoded in the path.

On task completion the client cache is invalidated so the next cluster poll
reflects the new/removed guest.

## 5. Permission model (per-capability, replaces the global 403 flip)

v1 flipped the **entire** install to read-only on the first power 403. With three
new mutating verbs that need different PVE privileges, that is too blunt: a token
with `VM.PowerMgmt` but not `VM.Allocate` would lock the whole UI on the first
clone attempt.

v2 maps each mutation to a capability class and remembers per-class denials:

| Capability | Actions | PVE privilege |
|---|---|---|
| `power` | start/stop/shutdown/reboot | `VM.PowerMgmt` |
| `allocate` | create/clone/delete | `VM.Allocate` (+ `Datastore.AllocateSpace` for create/clone) |

State gains `caps_denied` (a persisted set). A 403 on an action adds only that
action's capability to `caps_denied`; the guard then refuses that class and the
UI hides those controls, leaving the other class working. `read_only` stays as
the operator master switch (disables everything, unchanged meaning). An operator
can clear a learned denial by clearing read-only state via settings (a
`clear_denied` flag on the settings POST).

## 6. Guard (server-side, unchanged philosophy)

`check_action(node, gtype, vmid, action)` generalizes `check_power`:

1. `read_only` → refuse all.
2. capability of `action` in `caps_denied` → refuse (token lacks the right).
3. `action` is destructive-to-self (stop/shutdown/reboot/**delete**) AND target
   is the self-guest → refuse.
4. else allow.

`create`/`clone` are not aimed at an existing guest, so the self-guard doesn't
apply; only read-only + `allocate` denial gate them.

## 7. Audit

Existing `audit` table is reused. `action` widens to include
`create`/`clone`/`delete`. `vmid` is the *new* id for create/clone, the target
for delete. No secret material (LXC password, ssh keys) is ever written.

## 8. Manifest / capabilities delta

- Endpoint `methods` gains `"DELETE"` (create/clone are POST; discovery is GET).
- No new hosts, env, subprocess, or filesystem writes. `network.hosts` stays as
  the single declared endpoint. Scanner stays clean.
- `min_app_version` unchanged unless the host bridge needs a newer DELETE-capable
  contract; verify against the running host before release.

## 9. Out of scope (still)

Node power, storage/backup/firewall management, VM disk resize/move,
snapshot management, migration between nodes, template creation, multi-cluster.
Snapshots and migrate are the most likely v3 candidates.

## 10. Test plan

- client: cluster totals math (cores/util weighting, online-only); provision-body
  assembly for VM and LXC (exact PVE param strings); clone newid resolution;
  delete precondition ordering; task-status parsing.
- guard: per-capability denial; delete-on-self refused; create/clone bypass
  self-guard; read-only blocks all.
- router: delete confirm_vmid mismatch → 400 before upstream; 403 on clone marks
  only `allocate` denied, power still allowed.
- host: DELETE method permitted by the in-process replicant once declared.
