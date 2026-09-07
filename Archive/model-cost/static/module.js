/**
 * Model Cost - community module front-end.
 *
 * AgeniusDesk injects model-cost.html and loads this script once per view open.
 * One GET /summary drives the whole view: a provider strip (actual vs estimated
 * reconciliation) and a per-model table the window tabs filter. Refresh forces a
 * server-side re-poll. All cost figures are read-only; nothing here holds a key.
 */

const API = '/api/model-cost';

let _data = null;
let _win = 'mtd';

function af(path, opts) {
  const f = (window.AgeniusDesk && window.AgeniusDesk.fetch) || window.fetch;
  return f(path, opts);
}
async function jget(p) { const r = await af(p); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }
async function jpost(p) {
  const r = await af(p, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}
function $(id) { return document.getElementById(id); }
function esc(s) { const el = document.createElement('span'); el.textContent = s == null ? '' : String(s); return el.innerHTML; }
function toast(m, l) { if (window.AgeniusDesk && window.AgeniusDesk.notify) window.AgeniusDesk.notify(m, l || 'info'); }

function usd(n) {
  const v = Number(n) || 0;
  return '$' + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function tokens(n) {
  const v = Number(n) || 0;
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
  return String(Math.round(v));
}
function shortModel(name) {
  let n = String(name || '');
  if (n.includes('/')) n = n.split('/', 2)[1];
  return n;
}
function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  return Math.floor(s / 3600) + 'h ago';
}

// ── provider strip ──────────────────────────────────────────────────────────

function estSum(source, window) {
  return (_data.models || [])
    .filter(m => m.source === source && m.window === window)
    .reduce((a, m) => a + (Number(m.cost) || 0), 0);
}

// Anthropic/OpenAI headline on MTD (estimated per model, actual total shown);
// OpenRouter headline on its 30d actual spend.
function providerCard(p) {
  const badge = (text, color) =>
    `<span style="font-size:10px;padding:1px 7px;border-radius:10px;background:${color}22;color:${color};border:1px solid ${color}55">${esc(text)}</span>`;
  let status, body;
  if (!p.configured) {
    status = badge('not configured', '#94a3b8');
    body = `<div style="font-size:12px;opacity:0.6;margin-top:8px">Add its key under Settings ▸ Modules, then grant the endpoint.</div>`;
  } else if (!p.reachable) {
    status = badge('unreachable', '#ff6d5a');
    body = `<div style="font-size:12px;color:#ff6d5a;margin-top:8px">${esc(p.error || 'error')}</div>`;
  } else {
    status = badge('ok', '#34d399');
    const cost = p.cost || {};
    if (cost['30d']) {
      body = `<div style="font-size:22px;font-weight:700;margin-top:6px">${usd(cost['30d'].amount)}</div>
              <div style="font-size:11px;color:var(--text-secondary)">actual · 30 days · ${p.model_count} models</div>`;
    } else {
      const mtd = (cost.mtd || {}).amount || 0;
      const est = estSum(p.id, 'mtd');
      body = `<div style="font-size:22px;font-weight:700;margin-top:6px">${usd(mtd)}</div>
              <div style="font-size:11px;color:var(--text-secondary)">actual month · est ${usd(est)} · ${p.model_count} models</div>`;
    }
  }
  return `<div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:14px">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
      <strong style="font-size:14px">${esc(p.name)}</strong>${status}
    </div>${body}</div>`;
}

// ── model table ─────────────────────────────────────────────────────────────

function basisBadge(basis) {
  if (basis === 'actual') return `<span style="font-size:10px;color:#34d399">actual</span>`;
  if (basis === 'unpriced') return `<span style="font-size:10px;color:#94a3b8" title="no price on file for this model">unpriced</span>`;
  return `<span style="font-size:10px;color:var(--text-secondary)">est</span>`;
}

function modelTable(win) {
  const rows = (_data.models || [])
    .filter(m => m.window === win)
    .sort((a, b) => (Number(b.cost) || 0) - (Number(a.cost) || 0));
  if (!rows.length) {
    const hint = win === '30d'
      ? 'No OpenRouter per-model data. It needs a provisioning/management key.'
      : 'No per-model data for this window yet.';
    return `<div style="opacity:0.6;font-size:13px;padding:8px 0">${esc(hint)}</div>`;
  }
  const total = rows.reduce((a, m) => a + (Number(m.cost) || 0), 0);
  const body = rows.map(m => `
    <tr style="border-top:1px solid var(--border-dim)">
      <td style="padding:7px 8px;font-size:13px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(m.model)}">${esc(shortModel(m.model))}</td>
      <td style="padding:7px 8px;font-size:12px;color:var(--text-secondary)">${esc(m.source_name)}</td>
      <td style="padding:7px 8px;font-size:12px;text-align:right;color:var(--text-secondary)">${tokens(m.input_tokens)} / ${tokens(m.output_tokens)}</td>
      <td style="padding:7px 8px;font-size:12px;text-align:right">${tokens(m.tokens)}</td>
      <td style="padding:7px 8px;font-size:13px;text-align:right;font-weight:600">${usd(m.cost)} ${basisBadge(m.cost_basis)}</td>
    </tr>`).join('');
  return `
    <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse">
      <thead><tr style="text-align:left;color:var(--text-secondary);font-size:11px;text-transform:uppercase;letter-spacing:0.5px">
        <th style="padding:6px 8px">Model</th>
        <th style="padding:6px 8px">Source</th>
        <th style="padding:6px 8px;text-align:right">Tokens (in/out)</th>
        <th style="padding:6px 8px;text-align:right">Total</th>
        <th style="padding:6px 8px;text-align:right">Cost</th>
      </tr></thead>
      <tbody>${body}</tbody>
      <tfoot><tr style="border-top:2px solid var(--border-dim)">
        <td colspan="4" style="padding:8px;font-size:12px;color:var(--text-secondary)">${rows.length} model${rows.length === 1 ? '' : 's'}</td>
        <td style="padding:8px;text-align:right;font-weight:700">${usd(total)}</td>
      </tr></tfoot>
    </table></div>`;
}

// ── render ──────────────────────────────────────────────────────────────────

function paintTabs() {
  document.querySelectorAll('.mc-tab').forEach(b => {
    const on = b.dataset.win === _win;
    b.style.borderBottomColor = on ? 'var(--accent,#60a5fa)' : 'transparent';
    b.style.color = on ? 'var(--text-primary,inherit)' : 'var(--text-muted,var(--text-secondary))';
  });
}

function renderAll() {
  if (!_data) return;
  const providers = _data.providers || [];
  const configured = providers.filter(p => p.configured);
  const un = $('mc-unconfigured');
  if (un) {
    if (!configured.length) {
      un.style.display = 'block';
      un.textContent = 'No providers configured yet. Add an Anthropic, OpenAI, or OpenRouter admin key under Settings ▸ Modules and grant its endpoint, then Refresh.';
    } else {
      un.style.display = 'none';
    }
  }
  const strip = $('mc-providers');
  if (strip) strip.innerHTML = providers.map(providerCard).join('');
  const models = $('mc-models');
  if (models) models.innerHTML = modelTable(_win);
  const updated = $('mc-updated');
  if (updated) updated.textContent = _data.generated_at ? `Updated ${ago(_data.generated_at)} · cached up to 5 min` : '';
  paintTabs();
}

async function load() {
  try {
    _data = await jget(`${API}/summary`);
    renderAll();
  } catch (e) {
    const models = $('mc-models');
    if (models) models.innerHTML = `<div style="color:#ff6d5a;font-size:13px">Failed to load: ${esc(e.message)}</div>`;
  }
}

function init() {
  const root = $('mc-root');
  if (!root || root.dataset.mounted) return;
  root.dataset.mounted = '1';

  document.querySelectorAll('.mc-tab').forEach(b => {
    b.addEventListener('click', () => { _win = b.dataset.win; renderAll(); });
  });
  $('mc-refresh')?.addEventListener('click', async () => {
    const btn = $('mc-refresh');
    if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
    try {
      await jpost(`${API}/refresh`);
      await load();
      toast('Model cost data refreshed', 'success');
    } catch (e) {
      toast(`Refresh failed: ${e.message}`, 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
    }
  });

  paintTabs();
  load();
}

if (document.getElementById('mc-root')) init();
else document.addEventListener('DOMContentLoaded', init);
