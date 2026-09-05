# Proxmox community module v3: inventory, health, and capacity

Status: proposed

Target: AgeniusDesk CE plus `ageniusdesk-community-modules/modules/proxmox`

Date: 2026-09-05

Depends on: the CE `http.request` host bridge and trusted worker identity described
in the companion CE host note.

## 1. Decision

Version 3 turns the Proxmox module into an inventory-first operations surface. It
keeps the shipped cluster overview, guarded guest power actions, and provisioning,
then adds a point-in-time deep inventory, health findings, capacity calculations,
guest detail views, and a dependency-free Excel export.

The module will borrow the useful product ideas from PVEViewer, not copy its UI or
implementation. In particular:

- live, filterable infrastructure inventory;
- a structured health pass;
- Proxmox RRD-backed average and peak utilization;
- an Excel workbook that is useful outside AgeniusDesk;
- no agent installed on Proxmox nodes;
- partial results when an optional endpoint or guest agent is unavailable.

AgeniusDesk will remain different in four intentional ways:

- QEMU and LXC are both first-class;
- existing power and provisioning controls remain available behind server-side
  guards;
- v3 supports one Proxmox cluster per module install because CE currently grants
  one fixed `proxmox` endpoint;
- SSH host-configuration backup, username/password login, and unattended CLI
  operation are not part of this module.

## 2. Evidence and current baseline

### 2.1 Localhost v1

The running `localhost:3000` build was inspected through its served JavaScript and
read-only API on 2026-09-05. It currently provides:

- node cards with online state, PVE version, CPU, memory, root disk, and uptime;
- separate searchable/filterable tables for LXCs and QEMU VMs;
- VMID, name, node, state, live CPU, memory, and disk columns;
- start, stop, restart, and shutdown actions with confirmation for destructive
  actions;
- a per-node storage table with type, usage, enabled state, and content classes;
- four online nodes in the inspected environment, with 23 LXCs and 2 QEMU VMs.

This is the UX baseline to preserve. The v3 overview should feel like an evolution
of this surface, not a second unrelated Proxmox application.

### 2.2 Shipped community module

The community module already adds capabilities beyond localhost v1:

- a cluster-wide `/cluster/resources` roll-up;
- quorum and aggregate CPU/memory/disk statistics;
- a 10-second read cache and degraded-not-fatal behavior;
- a declared self-guest guard;
- operator read-only mode and learned per-capability denials;
- audited power actions;
- VM/LXC create, clone, stopped-only delete, and task polling.

The existing routes remain compatible in v3. Deep inventory is a separate, slower
collection path and must not be attached to the 5-second UI refresh loop.

### 2.3 PVEViewer reference

The product reference advertises full QEMU inventory, RRD averages and peaks,
guest-agent filesystem/network/OS information, snapshots, storage and host data,
health rules, multi-cluster views, a 13-sheet Excel export, host configuration
backup over SSH, and scheduled CLI exports.

The parity decision is:

| Reference capability | v3 decision | Notes |
|---|---|---|
| Live filterable grids | Build | Server pagination plus client sorting/filtering |
| Full QEMU inventory | Build | Add equivalent LXC coverage where PVE exposes data |
| Guest-agent details | Build, optional | Never invoke arbitrary guest commands |
| RRD average/peak | Build | 1 hour, 24 hour, and 7 day windows |
| Health analysis | Build | Stable rule ids and evidence on every finding |
| Excel export | Build | 13 sheets, generated with Python standard library |
| Multi-cluster | Defer to v3.1 | Requires repeatable host endpoint grants in CE |
| Scheduled/headless export | Defer to v3.1 | Requires a community-module scheduler contract |
| Password/TOTP login | Do not build | API tokens only |
| SSH host config backup | Do not build here | Separate high-risk module if ever approved |
| Inventory history database | Do not build | Latest successful point-in-time snapshot only |

## 3. Goals

1. Answer what exists, where it runs, what it consumes, and what looks unhealthy
   without opening the native PVE console.
2. Make a deep inventory usable in the browser and as a handoff-ready workbook.
3. Add richer reads without making the live overview slow or fragile.
4. Keep missing permissions, unavailable guest agents, and failed nodes explicit
   and non-fatal.
5. Preserve the module's credential-isolation and self-protection model.
6. Keep installation self-contained and dependency-free beyond the CE runtime.

## 4. Non-goals

- Replacing the Proxmox VE administrative console.
- Guest console access, terminal access, or arbitrary QEMU guest-agent execution.
- Creating, deleting, or rolling back snapshots in v3.
- Node power, firewall, SDN, HA, replication, Ceph, or PBS administration.
- Host filesystem reads, SSH, host-key collection, or `/etc/pve` backup.
- Long-term metrics/history storage or alert delivery.
- VMware discovery or migration workflows.
- More provisioning verbs than the module already ships.

## 5. Users and primary jobs

### Homelab operator

- See cluster health and resource pressure at a glance.
- Find a guest by name, VMID, node, tag, IP, or MAC address.
- Spot full storage, stale snapshots, and broken guest agents.
- Keep guarded power/provisioning close to the inventory.

### MSP or internal IT operator

- Export a consistent point-in-time inventory for review or handoff.
- Show assigned versus physical CPU/memory capacity.
- Identify incomplete data rather than silently treating it as zero.
- Re-run collection after fixing permissions and compare the health result outside
  the product through exported workbooks.

### Read-only reviewer

- Browse and export data without receiving any upstream mutation grant.
- Understand when data was collected, from which PVE version, and which probes
  failed.

## 6. Product information architecture

The module view gets five primary tabs. Do not reproduce all workbook sheets as UI
tabs; the browser needs an operational hierarchy, while Excel needs flat tables.

### 6.1 Overview

- Existing aggregate stat row: nodes, guests, running/stopped, cores, CPU, memory,
  and root-disk utilization.
- Node cards evolved from localhost v1, including version, uptime, maintenance or
  unknown state, and the number of findings on that node.
- Quorum banner when quorum is false or cannot be determined.
- Storage-pressure summary and top three active findings.
- Last live refresh and last deep collection timestamps shown separately.
- Existing create action and settings controls remain here.

### 6.2 Guests

One combined VM/LXC table with a type filter instead of permanently separate
tables. Required controls:

- search by name, VMID, node, tag, IP, or MAC;
- filters for cluster, node, type, status, template, guest-agent state, and health;
- sortable columns and 50-row pagination;
- column chooser saved in module settings, not browser storage;
- clear distinction between `0`, `unknown`, `not collected`, `not applicable`, and
  `permission denied`;
- row click opens a guest detail drawer;
- actions live in an explicit Actions menu and reuse the existing server-side
  guard. No action is triggered by row click.

Default columns: health, VMID, name, type, node, status, tags, vCPU, assigned
memory, live memory, primary IP, guest-agent state, snapshot count, and last
collected.

### 6.3 Capacity

- Physical cores versus assigned vCPU and allocation ratio.
- Physical memory versus assigned guest memory, live used memory, and allocation
  ratio.
- Storage physical used/free and provisioned guest disk bytes.
- Average and peak CPU/memory from PVE RRD for 1 hour, 24 hours, and 7 days.
- Top consumers by current, average, and peak utilization.
- Shared storage is counted once cluster-wide; local storage is counted per node.
- Explanatory tooltips state the formula and data coverage for each metric.

### 6.4 Health

- Finding counts by severity and rule family.
- Filterable findings table: severity, rule, resource, evidence, recommendation,
  first observed in current run, and collection timestamp.
- Coverage panel listing probes that succeeded, were denied, timed out, or were not
  applicable.
- No auto-remediation in v3.

### 6.5 Export

- Show the latest successful inventory timestamp and its coverage.
- Download Excel, JSON, or CSV ZIP.
- Collection and export are separate buttons. Export never silently starts a new
  collection.
- If no successful snapshot exists, disable export and explain how to collect one.

## 7. Collection model

### 7.1 Two read paths

`live summary` is the current `GET /api/proxmox/cluster` path. It stays cheap,
cached for 10 seconds, and is safe for the UI's 5-second poll.

`deep inventory` is an explicit point-in-time job. It fans out to node, guest,
storage, RRD, snapshot, and optional guest-agent endpoints. It writes an atomic
latest snapshot and is never run by the live poller.

### 7.2 Job lifecycle

States are `queued`, `running`, `complete`, `partial`, `failed`, and `cancelled`.
Only one collection may run per cluster. A second request returns `409` with the
active job id. Jobs survive view navigation but not a module-worker restart.

Progress stages:

1. `cluster` - version, cluster status, resources, and nodes;
2. `hosts` - node status, storage, certificates, and optional updates;
3. `guests` - current status and config for QEMU and LXC;
4. `details` - snapshots, RRD, and QEMU guest-agent reads;
5. `analyze` - capacity calculations and health rules;
6. `commit` - atomically replace the latest successful snapshot.

The job response reports stage, completed units, total units once known, warning
count, error count, start time, and elapsed milliseconds.

### 7.3 Concurrency and load limits

- Maximum eight upstream requests in flight for the one cluster.
- Maximum four guest-detail requests in flight per node.
- Connect timeout 5 seconds, read timeout 20 seconds, whole collection default 5
  minutes.
- Retry a GET once only for connect reset, timeout, or `429/502/503/504`, using
  bounded jitter. Never retry a `401`, `403`, or other `4xx`.
- Honor `Retry-After` when present and within the collection deadline.
- Maximum 5 MB per bridge response remains in force. A truncated response is a
  failed probe, never valid partial JSON.
- Maximum 10,000 guests, 100,000 inventory child rows, and 250,000 RRD points per
  collection. Crossing a limit marks the run partial and records the cap.

### 7.4 Atomicity and retention

The browser and exporter only read a completed snapshot. Collection builds a new
run, then updates one `latest_run_id` pointer in a transaction. A failed run never
replaces a good snapshot.

Retain:

- the latest successful or partial snapshot;
- the active run;
- the last 20 job summaries and their warnings/errors;
- no older inventory rows or RRD series.

This is operational metadata, not a history product.

## 8. Proxmox read plan

The implementation must verify the exact route and privilege against the supported
PVE 8.x and 9.x API viewers before coding. The intended sources are:

| Data | Intended PVE REST source | Required |
|---|---|---|
| PVE version | `GET /version` | Yes |
| Cluster/quorum | `GET /cluster/status` | Yes for clustered installs |
| All resources | `GET /cluster/resources` | Yes |
| Nodes | `GET /nodes` | Yes |
| Node detail | `GET /nodes/{node}/status` | Yes |
| Node storage | `GET /nodes/{node}/storage` | Yes |
| Cluster storage config | `GET /storage` | Best effort |
| Node certificates | `GET /nodes/{node}/certificates/info` | Best effort |
| Pending updates | `GET /nodes/{node}/apt/update` | Best effort, off by default |
| QEMU config/status | `GET /nodes/{node}/qemu/{vmid}/config` and `/status/current` | Yes |
| LXC config/status | `GET /nodes/{node}/lxc/{vmid}/config` and `/status/current` | Yes |
| QEMU/LXC RRD | `GET /nodes/{node}/{type}/{vmid}/rrddata` | Best effort |
| QEMU/LXC snapshots | `GET /nodes/{node}/{type}/{vmid}/snapshot` | Best effort |
| QEMU agent info | `GET /nodes/{node}/qemu/{vmid}/agent/info` | Best effort |
| QEMU OS info | `GET /nodes/{node}/qemu/{vmid}/agent/get-osinfo` | Best effort |
| QEMU filesystems | `GET /nodes/{node}/qemu/{vmid}/agent/get-fsinfo` | Best effort |
| QEMU interfaces | `GET /nodes/{node}/qemu/{vmid}/agent/network-get-interfaces` | Best effort |

No generic `/agent/{command}` proxy is exposed. The module has a fixed allowlist of
the four read-only agent calls above. LXC data uses PVE's own status/config fields;
QEMU-agent-only fields are `not_applicable` for LXC unless PVE exposes an equivalent
read endpoint that is added to this spec.

## 9. Canonical data contract

All records include `run_id`, `cluster_id`, `collected_at`, and a stable resource
key. Optional fields are nullable; missing data is never converted to zero.

### 9.1 Cluster

```jsonc
{
  "id": "primary",
  "name": "Home Lab",
  "pve_version": "9.2.11",
  "quorate": true,
  "node_count": 4,
  "guest_count": 20,
  "coverage": {"complete": 18, "partial": 2, "failed": 0}
}
```

### 9.2 Guest

Required core fields:

- identity: `type`, `vmid`, `name`, `node`, `template`, `pool`, `tags`;
- state: `status`, `uptime_seconds`, `ha_state`, `lock`;
- compute: `sockets`, `cores`, `vcpus`, `cpu_type`, `numa`, current CPU;
- memory: assigned bytes, current used bytes, ballooning configuration;
- boot: BIOS/UEFI, machine type, OS type, boot order, on-boot;
- agent: configured, responsive, version, OS name/version/kernel;
- collection: completeness state and per-section errors.

### 9.3 Child records

- `guest_disks`: bus/device, volume id, storage id, size, format, cache, discard,
  SSD emulation, backup flag, replication flag, unused flag.
- `guest_filesystems`: mountpoint, filesystem type, total/used/free bytes, disk
  aliases. QEMU guest agent only in v3.
- `guest_networks`: configured model/bridge/VLAN/firewall/rate/MAC plus observed
  interface, IPs, and counters when available.
- `guest_snapshots`: name, description, timestamp, parent, current flag, age days.
- `rrd_rollups`: resource key, window, metric, average, peak, sample count, and
  coverage percentage.
- `nodes`: online state, version, kernel, uptime, CPU topology, memory, root disk,
  certificate expiry, update count, and collection errors.
- `storage`: storage id, node or shared scope, type, content classes, active,
  enabled, shared, total/used/available, and provisioned guest bytes.

### 9.4 Provenance and completeness

Every optional section has one state: `ok`, `empty`, `not_applicable`,
`not_configured`, `permission_denied`, `timeout`, `unsupported`, or `error`.
Exported rows include that state. This prevents a silent guest-agent failure from
looking like a VM with no disks, IPs, or filesystems.

## 10. Persistence

Extend the module-owned SQLite database. Migrations are additive and idempotent.

Tables:

- `inventory_runs(id, cluster_id, status, stage, started_at, finished_at,
  pve_version, progress_done, progress_total, warning_count, error_count)`;
- `inventory_records(run_id, kind, resource_key, parent_key, payload_json,
  completeness, error_code, error_detail)`;
- `health_findings(run_id, rule_id, severity, resource_key, title, evidence_json,
  recommendation)`;
- `inventory_state(cluster_id PRIMARY KEY, latest_run_id, collecting_run_id)`;
- `collection_events(id, run_id, ts, level, stage, resource_key, message)`.

Use the generic `inventory_records` table rather than a wide schema because PVE
config fields evolve and QEMU/LXC records differ. The canonical serializer still
emits the stable contract in section 9. Index `(run_id, kind, resource_key)` and
`(run_id, parent_key)`.

Do not store credentials, raw Authorization headers, LXC passwords, SSH keys, or
arbitrary upstream response bodies. Limit recorded upstream errors to a sanitized
code plus 300 characters.

## 11. Capacity calculations

Calculations run only on the new snapshot and record their inputs and coverage.

### 11.1 CPU

- `physical_cores = sum(node.maxcpu for online nodes)`
- `assigned_vcpu = sum(guest.vcpus for non-template guests)`
- `vcpu_ratio = assigned_vcpu / physical_cores`
- live cluster CPU remains core-weighted, as in v2.
- RRD average is the arithmetic mean of non-null samples. Peak is their maximum.

Do not call a vCPU allocation ratio utilization. They answer different questions.

### 11.2 Memory

- `physical_memory = sum(node.maxmem for online nodes)`
- `assigned_memory = sum(guest.maxmem for non-template guests)`
- `memory_allocation_ratio = assigned_memory / physical_memory`
- `live_memory_used = sum(current guest memory)` is reported separately.

### 11.3 Storage

- Local storage key: `(cluster_id, node, storage_id)`.
- Shared storage key: `(cluster_id, storage_id)` and counted once.
- `provisioned_bytes` comes from parsed guest disk configuration, not live disk
  counters.
- Do not sum the same shared storage returned by every node.
- Unknown or unparseable disk sizes reduce coverage instead of becoming zero.

## 12. Health rules

Rules are pure functions over the canonical snapshot. Each finding has a stable id,
severity, evidence, recommendation, and affected resource. Thresholds live in
module settings and export metadata.

| Rule id | Default severity | Trigger |
|---|---|---|
| `cluster.no_quorum` | critical | Cluster reports `quorate=false` |
| `cluster.partial_collection` | warning | Any required probe failed |
| `node.offline` | critical | A configured cluster node is offline |
| `node.version_skew` | warning | Online nodes report different PVE major/minor versions |
| `node.cert_expiring` | warning/critical | Certificate expires within 30/7 days |
| `node.updates_pending` | info/warning | Optional update probe returns 1-19/20+ packages |
| `storage.inactive` | warning | Enabled storage is not active on an applicable node |
| `storage.pressure` | warning/critical | Used bytes are at least 85%/95% |
| `capacity.cpu_overcommit` | info/warning | vCPU ratio is at least 2.0/4.0 |
| `capacity.memory_overcommit` | warning/critical | Allocation ratio is at least 1.0/1.2 |
| `guest.agent_disabled` | info | Running QEMU VM has agent disabled in config |
| `guest.agent_unresponsive` | warning | Agent is configured on a running VM but the probe fails |
| `guest.snapshot_stale` | warning | Non-current snapshot is older than 30 days |
| `guest.snapshot_count` | info/warning | Guest has at least 3/5 snapshots |
| `guest.cpu_peak` | warning | 24-hour peak is at least 90% with at least 50% sample coverage |
| `guest.memory_peak` | warning | 24-hour peak is at least 90% with at least 50% sample coverage |

An unavailable optional probe creates coverage information, not one health finding
per resource. `permission_denied` for 300 guests should produce one summarized
coverage warning, not 300 noisy findings.

Orphaned-volume detection is deferred. Storage content can contain templates,
base images, imports, backups, and intentionally unattached volumes; a naive VMID
comparison creates unsafe false positives. It can be added only with a documented
high-confidence classifier and remains read-only.

## 13. API surface

Existing v2 routes and response shapes remain supported.

### 13.1 New routes

| Route | Purpose |
|---|---|
| `GET /api/proxmox/inventory/status` | Latest snapshot and active job summary |
| `POST /api/proxmox/inventory/collect` | Start a deep collection, return `202` |
| `GET /api/proxmox/inventory/jobs/{job_id}` | Poll progress and bounded event list |
| `POST /api/proxmox/inventory/jobs/{job_id}/cancel` | Cooperative cancellation |
| `GET /api/proxmox/inventory/latest` | Snapshot summary and coverage |
| `GET /api/proxmox/inventory/table/{kind}` | Paginated table data |
| `GET /api/proxmox/inventory/guests/{node}/{type}/{vmid}` | Guest detail bundle |
| `GET /api/proxmox/inventory/health` | Findings and rule summary |
| `GET /api/proxmox/inventory/export.xlsx` | 13-sheet workbook |
| `GET /api/proxmox/inventory/export.json` | Canonical JSON snapshot |
| `GET /api/proxmox/inventory/export.csv.zip` | One CSV per workbook sheet |

`kind` is allowlisted to the canonical record kinds. Table query parameters are
`q`, repeated `filter`, `sort`, `direction`, `limit` (default 50, max 250), and
`cursor`. The API never accepts a SQL fragment or arbitrary field expression.

### 13.2 Collection request

```jsonc
{
  "rrd_window": "day",
  "include_guest_agent": true,
  "include_updates": false
}
```

The server constrains these values. Supported RRD windows are `hour`, `day`, and
`week`. Updates are off by default because they can be slow and more privileged.

### 13.3 Error contract

Top-level API failures use:

```json
{"detail":"operator-facing message","code":"stable_code","retryable":false}
```

Partial collection problems are events and completeness states in a successful
job response, not top-level `500` responses.

## 14. Excel and flat-file export

### 14.1 Workbook sheets

The `.xlsx` contains exactly 13 sheets in this order:

1. `Metadata`
2. `Info`
3. `CPU`
4. `Memory`
5. `Disk`
6. `Partition`
7. `Network`
8. `Tools`
9. `Snapshot`
10. `Host`
11. `Cluster`
12. `Storage`
13. `Health`

Each sheet has a frozen header, autofilter, stable column order, ISO 8601 UTC
timestamps, byte values as integers plus human-readable companion columns, and a
source cluster column even while only one cluster is supported.

### 14.2 Dependency and delivery decision

Do not add `openpyxl`, `pandas`, or `xlsxwriter`; CE does not install per-module
dependencies. Implement a small, module-local OOXML writer with `zipfile` and XML
from the standard library. It only needs strings, numbers, booleans, dates, shared
styles, worksheets, autofilters, and frozen panes.

Generate into `tempfile.SpooledTemporaryFile`, stream the response, and close it in
a response background task. No workbook is persisted to the module data directory.
The CE worker proxy already streams response bodies and preserves safe download
headers.

### 14.3 Export safety

- Prefix string cells beginning with `=`, `+`, `-`, or `@` with an apostrophe to
  prevent spreadsheet-formula injection.
- Strip XML-disallowed control characters.
- Bound each cell at Excel's 32,767-character limit and record truncation in
  `Metadata`.
- Bound sheet rows at 1,048,576. Refuse the export with `413` before writing a
  misleading partial workbook.
- Filename: `ageniusdesk-proxmox-<cluster>-<UTC timestamp>.xlsx` after slugging.

JSON uses the canonical contract. CSV ZIP mirrors the 13 sheet names and applies
the same formula-injection protection.

## 15. Authentication, authorization, and safety

### 15.1 Proxmox credentials

- API-token authentication only.
- One complete `user@realm!tokenid=secret` secret remains host-resolved.
- A PVEAuditor-style token is the supported inventory baseline.
- Guest-agent details may require additional `VM.Monitor` rights and must degrade
  cleanly if absent.
- Existing management privileges remain capability-specific: `VM.PowerMgmt` for
  power, and allocation/datastore rights for provisioning.
- The module must not infer global read-only state from a failed optional read.

### 15.2 AgeniusDesk roles

- Any authenticated viewer may use live summary, latest inventory, health, and
  export routes.
- Starting/cancelling collection requires operator or admin.
- Existing upstream mutations require operator or admin and continue through the
  module guard.
- The host must authorize isolated module requests and pass a trusted actor/role
  identity. The worker must never trust client-supplied `X-AGD-*` headers.

### 15.3 Read-only install mode

At install, CE should let the operator reduce the endpoint grant to `GET/HEAD`.
That is a real capability boundary and hides all power/provisioning controls. The
module's `read_only` setting remains a defense-in-depth operational switch, not a
substitute for a read-only host grant.

### 15.4 Data handling

Inventory may contain internal hostnames, IPs, MAC addresses, tags, storage names,
and OS versions. Keep it in module-owned SQLite, return it only through authenticated
module routes, and do not send it to an LLM, analytics service, or third-party API.

## 16. CE host prerequisites

V3 cannot be released safely against the current CE `main` branch until these are
true:

1. `HostBridgeCapability` models `host.http` instead of silently dropping it.
2. `bridge.mint()` creates endpoint-scoped HTTP grants.
3. `POST /api/_host/http/request` exists with method, host, path, header, secret,
   response-size, timeout, redirect, and pinned-IP enforcement.
4. Effective endpoint base URL and TLS settings are stored host-side per install;
   an isolated worker never reads a secret or chooses a host.
5. The in-process facade calls the same host-owned policy/client implementation;
   it must not reproduce request policy, read the secret store, or open the
   Proxmox connection itself.
6. The isolated reverse proxy strips client `X-AGD-User`/`X-AGD-Role`, performs
   role authorization, and injects trusted identity for audit.
7. In-process and isolated transports pass the same conformance tests.
8. Binary module responses preserve `Content-Type` and `Content-Disposition` and
   close their upstream stream on completion.

The current community module's `_host.py` has an in-process compatibility path, but
its isolated path calls a CE bridge route that is only specified, not implemented,
on the inspected CE branch. Treat isolation support as blocked until the companion
host work ships. Once it does, change the manifest from the current unrestricted
`network.enabled=true, hosts=[]` declaration to the validated `host.http` grant and
remove direct Proxmox egress/secret resolution from the module's in-process path.

## 17. Multi-cluster and scheduling follow-up

### 17.1 Multi-cluster, v3.1

Do not predeclare a fixed `primary` and `secondary` endpoint. That caps the feature,
creates confusing unused install fields, and does not generalize to other homelab
modules.

CE should add repeatable endpoint instances. The manifest declares one template,
and the operator creates named, host-pinned instances. The worker receives opaque
instance ids and display names, never base URLs or secrets. Proposed host contract:

```jsonc
"http": {
  "endpoint_templates": [{
    "id": "proxmox",
    "min_instances": 1,
    "max_instances": 8,
    "methods": ["GET", "POST", "DELETE"],
    "auth": {"type":"header","header":"Authorization","format":"PVEAPIToken={value}"}
  }]
}
```

The module then adds `cluster_id` to every existing route that would otherwise be
ambiguous. Existing unscoped routes alias the default cluster for compatibility.

### 17.2 Scheduled exports, v3.1

Community workers need a host-owned scheduler registration contract before this is
added. The host should trigger an authenticated module route and record run status;
workers must not run independent infinite loops. Destination options should reuse
the AgeniusDesk vault/backup abstractions. Arbitrary SMB paths and email delivery
are out of scope for the first scheduled version.

## 18. Module layout

Proposed files, all self-contained under `modules/proxmox/`:

```text
collector.py        bounded fan-out and progress
inventory.py        canonical normalization and completeness
capacity.py         roll-ups and formulas
health.py           pure health rules
store.py            inventory SQLite migration and atomic snapshot swap
export_xlsx.py      standard-library OOXML writer
export_flat.py      JSON and CSV ZIP
router.py           existing routes plus inventory API
client.py           typed PVE reads plus existing mutations
static/proxmox.html tab shell and dialogs
static/proxmox.js   view state, filters, pagination, detail drawer, jobs
tests/              unit, route, export, and contract tests
```

Do not split shared code outside the module subtree.

## 19. Delivery phases

### Phase 0: CE safety prerequisite

Delivered in CE 0.6.0 (2026-09-05, `docs/specs/2026-09-05-host-http-bridge-build.md`
in the CE repo). This module's 0.2.0 release consumes it: `min_app_version` is
`0.6.0`, the unrestricted `network` declaration is gone, the in-process
compatibility path is replaced by the host facade, and route classes are declared
in the manifest.

- Implement the `host.http` manifest model and bridge from the existing CE spec.
- Route the module's in-process calls through the same host-owned policy/client and
  remove its direct secret-store/network compatibility implementation.
- Add host-side read-only method reduction.
- Add trusted worker actor/role propagation and route authorization.
- Add conformance tests using the Proxmox module facade.

Exit: the current community module's cluster read and a guarded power call work in
`in_process`, `subprocess`, and `container` modes without exposing the token.

### Phase 1: Inventory foundation

- Add collection jobs, persistence, versioned canonical DTOs, and atomic swap.
- Collect cluster, node, storage, QEMU/LXC config/status, snapshots, and optional
  guest-agent details.
- Build Overview and Guests tabs plus the detail drawer.
- Preserve every current route and guarded action.

Exit: a mixed QEMU/LXC cluster can complete or partially complete a deep collection,
and the UI explains every missing section.

### Phase 2: Capacity and health

- Add RRD collection and rollups.
- Add storage de-duplication and allocation calculations.
- Implement the v3 rule set and Health/Capacity tabs.
- Enrich `/fleet-health` with critical/warning counts from the latest snapshot
  without triggering collection.

Exit: rule fixtures prove the calculations, thresholds, and low-noise coverage
behavior.

### Phase 3: Export and hardening

- Add JSON, CSV ZIP, and 13-sheet XLSX.
- Add export injection, limits, and round-trip tests.
- Run large synthetic inventory, cancellation, timeout, and partial-node tests.
- Update README, screenshots, install text, privilege guidance, and changelog.

Exit: Excel opens without repair warnings in Excel and LibreOffice, all sheets have
stable columns, and the export matches the latest snapshot totals.

### Phase 4: v3.1 host extensions

- Repeatable endpoint instances and multi-cluster aggregation.
- Host-owned scheduled collection/export.

This phase is separately releasable and does not block v3 single-cluster value.

## 20. Acceptance criteria

1. Opening Overview causes no deep collection and no guest-agent fan-out.
2. A 20-guest cluster similar to localhost v1 completes required inventory in
   under 30 seconds on a healthy LAN; optional probes may continue within the
   overall deadline.
3. One failed node yields a `partial` run, retains successful-node records, and
   never replaces a previous complete snapshot without clearly labeling coverage.
4. `401` marks the cluster credential invalid; `403` marks only the denied probe or
   existing mutation capability; `404` on an optional endpoint becomes
   `unsupported`.
5. Shared storage returned by four nodes contributes once to cluster totals.
6. QEMU guest-agent failure never prevents config, status, snapshot, or RRD data
   from rendering.
7. LXC records appear in the same inventory and exports with unsupported agent
   fields explicitly marked.
8. Every health finding is reproducible from exported evidence and rule settings.
9. No upstream mutation is added by this spec, and every existing mutation still
   passes role authorization, the read-only switch, capability denial, self-guest
   protection, confirmation, and audit.
10. A read-only endpoint grant makes upstream POST/DELETE impossible even if the
    worker is compromised.
11. The generated workbook has 13 sheets, cannot execute formulas sourced from PVE
    strings, and opens without a repair prompt.
12. The module works under every supported isolation mode after Phase 0 and keeps
    all code/state within its declared subtree/data directory.
13. The released manifest no longer declares unrestricted direct network access;
    its Proxmox access is represented by the operator-consented `host.http` grant.

## 21. Test plan

### Unit

- Config parsers for QEMU/LXC disks, networks, tags, boot, agent, and nullable
  values.
- RRD average/peak and sample coverage with gaps and counter resets.
- CPU/memory/storage formulas, including zero-node and mixed online/offline cases.
- Shared-storage de-duplication.
- Every health rule at below/equal/above threshold.
- OOXML cell escaping, formula-injection defense, sheet naming, row limits, and
  deterministic column order.

### Client

- Exact path/query for every supported PVE 8/9 read.
- One retry only on retryable failures.
- `401`, `403`, `404`, timeout, malformed JSON, and bridge truncation mapping.
- Guest-agent routes stay on the fixed allowlist.
- Bounded concurrency is not exceeded.

### Store and jobs

- Idempotent migration from the current `state.db`.
- Single-flight collection and cooperative cancellation.
- Failed run does not replace latest snapshot.
- Atomic replacement and old-row cleanup.
- Restart leaves no run stuck as active; stale `running` becomes `failed` with a
  restart reason.

### Router and security

- Role matrix for reads, collect/cancel, export, power, and provisioning.
- Client-supplied actor headers cannot spoof audit identity.
- Pagination/filter allowlists reject SQL-like field input.
- Export headers survive both worker proxies.
- Existing v2 route contract tests remain green.

### UI

- Empty, unconfigured, collecting, complete, partial, failed, and stale states.
- Search/filter/pagination across mixed QEMU/LXC fixtures.
- Detail drawer sections and completeness labels.
- No inline upstream data enters HTML without escaping.
- Narrow viewport keeps controls usable and tables horizontally scrollable.

### Scale fixture

Generate 8 nodes, 2,000 guests, 8,000 disks, 6,000 interfaces, 4,000 snapshots,
and bounded RRD series. Collection normalization, SQLite commit, paginated API, and
all exports must stay within the declared caps without unbounded memory growth.

## 22. Release and migration

- Call this product milestone v3, but bump the module semantic version from `0.1.0`
  to `0.2.0` when Phase 1 begins.
- Set `min_app_version` to the first CE release that ships the Phase 0 bridge and
  identity contract. Do not guess this value before that host release exists.
- Existing settings and audit rows migrate in place.
- Existing bookmarks/API clients continue to use the unmodified v2 routes.
- The first deep collection is opt-in. Upgrade must not immediately fan out across
  the cluster.
- If rollback removes v3 code, v2 ignores additive tables and continues to work.

## 23. Locked decisions

- Single cluster in v3; repeatable host endpoints in v3.1.
- Latest point-in-time snapshot only; no time-series database.
- PVE RRD is the source of trend context.
- QEMU and LXC are first-class.
- API token only.
- No SSH and no host configuration backup in this module.
- No generic guest-agent command surface.
- No snapshot mutations in v3.
- Dependency-free workbook generation.
- Existing guarded management features stay, but inventory and health lead the UX.

## 24. References

- [PVEViewer product reference](https://pveviewer.com/)
- [Proxmox VE API Viewer](https://pve.proxmox.com/pve-docs/api-viewer/)
- [Proxmox VE Administration Guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.html)
- Existing module v2 build contract: `SPEC.md`
