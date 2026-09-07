/**
 * TokenPulse - consolidated cost + usage front-end.
 *
 * One GET /summary drives everything: an overview (spend totals, quota gauges,
 * provider tiles, monthly trend) and a per-provider drill-down (spend windows,
 * that provider's quotas, its per-model cost table, its daily trend). Click a
 * provider tile to drill in. Budgets writes per-provider monthly budgets.
 */

const API = '/api/tokenpulse';

let _data = null;
let _detail = null;      // provider id currently drilled into, or null
let _modelWin = {};      // per-provider selected model-table window

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

function usd(n) { return '$' + (Number(n) || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function tok(n) {
  const v = Number(n) || 0;
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
  return String(Math.round(v));
}
function shortModel(name) { let n = String(name || ''); if (n.includes('/')) n = n.split('/', 2)[1]; return n; }
function pctColor(p) { if (p == null) return '#60a5fa'; if (p >= 90) return '#ff6d5a'; if (p >= 75) return '#fbbf24'; return '#34d399'; }
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
const WIN_LABEL = { today: 'Today', week: 'Week', mtd: 'Month', '30d': '30 days' };

// ── shared pieces ─────────────────────────────────────────────────────────────

function gauge(q) {
  const color = pctColor(q.pct);
  const pctText = q.pct == null ? usd(q.used) : `${q.pct.toFixed(0)}%`;
  const barW = q.pct == null ? 0 : Math.max(2, Math.min(100, q.pct));
  const sub = q.pct == null ? `${usd(q.used)} used` : `${usd(q.used)} of ${usd(q.limit)} · ${usd(q.remaining)} left`;
  const reset = q.resets_at ? `<span style="opacity:0.6">${esc(resetIn(q.resets_at))}</span>` : '';
  return `<div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-left:3px solid ${color};border-radius:var(--radius);padding:14px">
    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">
      <strong style="font-size:13px">${esc(q.label)}</strong>
      <span style="font-size:20px;font-weight:700;color:${color}">${esc(pctText)}</span>
    </div>
    <div style="height:6px;background:var(--border-dim);border-radius:3px;overflow:hidden;margin:8px 0 6px"><div style="width:${barW}%;height:100%;background:${color}"></div></div>
    <div style="font-size:11px;color:var(--text-secondary);display:flex;justify-content:space-between;gap:8px"><span>${esc(sub)}</span>${reset}</div>
  </div>`;
}

function basisBadge(b) {
  if (b === 'actual') return `<span style="font-size:10px;color:#34d399">actual</span>`;
  if (b === 'unpriced') return `<span style="font-size:10px;color:#94a3b8" title="no price on file">unpriced</span>`;
  return `<span style="font-size:10px;color:var(--text-secondary)">est</span>`;
}

function modelTable(models, win) {
  const rows = models.filter(m => m.window === win).sort((a, b) => (b.cost || 0) - (a.cost || 0));
  if (!rows.length) return `<div style="opacity:0.6;font-size:13px;padding:8px 0">No per-model data for this window.</div>`;
  const total = rows.reduce((a, m) => a + (m.cost || 0), 0);
  const body = rows.map(m => `<tr style="border-top:1px solid var(--border-dim)">
    <td style="padding:7px 8px;font-size:13px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(m.model)}">${esc(shortModel(m.model))}</td>
    <td style="padding:7px 8px;font-size:12px;text-align:right;color:var(--text-secondary)">${tok(m.input_tokens)} / ${tok(m.output_tokens)}</td>
    <td style="padding:7px 8px;font-size:12px;text-align:right">${tok(m.tokens)}</td>
    <td style="padding:7px 8px;font-size:13px;text-align:right;font-weight:600">${usd(m.cost)} ${basisBadge(m.cost_basis)}</td>
  </tr>`).join('');
  return `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">
    <thead><tr style="text-align:left;color:var(--text-secondary);font-size:11px;text-transform:uppercase;letter-spacing:0.5px">
      <th style="padding:6px 8px">Model</th><th style="padding:6px 8px;text-align:right">Tokens (in/out)</th>
      <th style="padding:6px 8px;text-align:right">Total</th><th style="padding:6px 8px;text-align:right">Cost</th></tr></thead>
    <tbody>${body}</tbody>
    <tfoot><tr style="border-top:2px solid var(--border-dim)"><td colspan="3" style="padding:8px;font-size:12px;color:var(--text-secondary)">${rows.length} model${rows.length === 1 ? '' : 's'}</td>
      <td style="padding:8px;text-align:right;font-weight:700">${usd(total)}</td></tr></tfoot>
  </table></div>`;
}

function trendBars(trend) {
  if (!trend || !trend.length) return '';
  const max = Math.max(...trend.map(t => t.amount), 0.0001);
  return `<div style="display:flex;align-items:flex-end;gap:3px;height:70px;background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:10px">` +
    trend.map(t => `<div title="${esc(t.date)}: ${usd(t.amount)}" style="flex:1;min-width:4px;height:${Math.max(2, Math.round(t.amount / max * 100))}%;background:var(--accent,#60a5fa);border-radius:2px 2px 0 0;opacity:0.85"></div>`).join('') +
    `</div>`;
}

// ── overview ──────────────────────────────────────────────────────────────────

function providerTile(p) {
  const badge = (t, c) => `<span style="font-size:10px;padding:1px 7px;border-radius:10px;background:${c}22;color:${c};border:1px solid ${c}55">${esc(t)}</span>`;
  let status, body, clickable = false;
  if (!p.configured) {
    status = badge('not configured', '#94a3b8');
    body = `<div style="font-size:12px;opacity:0.6;margin-top:8px">Add its key under Settings ▸ Modules, then grant the endpoint.</div>`;
  } else if (!p.reachable) {
    status = badge('unreachable', '#ff6d5a');
    body = `<div style="font-size:12px;color:#ff6d5a;margin-top:8px">${esc(p.error || 'error')}</div>`;
  } else {
    status = badge('ok', '#34d399');
    clickable = true;
    const s = p.spend || {};
    const credits = p.credits ? `<div style="font-size:11px;color:var(--text-secondary);margin-top:4px">${usd(p.credits.remaining)} credits left</div>` : '';
    const nModels = (p.models || []).length;
    body = `<div style="font-size:22px;font-weight:700;margin-top:6px">${usd(s.mtd || 0)}</div>
            <div style="font-size:11px;color:var(--text-secondary)">this month · ${usd(s.today || 0)} today${nModels ? ` · ${nModels} models` : ''}</div>${credits}
            <div style="font-size:11px;color:var(--accent,#60a5fa);margin-top:8px">view detail →</div>`;
  }
  return `<div ${clickable ? `data-provider="${esc(p.id)}" role="button" tabindex="0"` : ''} style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:14px;${clickable ? 'cursor:pointer' : ''}">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><strong style="font-size:14px">${esc(p.name)}</strong>${status}</div>${body}</div>`;
}

function renderOverview() {
  const providers = _data.providers || [];
  const configured = providers.filter(p => p.configured);
  const un = $('tp-unconfigured');
  if (un) {
    if (!configured.length) { un.style.display = 'block'; un.textContent = 'No providers configured yet. Add an Anthropic, OpenAI, or OpenRouter key under Settings ▸ Modules and grant its endpoint, then Refresh.'; }
    else un.style.display = 'none';
  }
  const sum = $('tp-summary');
  if (sum) {
    const tile = (label, v) => `<div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:12px 14px;text-align:center"><div style="font-size:22px;font-weight:700">${v}</div><div style="font-size:11px;color:var(--text-secondary)">${esc(label)}</div></div>`;
    const today = providers.reduce((a, p) => a + ((p.spend || {}).today || 0), 0);
    const mtd = providers.reduce((a, p) => a + ((p.spend || {}).mtd || 0), 0);
    const or = providers.find(p => p.id === 'openrouter' && (p.spend || {})['30d'] != null);
    sum.innerHTML = tile('Today', usd(today)) + tile('Month to date', usd(mtd)) + (or ? tile('OpenRouter 30d', usd(or.spend['30d'])) : '');
  }
  const quotas = _data.quotas || [];
  const qSec = $('tp-quotas-section'), qEl = $('tp-quotas');
  if (qEl) qEl.innerHTML = quotas.map(gauge).join('');
  if (qSec) qSec.style.display = quotas.length ? 'block' : 'none';
  const pEl = $('tp-providers');
  if (pEl) pEl.innerHTML = providers.map(providerTile).join('');
  const tw = $('tp-trend-wrap');
  if (tw) { tw.style.display = (_data.trend || []).length ? 'block' : 'none'; const t = $('tp-trend'); if (t && _data.trend) { const inner = trendBars(_data.trend); t.outerHTML = `<div id="tp-trend">${inner ? inner : ''}</div>`; } }
}

// ── provider detail (drill-down) ──────────────────────────────────────────────

function renderDetail(pid) {
  const p = (_data.providers || []).find(x => x.id === pid);
  const el = $('tp-detail');
  if (!p || !el) return;
  const s = p.spend || {};
  const spendCells = ['today', 'week', 'mtd', '30d'].filter(w => s[w] != null).map(w =>
    `<div style="background:var(--bg-panel);border:1px solid var(--border-dim);border-radius:var(--radius);padding:10px 12px;text-align:center;min-width:90px">
      <div style="font-size:18px;font-weight:700">${usd(s[w])}</div><div style="font-size:11px;color:var(--text-secondary)">${esc(WIN_LABEL[w] || w)}</div></div>`).join('');

  const wins = [...new Set((p.models || []).map(m => m.window))];
  const order = ['mtd', 'today', '30d'];
  wins.sort((a, b) => order.indexOf(a) - order.indexOf(b));
  const cur = _modelWin[pid] && wins.includes(_modelWin[pid]) ? _modelWin[pid] : (wins[0] || 'mtd');
  _modelWin[pid] = cur;
  const tabs = wins.length > 1 ? `<div style="display:flex;gap:6px;margin-bottom:10px">` + wins.map(w =>
    `<button class="tp-mwin" data-provider="${esc(pid)}" data-win="${esc(w)}" type="button" style="background:${w === cur ? 'var(--accent,#60a5fa)' : 'var(--bg-panel)'};color:${w === cur ? '#fff' : 'var(--text-secondary)'};border:1px solid var(--border-dim);border-radius:var(--radius);font-size:12px;padding:4px 10px;cursor:pointer">${esc(WIN_LABEL[w] || w)}</button>`).join('') + `</div>` : '';

  const quotas = (p.quotas || []).map(gauge).join('');
  const err = !p.reachable ? `<div style="color:#ff6d5a;font-size:13px;margin-bottom:12px">${esc(p.error || 'unreachable')}</div>` : '';

  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
      <button id="tp-back" type="button" class="btn" style="cursor:pointer;font-size:12px;padding:6px 12px;border:1px solid var(--border-dim);border-radius:var(--radius);background:var(--bg-panel);color:var(--text-secondary)">← Back</button>
      <h2 style="margin:0;font-size:17px">${esc(p.name)}${p.label ? ` <span style="font-size:12px;opacity:0.5;font-weight:400">${esc(p.label)}</span>` : ''}</h2>
    </div>
    ${err}
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px">${spendCells || '<div style="opacity:0.6;font-size:13px">No spend windows reported.</div>'}</div>
    ${quotas ? `<div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.5;margin-bottom:8px">Quotas</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-bottom:20px">${quotas}</div>` : ''}
    <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.5;margin-bottom:8px">Cost by model</div>
    ${tabs}
    <div id="tp-detail-models">${modelTable(p.models || [], cur)}</div>
    ${(p.trend || []).length ? `<div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;opacity:0.5;margin:18px 0 8px">Spend this month, per day</div>${trendBars(p.trend)}` : ''}
  `;
  $('tp-back')?.addEventListener('click', () => showOverview());
  el.querySelectorAll('.tp-mwin').forEach(b => b.addEventListener('click', () => {
    _modelWin[pid] = b.dataset.win;
    $('tp-detail-models').innerHTML = modelTable(p.models || [], b.dataset.win);
    renderDetail(pid); // repaint tabs active state
  }));
}

function showDetail(pid) { _detail = pid; $('tp-overview').style.display = 'none'; $('tp-detail').style.display = 'block'; renderDetail(pid); }
function showOverview() { _detail = null; $('tp-detail').style.display = 'none'; $('tp-overview').style.display = 'block'; }

// ── render + load ─────────────────────────────────────────────────────────────

function renderAll() {
  if (!_data) return;
  renderOverview();
  if (_detail && (_data.providers || []).some(p => p.id === _detail && p.reachable)) renderDetail(_detail);
  else if (_detail) showOverview();
  const b = _data.budgets || {};
  const set = (id, v) => { const el = $(id); if (el && document.activeElement !== el) el.value = v ? String(v) : ''; };
  set('tp-b-anthropic', b.anthropic); set('tp-b-openai', b.openai); set('tp-b-openrouter', b.openrouter);
  const u = $('tp-updated'); if (u) u.textContent = _data.generated_at ? `Updated ${ago(_data.generated_at)} · cached up to 5 min` : '';
}

async function load() {
  try { _data = await jget(`${API}/summary`); renderAll(); }
  catch (e) { const el = $('tp-providers'); if (el) el.innerHTML = `<div style="color:#ff6d5a;font-size:13px">Failed to load: ${esc(e.message)}</div>`; }
}

function init() {
  const root = $('tp-root');
  if (!root || root.dataset.mounted) return;
  root.dataset.mounted = '1';

  // Delegated tile click -> drill-down.
  $('tp-providers')?.addEventListener('click', e => {
    const tile = e.target.closest('[data-provider]');
    if (tile) showDetail(tile.dataset.provider);
  });
  $('tp-providers')?.addEventListener('keydown', e => {
    const tile = e.target.closest('[data-provider]');
    if (tile && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); showDetail(tile.dataset.provider); }
  });

  $('tp-budgets-toggle')?.addEventListener('click', () => { const p = $('tp-budgets'); if (p) p.style.display = p.style.display === 'none' ? 'block' : 'none'; });
  $('tp-refresh')?.addEventListener('click', async () => {
    const btn = $('tp-refresh'); if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
    try { await jpost(`${API}/refresh`); await load(); toast('TokenPulse refreshed', 'success'); }
    catch (e) { toast(`Refresh failed: ${e.message}`, 'error'); }
    finally { if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; } }
  });
  $('tp-b-save')?.addEventListener('click', async () => {
    const num = id => { const v = parseFloat($(id)?.value); return isNaN(v) ? 0 : v; };
    try { await jpost(`${API}/settings`, { budgets: { anthropic: num('tp-b-anthropic'), openai: num('tp-b-openai'), openrouter: num('tp-b-openrouter') } }); await load(); toast('Budgets saved', 'success'); }
    catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
  });

  load();
}

if (document.getElementById('tp-root')) init();
else document.addEventListener('DOMContentLoaded', init);
