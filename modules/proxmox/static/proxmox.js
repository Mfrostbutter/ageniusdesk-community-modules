/**
 * Proxmox - community module front-end.
 *
 * AgeniusDesk injects proxmox.html and loads this script once. Behavior is bound
 * via document-level delegation + a MutationObserver that (re)mounts when the view
 * appears. No WebSocket: the cluster view polls every 5s (one /cluster call rolls
 * up the whole cluster). Power actions POST to the module; the SERVER enforces
 * self-protection + read-only, so hidden controls here are only cosmetic.
 */

const API = '/api/proxmox';
const POLL_MS = 5000;

let _poll = null;
let _settings = { read_only: false, self_node: '', self_vmid: null, self_type: '' };

function af(path, opts) {
  const f = (window.AgeniusDesk && window.AgeniusDesk.fetch) || window.fetch;
  return f(path, opts);
}
async function jget(p) { const r = await af(p); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }
async function jpost(p, body) {
  const r = await af(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}
function esc(s) { const el = document.createElement('span'); el.textContent = s == null ? '' : String(s); return el.innerHTML; }
function $(id) { return document.getElementById(id); }

function fmtMem(m) {
  if (!m || !m.total) return '—';
  const gb = (b) => (b / (1 << 30)).toFixed(1);
  return `${gb(m.used)}/${gb(m.total)} GB (${m.pct}%)`;
}
function fmtUptime(s) {
  if (!s) return '';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  return d ? `${d}d ${h}h` : `${h}h`;
}

// Guest power actions. `stop` is a HARD pull in PVE, so it is labeled distinctly.
const ACTIONS = [
  { key: 'start', label: 'Start', danger: false },
  { key: 'shutdown', label: 'Shutdown', danger: true },
  { key: 'stop', label: 'Force stop (hard)', danger: true },
  { key: 'reboot', label: 'Reboot', danger: true },
];

function nodeCard(n) {
  const dot = n.online ? '#34d399' : '#ff6d5a';
  const self = n.is_self_host ? ' <span style="font-size:10px;opacity:0.6">(dashboard host)</span>' : '';
  return `<div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:12px;min-width:180px">
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
      <span style="width:8px;height:8px;border-radius:50%;background:${dot}"></span>
      <strong style="font-size:13px">${esc(n.name)}</strong>${self}
    </div>
    <div style="font-size:12px;color:var(--text-secondary);display:grid;grid-template-columns:1fr 1fr;gap:4px">
      <div>CPU ${esc(n.cpu_pct)}%</div><div>up ${esc(fmtUptime(n.uptime))}</div>
      <div style="grid-column:1/3">Mem ${esc(fmtMem(n.mem))}</div>
    </div>
  </div>`;
}

function guestRow(g) {
  const running = g.status === 'running';
  const dot = running ? '#34d399' : '#94a3b8';
  const kind = g.type === 'lxc' ? 'LXC' : 'VM';
  let controls;
  if (g.is_self) {
    controls = '<span style="font-size:11px;color:#fbbf24;border:1px solid #fbbf2455;border-radius:4px;padding:2px 6px">dashboard — console only</span>';
  } else if (_settings.read_only) {
    controls = '<span style="font-size:11px;opacity:0.5">read-only</span>';
  } else {
    controls = ACTIONS.filter(a => (a.key === 'start') !== running)
      .map(a => `<button type="button" class="pmx-act" data-node="${esc(g.node)}" data-type="${esc(g.type)}" data-vmid="${esc(g.vmid)}" data-name="${esc(g.name)}" data-action="${a.key}"
        style="cursor:pointer;font-size:11px;padding:3px 8px;border-radius:4px;border:1px solid ${a.danger ? '#ff6d5a55' : 'var(--border-dim)'};background:transparent;color:${a.danger ? '#ff6d5a' : 'var(--text-secondary)'}">${esc(a.label)}</button>`)
      .join(' ');
  }
  return `<tr style="border-top:1px solid var(--border-dim)">
    <td style="padding:6px 8px"><span style="width:7px;height:7px;border-radius:50%;background:${dot};display:inline-block;margin-right:6px"></span>${esc(g.name || '(unnamed)')}</td>
    <td style="padding:6px 8px;color:var(--text-secondary);font-size:12px">${esc(kind)} ${esc(g.vmid)}</td>
    <td style="padding:6px 8px;color:var(--text-secondary);font-size:12px">${esc(g.node)}</td>
    <td style="padding:6px 8px;font-size:12px">${esc(g.status)}</td>
    <td style="padding:6px 8px;color:var(--text-secondary);font-size:12px">${running ? esc(g.cpu_pct) + '% · ' + esc(fmtMem(g.mem)) : ''}</td>
    <td style="padding:6px 8px;text-align:right;white-space:nowrap">${controls}</td>
  </tr>`;
}

function render(data) {
  const c = data.cluster || {};
  _settings = data.settings || _settings;
  $('pmx-readonly-banner').style.display = _settings.read_only ? '' : 'none';
  const ro = $('pmx-readonly'); if (ro) ro.checked = !!_settings.read_only;
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
  body.innerHTML = `
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

// ── actions (document-delegated) ──────────────────────────────────────────────

document.addEventListener('click', async (ev) => {
  const act = ev.target.closest('.pmx-act');
  if (act) {
    const { node, type, vmid, name, action } = act.dataset;
    const label = (ACTIONS.find(a => a.key === action) || {}).label || action;
    if (action !== 'start' && !confirm(`${label} ${name} (${type} ${vmid})?`)) return;
    act.disabled = true;
    try { await jpost(`${API}/guests/${encodeURIComponent(node)}/${encodeURIComponent(type)}/${encodeURIComponent(vmid)}/${action}`); }
    catch (e) { alert(e.message); }
    finally { refresh(); }
    return;
  }
  if (ev.target.id === 'pmx-settings-toggle') {
    const s = $('pmx-settings'); s.style.display = s.style.display === 'none' ? '' : 'none';
    return;
  }
  if (ev.target.id === 'pmx-self-save') {
    const vmidRaw = $('pmx-self-vmid').value;
    try {
      await jpost(`${API}/settings`, {
        self_node: $('pmx-self-node').value.trim(),
        self_type: $('pmx-self-type').value,
        self_vmid: vmidRaw ? parseInt(vmidRaw, 10) : null,
      });
      refresh();
    } catch (e) { alert(e.message); }
    return;
  }
});

document.addEventListener('change', async (ev) => {
  if (ev.target.id === 'pmx-readonly') {
    try { await jpost(`${API}/settings`, { read_only: ev.target.checked }); refresh(); }
    catch (e) { alert(e.message); }
  }
});

// ── mount + poll ──────────────────────────────────────────────────────────────

function startPolling() { stopPolling(); _poll = setInterval(refresh, POLL_MS); }
function stopPolling() { if (_poll) { clearInterval(_poll); _poll = null; } }

async function mount() {
  await refresh();
  startPolling();
}

const _obs = new MutationObserver(() => {
  const root = $('pmx-root');
  if (root && root.dataset.mounted !== '1') { root.dataset.mounted = '1'; mount(); }
  else if (!root && _poll) { stopPolling(); }  // view left the DOM
});
_obs.observe(document.body, { childList: true, subtree: true });
if ($('pmx-root')) { $('pmx-root').dataset.mounted = '1'; mount(); }
