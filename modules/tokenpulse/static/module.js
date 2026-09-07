/**
 * TokenPulse - community module front-end.
 *
 * One GET /summary drives the view: quota gauges (loudest first), a provider
 * spend strip, and a per-day monthly spend trend. The Budgets panel writes
 * per-provider monthly budgets (operator). All read-only against providers.
 */

const API = '/api/tokenpulse';

let _data = null;

function af(path, opts) {
  const f = (window.AgeniusDesk && window.AgeniusDesk.fetch) || window.fetch;
  return f(path, opts);
}
async function jget(p) { const r = await af(p); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }
async function jpost(p, body) {
  const r = await af(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body !== undefined ? JSON.stringify(body) : undefined });
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
function pctColor(pct) {
  if (pct == null) return '#60a5fa';
  if (pct >= 90) return '#ff6d5a';
  if (pct >= 75) return '#fbbf24';
  return '#34d399';
}
function resetIn(ts) {
  if (!ts) return '';
  const s = Math.floor(ts - Date.now() / 1000);
  if (s <= 0) return 'resets now';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  if (d) return `resets in ${d}d ${h}h`;
  const m = Math.floor((s % 3600) / 60);
  return h ? `resets in ${h}h ${m}m` : `resets in ${m}m`;
}
function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  return Math.floor(s / 3600) + 'h ago';
}

// ── quota gauges ──────────────────────────────────────────────────────────────

function gauge(q) {
  const color = pctColor(q.pct);
  const pctText = q.pct == null ? usd(q.used) : `${q.pct.toFixed(0)}%`;
  const barW = q.pct == null ? 0 : Math.max(2, Math.min(100, q.pct));
  const sub = q.pct == null
    ? `${usd(q.used)} used`
    : `${usd(q.used)} of ${usd(q.limit)} · ${usd(q.remaining)} left`;
  const reset = q.resets_at ? `<span style="opacity:0.6">${esc(resetIn(q.resets_at))}</span>` : '';
  return `<div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-left:3px solid ${color};border-radius:var(--radius);padding:14px">
    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">
      <strong style="font-size:13px">${esc(q.label)}</strong>
      <span style="font-size:20px;font-weight:700;color:${color}">${esc(pctText)}</span>
    </div>
    <div style="height:6px;background:var(--border-dim);border-radius:3px;overflow:hidden;margin:8px 0 6px">
      <div style="width:${barW}%;height:100%;background:${color}"></div>
    </div>
    <div style="font-size:11px;color:var(--text-secondary);display:flex;justify-content:space-between;gap:8px">
      <span>${esc(sub)}</span>${reset}
    </div>
  </div>`;
}

// ── provider strip ────────────────────────────────────────────────────────────

function providerCard(p) {
  const badge = (t, c) => `<span style="font-size:10px;padding:1px 7px;border-radius:10px;background:${c}22;color:${c};border:1px solid ${c}55">${esc(t)}</span>`;
  let status, body;
  if (!p.configured) {
    status = badge('not configured', '#94a3b8');
    body = `<div style="font-size:12px;opacity:0.6;margin-top:8px">Add its key under Settings ▸ Modules, then grant the endpoint.</div>`;
  } else if (!p.reachable) {
    status = badge('unreachable', '#ff6d5a');
    body = `<div style="font-size:12px;color:#ff6d5a;margin-top:8px">${esc(p.error || 'error')}</div>`;
  } else {
    status = badge('ok', '#34d399');
    const s = p.spend || {};
    const credits = p.credits ? `<div style="font-size:11px;color:var(--text-secondary);margin-top:4px">${usd(p.credits.remaining)} credits left</div>` : '';
    body = `<div style="font-size:22px;font-weight:700;margin-top:6px">${usd(s.mtd || 0)}</div>
            <div style="font-size:11px;color:var(--text-secondary)">this month · ${usd(s.today || 0)} today</div>${credits}`;
  }
  return `<div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:14px">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
      <strong style="font-size:14px">${esc(p.name)}</strong>${status}
    </div>${body}</div>`;
}

// ── trend sparkline ───────────────────────────────────────────────────────────

function renderTrend(trend) {
  const wrap = $('tp-trend-wrap'), el = $('tp-trend');
  if (!wrap || !el) return;
  if (!trend || !trend.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'block';
  const max = Math.max(...trend.map(t => t.amount), 0.0001);
  el.innerHTML = trend.map(t => {
    const h = Math.max(2, Math.round(t.amount / max * 100));
    return `<div title="${esc(t.date)}: ${usd(t.amount)}" style="flex:1;min-width:4px;height:${h}%;background:var(--accent,#60a5fa);border-radius:2px 2px 0 0;opacity:0.85"></div>`;
  }).join('');
}

// ── render ──────────────────────────────────────────────────────────────────

function renderAll() {
  if (!_data) return;
  const providers = _data.providers || [];
  const configured = providers.filter(p => p.configured);
  const un = $('tp-unconfigured');
  if (un) {
    if (!configured.length) {
      un.style.display = 'block';
      un.textContent = 'No providers configured yet. Add an Anthropic, OpenAI, or OpenRouter key under Settings ▸ Modules and grant its endpoint, then Refresh.';
    } else { un.style.display = 'none'; }
  }
  const quotas = _data.quotas || [];
  const qEl = $('tp-quotas');
  if (qEl) {
    qEl.innerHTML = quotas.length
      ? quotas.map(gauge).join('')
      : `<div style="opacity:0.6;font-size:13px">No quotas yet. Set a monthly budget (Budgets, above) or add an OpenRouter key for credit/limit gauges.</div>`;
  }
  const pEl = $('tp-providers');
  if (pEl) pEl.innerHTML = providers.map(providerCard).join('');
  renderTrend(_data.trend);
  const b = _data.budgets || {};
  const set = (id, v) => { const el = $(id); if (el && document.activeElement !== el) el.value = v ? String(v) : ''; };
  set('tp-b-anthropic', b.anthropic); set('tp-b-openai', b.openai); set('tp-b-openrouter', b.openrouter);
  const updated = $('tp-updated');
  if (updated) updated.textContent = _data.generated_at ? `Updated ${ago(_data.generated_at)} · cached up to 5 min` : '';
}

async function load() {
  try {
    _data = await jget(`${API}/summary`);
    renderAll();
  } catch (e) {
    const q = $('tp-quotas');
    if (q) q.innerHTML = `<div style="color:#ff6d5a;font-size:13px">Failed to load: ${esc(e.message)}</div>`;
  }
}

function init() {
  const root = $('tp-root');
  if (!root || root.dataset.mounted) return;
  root.dataset.mounted = '1';

  $('tp-budgets-toggle')?.addEventListener('click', () => {
    const p = $('tp-budgets');
    if (p) p.style.display = p.style.display === 'none' ? 'block' : 'none';
  });
  $('tp-refresh')?.addEventListener('click', async () => {
    const btn = $('tp-refresh');
    if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
    try { await jpost(`${API}/refresh`); await load(); toast('TokenPulse refreshed', 'success'); }
    catch (e) { toast(`Refresh failed: ${e.message}`, 'error'); }
    finally { if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; } }
  });
  $('tp-b-save')?.addEventListener('click', async () => {
    const num = id => { const v = parseFloat($(id)?.value); return isNaN(v) ? 0 : v; };
    const budgets = { anthropic: num('tp-b-anthropic'), openai: num('tp-b-openai'), openrouter: num('tp-b-openrouter') };
    try {
      await jpost(`${API}/settings`, { budgets });
      await load();
      toast('Budgets saved', 'success');
    } catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
  });

  load();
}

if (document.getElementById('tp-root')) init();
else document.addEventListener('DOMContentLoaded', init);
