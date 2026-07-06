/**
 * Proxmox - community module front-end.
 *
 * AgeniusDesk injects proxmox.html and loads this script once. Behavior is bound
 * via document-level delegation + a MutationObserver that (re)mounts when the view
 * appears. The cluster view polls every 5s (one /cluster call rolls up the whole
 * cluster). Every mutation (power / create / clone / delete) is gated SERVER-SIDE
 * (read-only, per-capability denial, self-guest); the UI gating here is cosmetic.
 * Create/clone/delete are async in PVE: the module returns a task UPID which we
 * poll to completion before refreshing.
 */

const API = '/api/proxmox';
const POLL_MS = 5000;

let _poll = null;
let _settings = { read_only: false, self_node: '', self_vmid: null, self_type: '', caps_denied: [] };
let _nodes = [];                 // last-known node names, for the picker selects
const _optCache = {};            // node -> provision-options (storage/iso/tmpl/bridge)

function af(path, opts) {
  const f = (window.AgeniusDesk && window.AgeniusDesk.fetch) || window.fetch;
  return f(path, opts);
}
async function jget(p) { const r = await af(p); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }
async function jreq(p, method, body) {
  const r = await af(p, { method, headers: { 'Content-Type': 'application/json' }, body: body !== undefined ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}
const jpost = (p, body) => jreq(p, 'POST', body);
const jdel = (p, body) => jreq(p, 'DELETE', body);

function esc(s) { const el = document.createElement('span'); el.textContent = s == null ? '' : String(s); return el.innerHTML; }
function $(id) { return document.getElementById(id); }
function toast(msg, level) { if (window.AgeniusDesk && window.AgeniusDesk.notify) window.AgeniusDesk.notify(msg, level || 'info'); }

function gb(b) { return (b / (1 << 30)).toFixed(1); }
function fmtMem(m) { if (!m || !m.total) return '—'; return `${gb(m.used)}/${gb(m.total)} GB`; }
function fmtUptime(s) {
  if (!s) return '';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  return d ? `${d}d ${h}h` : `${h}h`;
}

function canPower() { return !_settings.read_only && !(_settings.caps_denied || []).includes('power'); }
function canAllocate() { return !_settings.read_only && !(_settings.caps_denied || []).includes('allocate'); }

// ── cluster stats row ─────────────────────────────────────────────────────────

function bar(pct, color) {
  const p = Math.max(0, Math.min(100, pct || 0));
  return `<div style="height:6px;background:var(--border-dim);border-radius:3px;overflow:hidden;margin-top:8px">
    <div style="width:${p}%;height:100%;background:${color}"></div></div>`;
}
function statTile(label, big, sub, pct, color) {
  return `<div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:12px 14px">
    <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-secondary)">${esc(label)}</div>
    <div style="font-size:20px;font-weight:700;margin-top:2px">${big}</div>
    <div style="font-size:11px;color:var(--text-secondary)">${esc(sub)}</div>
    ${pct == null ? '' : bar(pct, color)}
  </div>`;
}
function statsRow(t) {
  const m = t.mem || {}, d = t.disk || {};
  return `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px">
    ${statTile('CPU', `${t.cpu_pct || 0}%`, `${t.cores || 0} cores`, t.cpu_pct, '#60a5fa')}
    ${statTile('Memory', `${m.pct || 0}%`, fmtMem(m), m.pct, '#34d399')}
    ${statTile('Disk', `${d.pct || 0}%`, fmtMem(d), d.pct, '#a78bfa')}
    ${statTile('Guests', `${t.running || 0} / ${t.guests || 0}`, `running · ${t.nodes_online || 0}/${t.nodes || 0} nodes`, null, null)}
  </div>`;
}

// ── cluster render ────────────────────────────────────────────────────────────

function nodeCard(n) {
  const dot = n.online ? '#34d399' : '#ff6d5a';
  const self = n.is_self_host ? ' <span style="font-size:10px;opacity:0.6">(dashboard host)</span>' : '';
  return `<div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:12px;min-width:180px">
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
      <span style="width:8px;height:8px;border-radius:50%;background:${dot}"></span>
      <strong style="font-size:13px">${esc(n.name)}</strong>${self}
    </div>
    <div style="font-size:12px;color:var(--text-secondary);display:grid;grid-template-columns:1fr 1fr;gap:4px">
      <div>CPU ${esc(n.cpu_pct)}%</div><div>${esc(n.cores)} cores</div>
      <div style="grid-column:1/3">Mem ${esc(fmtMem(n.mem))} (${esc(n.mem ? n.mem.pct : 0)}%)</div>
      <div style="grid-column:1/3">Disk ${esc(fmtMem(n.disk))} · up ${esc(fmtUptime(n.uptime))}</div>
    </div>
  </div>`;
}

const POWER = [
  { key: 'start', label: 'Start', danger: false },
  { key: 'shutdown', label: 'Shutdown', danger: true },
  { key: 'stop', label: 'Force stop (hard)', danger: true },
  { key: 'reboot', label: 'Reboot', danger: true },
];
function btn(cls, ds, label, danger) {
  return `<button type="button" class="${cls}" ${ds}
    style="cursor:pointer;font-size:11px;padding:3px 8px;border-radius:4px;border:1px solid ${danger ? '#ff6d5a55' : 'var(--border-dim)'};background:transparent;color:${danger ? '#ff6d5a' : 'var(--text-secondary)'}">${esc(label)}</button>`;
}

function guestRow(g) {
  const running = g.status === 'running';
  const dot = running ? '#34d399' : '#94a3b8';
  const kind = g.type === 'lxc' ? 'LXC' : 'VM';
  const ds = `data-node="${esc(g.node)}" data-type="${esc(g.type)}" data-vmid="${esc(g.vmid)}" data-name="${esc(g.name)}"`;
  const parts = [];
  if (g.is_self) parts.push('<span style="font-size:11px;color:#fbbf24;border:1px solid #fbbf2455;border-radius:4px;padding:2px 6px">dashboard — console only</span>');
  if (!g.is_self && canPower()) {
    POWER.filter(a => (a.key === 'start') !== running)
      .forEach(a => parts.push(btn('pmx-act', ds + ` data-action="${a.key}"`, a.label, a.danger)));
  }
  if (canAllocate()) {
    parts.push(btn('pmx-clone', ds, 'Clone', false));
    if (!running && !g.is_self) parts.push(btn('pmx-del', ds, 'Delete', true));
  }
  if (!parts.length && _settings.read_only) parts.push('<span style="font-size:11px;opacity:0.5">read-only</span>');
  return `<tr style="border-top:1px solid var(--border-dim)">
    <td style="padding:6px 8px"><span style="width:7px;height:7px;border-radius:50%;background:${dot};display:inline-block;margin-right:6px"></span>${esc(g.name || '(unnamed)')}</td>
    <td style="padding:6px 8px;color:var(--text-secondary);font-size:12px">${esc(kind)} ${esc(g.vmid)}</td>
    <td style="padding:6px 8px;color:var(--text-secondary);font-size:12px">${esc(g.node)}</td>
    <td style="padding:6px 8px;font-size:12px">${esc(g.status)}</td>
    <td style="padding:6px 8px;color:var(--text-secondary);font-size:12px">${running ? esc(g.cpu_pct) + '% · ' + esc(fmtMem(g.mem)) : ''}</td>
    <td style="padding:6px 8px;text-align:right;white-space:nowrap">${parts.join(' ')}</td>
  </tr>`;
}

function render(data) {
  const c = data.cluster || {};
  _settings = data.settings || _settings;
  _nodes = (c.nodes || []).map(n => n.name);
  $('pmx-readonly-banner').style.display = _settings.read_only ? '' : 'none';
  const ro = $('pmx-readonly'); if (ro) ro.checked = !!_settings.read_only;
  const createBtn = $('pmx-create-toggle'); if (createBtn) createBtn.style.display = canAllocate() ? '' : 'none';
  const clr = $('pmx-clear-denied'); if (clr) clr.style.display = (_settings.caps_denied || []).length ? '' : 'none';
  if (!$('pmx-self-node').value) $('pmx-self-node').value = _settings.self_node || '';
  if (!$('pmx-self-vmid').value && _settings.self_vmid != null) $('pmx-self-vmid').value = _settings.self_vmid;

  const body = $('pmx-body');
  if (!c.reachable) {
    body.innerHTML = `<div style="background:var(--bg-panel);border:1px solid #ff6d5a55;border-left:3px solid #ff6d5a;border-radius:var(--radius);padding:14px">
      <strong style="font-size:14px">Cluster unreachable</strong>
      <div style="font-size:12px;opacity:0.7;margin-top:6px">${esc(c.error || 'No response from Proxmox. Check the console URL and API token.')}</div>
    </div>`;
    return;
  }
  const t = c.totals || {};
  const quorum = c.quorate === false ? ' · <span style="color:#fbbf24">no quorum</span>' : '';
  const denied = (_settings.caps_denied || []).length
    ? `<div style="font-size:11px;color:#fbbf24;margin-bottom:10px">Token lacks ${esc((_settings.caps_denied || []).join(' + '))} rights — those controls are hidden. Clear under Settings once fixed.</div>` : '';
  body.innerHTML = `
    ${statsRow(t)}
    ${denied}
    <div style="font-size:12px;color:var(--text-secondary);margin-bottom:10px">
      ${esc(t.nodes_online)}/${esc(t.nodes)} nodes online · ${esc(t.running)} running · ${esc(t.stopped)} stopped${quorum}
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px">${(c.nodes || []).map(nodeCard).join('')}</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="text-align:left;color:var(--text-secondary);font-size:11px;text-transform:uppercase;letter-spacing:0.5px">
        <th style="padding:0 8px 6px">Guest</th><th style="padding:0 8px 6px">ID</th><th style="padding:0 8px 6px">Node</th>
        <th style="padding:0 8px 6px">Status</th><th style="padding:0 8px 6px">Load</th><th></th>
      </tr></thead>
      <tbody>${(c.guests || []).map(guestRow).join('') || '<tr><td colspan="6" style="padding:12px;opacity:0.6">No guests.</td></tr>'}</tbody>
    </table>`;
}

async function refresh() {
  try { render(await jget(`${API}/cluster`)); }
  catch (e) { const b = $('pmx-body'); if (b) b.innerHTML = `<div style="color:#ff6d5a;font-size:13px">${esc(e.message)}</div>`; }
}

// ── modal + async task polling ────────────────────────────────────────────────

function openModal(html) { $('pmx-modal-card').innerHTML = html; $('pmx-modal').style.display = ''; }
function closeModal() { $('pmx-modal').style.display = 'none'; $('pmx-modal-card').innerHTML = ''; }

const fieldStyle = 'width:100%;box-sizing:border-box';
function field(label, inner) {
  return `<label style="display:block;font-size:12px;margin-bottom:10px">
    <span style="color:var(--text-secondary)">${esc(label)}</span>
    <div style="margin-top:3px">${inner}</div></label>`;
}
function modalButtons(okId, okLabel, danger) {
  return `<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">
    <button type="button" id="pmx-modal-cancel" class="btn" style="cursor:pointer;font-size:12px;padding:6px 12px;border:1px solid var(--border-dim);border-radius:var(--radius);background:var(--bg-panel);color:var(--text-secondary)">Cancel</button>
    <button type="button" id="${okId}" class="btn" style="cursor:pointer;font-size:12px;padding:6px 14px;border-radius:var(--radius);border:1px solid ${danger ? '#ff6d5a' : 'transparent'};background:${danger ? '#ff6d5a' : 'var(--accent,#60a5fa)'};color:#fff">${esc(okLabel)}</button>
  </div>`;
}
function setStatus(msg, err) {
  let el = $('pmx-modal-status');
  if (!el) { el = document.createElement('div'); el.id = 'pmx-modal-status'; el.style.cssText = 'font-size:12px;margin-top:10px'; $('pmx-modal-card').appendChild(el); }
  el.style.color = err ? '#ff6d5a' : 'var(--text-secondary)';
  el.textContent = msg;
}

// Poll a PVE task to completion; resolves true on OK, false on failure/timeout.
async function pollTask(node, upid, label) {
  if (!upid) { return true; }
  const started = Date.now();
  while (Date.now() - started < 10 * 60 * 1000) {
    let s;
    try { s = await jget(`${API}/tasks/${encodeURIComponent(node)}/${encodeURIComponent(upid)}`); }
    catch (e) { setStatus(`${label}: ${e.message}`, true); return false; }
    if (s.done) {
      if (s.ok) { setStatus(`${label}: done`, false); return true; }
      setStatus(`${label} failed: ${s.exitstatus || 'error'}`, true);
      return false;
    }
    setStatus(`${label}… (${s.status || 'running'})`, false);
    await new Promise(r => setTimeout(r, 1500));
  }
  setStatus(`${label}: still running (timed out watching)`, true);
  return false;
}

// ── create wizard ─────────────────────────────────────────────────────────────

async function loadOptions(node) {
  if (_optCache[node]) return _optCache[node];
  const o = await jget(`${API}/nodes/${encodeURIComponent(node)}/provision-options`);
  _optCache[node] = o;
  return o;
}
function opts(list, valueKey, labelFn) {
  return list.map(x => `<option value="${esc(x[valueKey])}">${esc(labelFn(x))}</option>`).join('');
}

function createFormHTML() {
  const nodeOpts = _nodes.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  return `<h2 style="font-size:16px;font-weight:700;margin:0 0 14px">New guest</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 12px">
      ${field('Node', `<select id="pmx-c-node" class="input" style="${fieldStyle}">${nodeOpts}</select>`)}
      ${field('Type', `<select id="pmx-c-type" class="input" style="${fieldStyle}"><option value="qemu">VM (qemu)</option><option value="lxc">LXC container</option></select>`)}
      ${field('VMID', `<input id="pmx-c-vmid" type="number" class="input" style="${fieldStyle}">`)}
      ${field('Name / hostname', `<input id="pmx-c-name" class="input" style="${fieldStyle}">`)}
      ${field('Cores', `<input id="pmx-c-cores" type="number" value="1" class="input" style="${fieldStyle}">`)}
      ${field('Memory (MiB)', `<input id="pmx-c-memory" type="number" value="2048" class="input" style="${fieldStyle}">`)}
      ${field('Disk (GB)', `<input id="pmx-c-disk" type="number" value="16" class="input" style="${fieldStyle}">`)}
      ${field('Storage', `<select id="pmx-c-storage" class="input" style="${fieldStyle}"></select>`)}
      <div id="pmx-c-vm-only" style="display:contents">
        ${field('Install ISO', `<select id="pmx-c-iso" class="input" style="${fieldStyle}"></select>`)}
        ${field('OS type', `<input id="pmx-c-ostype" value="l26" class="input" style="${fieldStyle}">`)}
      </div>
      <div id="pmx-c-lxc-only" style="display:none">
        ${field('Template', `<select id="pmx-c-template" class="input" style="${fieldStyle}"></select>`)}
        ${field('Root password', `<input id="pmx-c-password" type="password" class="input" style="${fieldStyle}">`)}
      </div>
      ${field('Bridge', `<select id="pmx-c-bridge" class="input" style="${fieldStyle}"></select>`)}
      ${field('VLAN (optional)', `<input id="pmx-c-vlan" type="number" class="input" style="${fieldStyle}">`)}
    </div>
    <div id="pmx-c-lxc-net" style="display:none">
      ${field('IP (dhcp or CIDR, e.g. 10.0.0.5/24)', `<input id="pmx-c-ip" value="dhcp" class="input" style="${fieldStyle}">`)}
      ${field('SSH public keys (optional)', `<textarea id="pmx-c-ssh" class="input" rows="2" style="${fieldStyle}"></textarea>`)}
    </div>
    <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin:4px 0 2px"><input id="pmx-c-start" type="checkbox"> Start after create</label>
    ${modalButtons('pmx-c-submit', 'Create', false)}`;
}

function fillPickers(o, gtype) {
  const stores = (o.storages || []).filter(s => s.active && s.content.includes(gtype === 'lxc' ? 'rootdir' : 'images'));
  $('pmx-c-storage').innerHTML = opts(stores, 'name', s => `${s.name} (${gb(s.avail)} GB free)`)
    || '<option value="">no compatible storage</option>';
  const brs = o.bridges || [];
  $('pmx-c-bridge').innerHTML = opts(brs, 'iface', b => b.iface) || '<option value="vmbr0">vmbr0</option>';
  if (gtype === 'qemu') {
    $('pmx-c-iso').innerHTML = '<option value="">(none — no install media)</option>' + opts(o.isos || [], 'volid', i => i.volid);
  } else {
    $('pmx-c-template').innerHTML = opts(o.templates || [], 'volid', t => t.volid) || '<option value="">no templates found</option>';
  }
  if ((o.warnings || []).length) setStatus('Some options unavailable: ' + o.warnings.join('; '), false);
}

function toggleType() {
  const gtype = $('pmx-c-type').value;
  $('pmx-c-vm-only').style.display = gtype === 'qemu' ? 'contents' : 'none';
  $('pmx-c-lxc-only').style.display = gtype === 'lxc' ? 'contents' : 'none';
  $('pmx-c-lxc-net').style.display = gtype === 'lxc' ? '' : 'none';
  const o = _optCache[$('pmx-c-node').value];
  if (o) fillPickers(o, gtype);
}

async function reloadCreateOptions() {
  const node = $('pmx-c-node').value;
  setStatus('Loading options…', false);
  try { const o = await loadOptions(node); fillPickers(o, $('pmx-c-type').value); setStatus('', false); }
  catch (e) { setStatus('Could not load node options: ' + e.message, true); }
}

async function openCreate() {
  if (!_nodes.length) { toast('No nodes available', 'error'); return; }
  openModal(createFormHTML());
  try { const { vmid } = await jget(`${API}/nextid`); $('pmx-c-vmid').value = vmid; } catch (e) { /* leave blank */ }
  await reloadCreateOptions();
}

function readCreatePayload(gtype) {
  const num = (id) => { const v = $(id).value; return v === '' ? null : parseInt(v, 10); };
  const base = {
    vmid: num('pmx-c-vmid'),
    cores: num('pmx-c-cores') || 1,
    memory: num('pmx-c-memory') || (gtype === 'lxc' ? 512 : 2048),
    storage: $('pmx-c-storage').value,
    disk_gb: num('pmx-c-disk') || 8,
    bridge: $('pmx-c-bridge').value || 'vmbr0',
    vlan: num('pmx-c-vlan'),
    start: $('pmx-c-start').checked,
  };
  if (gtype === 'qemu') {
    return { ...base, name: $('pmx-c-name').value.trim() || null, iso: $('pmx-c-iso').value || null, ostype: $('pmx-c-ostype').value.trim() || 'l26' };
  }
  return {
    ...base, hostname: $('pmx-c-name').value.trim() || null, ostemplate: $('pmx-c-template').value,
    ip: $('pmx-c-ip').value.trim() || 'dhcp', password: $('pmx-c-password').value || null,
    ssh_public_keys: $('pmx-c-ssh').value.trim() || null, unprivileged: true,
  };
}

async function submitCreate() {
  const node = $('pmx-c-node').value, gtype = $('pmx-c-type').value;
  const payload = readCreatePayload(gtype);
  if (!payload.vmid) { setStatus('VMID is required', true); return; }
  if (!payload.storage) { setStatus('Pick a storage', true); return; }
  if (gtype === 'lxc' && !payload.ostemplate) { setStatus('Pick a template', true); return; }
  $('pmx-c-submit').disabled = true;
  setStatus('Submitting…', false);
  try {
    const r = await jpost(`${API}/provision/${encodeURIComponent(node)}/${gtype}`, payload);
    const ok = await pollTask(node, r.upid, `Creating ${gtype} ${payload.vmid}`);
    if (ok && payload.start) { try { await jpost(`${API}/guests/${encodeURIComponent(node)}/${gtype}/${payload.vmid}/start`); } catch (e) { /* surfaced on refresh */ } }
    if (ok) { toast(`Created ${gtype} ${payload.vmid}`, 'success'); setTimeout(closeModal, 700); }
  } catch (e) { setStatus(e.message, true); }
  finally { const b = $('pmx-c-submit'); if (b) b.disabled = false; refresh(); }
}

// ── clone ─────────────────────────────────────────────────────────────────────

async function openClone(g) {
  const nodeOpts = _nodes.map(n => `<option value="${esc(n)}"${n === g.node ? ' selected' : ''}>${esc(n)}</option>`).join('');
  openModal(`<h2 style="font-size:16px;font-weight:700;margin:0 0 4px">Clone ${esc(g.name || g.vmid)}</h2>
    <p style="font-size:12px;color:var(--text-secondary);margin:0 0 14px">Copy ${esc(g.type === 'lxc' ? 'LXC' : 'VM')} ${esc(g.vmid)} on ${esc(g.node)} to a new guest.</p>
    ${field('New VMID', `<input id="pmx-cl-newid" type="number" class="input" style="${fieldStyle}">`)}
    ${field('New name / hostname', `<input id="pmx-cl-name" class="input" style="${fieldStyle}">`)}
    ${field('Target node', `<select id="pmx-cl-target" class="input" style="${fieldStyle}">${nodeOpts}</select>`)}
    ${g.type === 'qemu' ? `<label style="display:flex;align-items:center;gap:8px;font-size:13px;margin:4px 0"><input id="pmx-cl-full" type="checkbox" checked> Full clone (independent copy)</label>` : '<div style="font-size:11px;color:var(--text-secondary);margin-bottom:6px">LXC clones are always full.</div>'}
    ${modalButtons('pmx-cl-submit', 'Clone', false)}`);
  $('pmx-cl-submit').dataset.node = g.node; $('pmx-cl-submit').dataset.type = g.type; $('pmx-cl-submit').dataset.vmid = g.vmid;
  try { const { vmid } = await jget(`${API}/nextid`); $('pmx-cl-newid').value = vmid; } catch (e) { /* leave blank */ }
}

async function submitClone(el) {
  const { node, type, vmid } = el.dataset;
  const newidRaw = $('pmx-cl-newid').value;
  const payload = {
    newid: newidRaw ? parseInt(newidRaw, 10) : null,
    name: $('pmx-cl-name').value.trim() || null,
    target: $('pmx-cl-target').value || null,
    full: $('pmx-cl-full') ? $('pmx-cl-full').checked : true,
  };
  el.disabled = true; setStatus('Submitting…', false);
  try {
    const r = await jpost(`${API}/provision/${encodeURIComponent(node)}/${type}/${vmid}/clone`, payload);
    const ok = await pollTask(node, r.upid, `Cloning ${type} ${vmid}`);
    if (ok) { toast('Clone complete', 'success'); setTimeout(closeModal, 700); }
  } catch (e) { setStatus(e.message, true); }
  finally { const b = $('pmx-cl-submit'); if (b) b.disabled = false; refresh(); }
}

// ── delete (type-to-confirm) ──────────────────────────────────────────────────

function openDelete(g) {
  openModal(`<h2 style="font-size:16px;font-weight:700;margin:0 0 4px;color:#ff6d5a">Delete ${esc(g.name || g.vmid)}</h2>
    <p style="font-size:12px;color:var(--text-secondary);margin:0 0 12px">This permanently destroys ${esc(g.type === 'lxc' ? 'LXC' : 'VM')} <strong>${esc(g.vmid)}</strong> on ${esc(g.node)} and its disks. This cannot be undone. The guest must be stopped.</p>
    ${field(`Type the VMID (${esc(g.vmid)}) to confirm`, `<input id="pmx-del-confirm" type="number" class="input" style="${fieldStyle}">`)}
    ${modalButtons('pmx-del-submit', 'Delete permanently', true)}`);
  const b = $('pmx-del-submit');
  b.dataset.node = g.node; b.dataset.type = g.type; b.dataset.vmid = g.vmid;
}

async function submitDelete(el) {
  const { node, type, vmid } = el.dataset;
  const confirmVmid = parseInt($('pmx-del-confirm').value, 10);
  if (confirmVmid !== parseInt(vmid, 10)) { setStatus('VMID does not match', true); return; }
  el.disabled = true; setStatus('Deleting…', false);
  try {
    const r = await jdel(`${API}/guests/${encodeURIComponent(node)}/${type}/${vmid}`, { confirm_vmid: confirmVmid });
    const ok = await pollTask(node, r.upid, `Deleting ${type} ${vmid}`);
    if (ok) { toast(`Deleted ${type} ${vmid}`, 'success'); setTimeout(closeModal, 700); }
  } catch (e) { setStatus(e.message, true); }
  finally { const b = $('pmx-del-submit'); if (b) b.disabled = false; refresh(); }
}

// ── event delegation ──────────────────────────────────────────────────────────

function guestFromEl(el) {
  return { node: el.dataset.node, type: el.dataset.type, vmid: el.dataset.vmid, name: el.dataset.name, status: '' };
}

document.addEventListener('click', async (ev) => {
  const target = ev.target;

  const act = target.closest('.pmx-act');
  if (act) {
    const { node, type, vmid, name } = act.dataset;
    const action = act.dataset.action;
    const label = (POWER.find(a => a.key === action) || {}).label || action;
    if (action !== 'start' && !confirm(`${label} ${name} (${type} ${vmid})?`)) return;
    act.disabled = true;
    try { await jpost(`${API}/guests/${encodeURIComponent(node)}/${encodeURIComponent(type)}/${encodeURIComponent(vmid)}/${action}`); }
    catch (e) { toast(e.message, 'error'); }
    finally { refresh(); }
    return;
  }
  const cl = target.closest('.pmx-clone'); if (cl) { openClone(guestFromEl(cl)); return; }
  const dl = target.closest('.pmx-del'); if (dl) { openDelete(guestFromEl(dl)); return; }

  if (target.id === 'pmx-create-toggle') { openCreate(); return; }
  if (target.id === 'pmx-c-submit') { submitCreate(); return; }
  if (target.id === 'pmx-cl-submit') { submitClone(target); return; }
  if (target.id === 'pmx-del-submit') { submitDelete(target); return; }
  if (target.id === 'pmx-modal-cancel' || target.id === 'pmx-modal') { closeModal(); return; }

  if (target.id === 'pmx-settings-toggle') {
    const s = $('pmx-settings'); s.style.display = s.style.display === 'none' ? '' : 'none';
    return;
  }
  if (target.id === 'pmx-self-save') {
    const vmidRaw = $('pmx-self-vmid').value;
    try {
      await jpost(`${API}/settings`, { self_node: $('pmx-self-node').value.trim(), self_type: $('pmx-self-type').value, self_vmid: vmidRaw ? parseInt(vmidRaw, 10) : null });
      refresh();
    } catch (e) { toast(e.message, 'error'); }
    return;
  }
  if (target.id === 'pmx-clear-denied') {
    try { await jpost(`${API}/settings`, { clear_denied: true }); refresh(); } catch (e) { toast(e.message, 'error'); }
    return;
  }
});

document.addEventListener('change', async (ev) => {
  if (ev.target.id === 'pmx-readonly') {
    try { await jpost(`${API}/settings`, { read_only: ev.target.checked }); refresh(); }
    catch (e) { toast(e.message, 'error'); }
    return;
  }
  if (ev.target.id === 'pmx-c-node') { reloadCreateOptions(); return; }
  if (ev.target.id === 'pmx-c-type') { toggleType(); return; }
});

// ── mount + poll ──────────────────────────────────────────────────────────────

function startPolling() { stopPolling(); _poll = setInterval(refresh, POLL_MS); }
function stopPolling() { if (_poll) { clearInterval(_poll); _poll = null; } }

async function mount() { await refresh(); startPolling(); }

const _obs = new MutationObserver(() => {
  const root = $('pmx-root');
  if (root && root.dataset.mounted !== '1') { root.dataset.mounted = '1'; mount(); }
  else if (!root && _poll) { stopPolling(); }  // view left the DOM
});
_obs.observe(document.body, { childList: true, subtree: true });
if ($('pmx-root')) { $('pmx-root').dataset.mounted = '1'; mount(); }
