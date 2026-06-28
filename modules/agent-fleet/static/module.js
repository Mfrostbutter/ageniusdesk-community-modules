/**
 * Agent Fleet - community module front-end.
 *
 * A managed fleet of LangGraph agents. The top is a catalog of registered agents;
 * selecting one scopes the run composer and the run history. Each run is driven
 * by the backend and PERSISTED as an event log; this view POLLS the run detail
 * while a run is running or paused (the sandboxed iframe cannot stream), and the
 * timeline + live graph re-render idempotently from the full event log.
 *
 * Talks to the host only through window.AgeniusDesk.fetch (same-origin /api/ paths,
 * auth + CSRF added by the host). The SVG live-graph renderer and the markdown
 * renderer are inlined so the module is a single self-contained module.js.
 */

const API = '/api/agent-fleet';
const CDN = 'https://esm.sh';

let _agents = [];
let _selectedAgentId = null;
let _runs = [];
let _errors = [];
let _selectedId = null;
let _graphs = {};          // agent_id -> {nodes, edges} topology (cached)
let _serverLive = null;    // server live_run_id: authoritative "is anything running"
let _poll = null;
let _marked = null;
const _expandedCards = new Set();
const _STATUS_RANK = { running: 0, paused: 1, done: 2, error: 2 };

// ── Host fetch + helpers ───────────────────────────────────────────────────────

function af(path, opts) {
  const f = (window.AgeniusDesk && window.AgeniusDesk.fetch) || window.fetch;
  return f(path, opts);
}
async function jget(p) { const r = await af(p); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }
async function jsend(p, method, body) {
  const r = await af(p, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}
const jpost = (p, b) => jsend(p, 'POST', b);
const jdel = (p) => jsend(p, 'DELETE');
function notify(msg, level) { try { window.AgeniusDesk?.notify(String(msg), level || 'info'); } catch { /* noop */ } }

function esc(s) { const el = document.createElement('span'); el.textContent = s == null ? '' : String(s); return el.innerHTML; }
function $(id) { return document.getElementById(id); }
function rank(s) { return _STATUS_RANK[s] ?? 0; }

async function renderMd(text) {
  if (!text) return '<p style="color:var(--text-muted)">(empty)</p>';
  if (!_marked) {
    try { const mod = await import(`${CDN}/marked`); _marked = mod.marked || mod.default; }
    catch { return `<pre style="white-space:pre-wrap;font-family:inherit">${esc(text)}</pre>`; }
  }
  return _marked.parse(text, { breaks: true });
}

function fmtWhen(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso.replace(' ', 'T') + 'Z');
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  } catch { return iso; }
}
function fmtArgs(args) {
  if (!args || !Object.keys(args).length) return '';
  return Object.entries(args).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join(', ');
}
function fmtTokens(n) { if (!n) return ''; return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n); }
function fmtCost(c) { if (!c) return ''; return c < 0.01 ? '<$0.01' : '$' + c.toFixed(c < 1 ? 3 : 2); }
function fmtCost6(c) { return c ? '$' + Number(c).toFixed(6) : '$0'; }

function runTitle(run) {
  if (run.prompt) return run.prompt;
  const agent = _agents.find((a) => a.id === (run.agent_id || _selectedAgentId));
  if (agent && agent.uses_errors === false) return `${agent.name} run`;
  if (run.target && run.target !== 'latest') return `Error ${run.target}`;
  return 'Most recent error';
}

const STATUS = {
  running: { label: 'Running', color: '#38bdf8', spin: true },
  paused: { label: 'Awaiting approval', color: '#f59e0b', spin: false },
  done: { label: 'Done', color: '#34d399', spin: false },
  error: { label: 'Error', color: '#ef4444', spin: false },
};
function statusBadge(status) {
  const s = STATUS[status] || STATUS.running;
  const dot = s.spin
    ? `<span class="lg-spin" style="display:inline-block;width:9px;height:9px;border:2px solid ${s.color};border-right-color:transparent;border-radius:50%"></span>`
    : `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${s.color}"></span>`;
  return `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:${s.color}">${dot}${esc(s.label)}</span>`;
}

const BADGE_COLORS = {
  'human-in-the-loop': '#f59e0b', 'checkpointer': '#f59e0b', 'interrupt': '#f59e0b',
  'parallel': '#a78bfa', 'fan-out': '#a78bfa', 'map-reduce': '#a78bfa',
};
function badgeChip(b) {
  const c = BADGE_COLORS[b] || 'var(--text-muted)';
  return `<span style="font-size:10px;font-weight:700;color:${c};border:1px solid ${c};border-radius:10px;padding:1px 7px;opacity:.9">${esc(b)}</span>`;
}
function modelChip(model) {
  const m = String(model || '');
  let label = m, color = 'var(--text-muted)';
  if (m.includes('haiku')) { label = 'Haiku'; color = '#34d399'; }
  else if (m.includes('sonnet')) { label = 'Sonnet'; color = '#38bdf8'; }
  else if (m.includes('opus')) { label = 'Opus'; color = '#f59e0b'; }
  return `<span title="${esc(m)}" style="font-size:10px;font-weight:700;color:${color};border:1px solid ${color};border-radius:10px;padding:1px 7px">${esc(label)}</span>`;
}

// ── Inlined SVG live-graph renderer ─────────────────────────────────────────────

const NODE_W = 132, NODE_H = 34, ROW_H = 68, COL_W = 152, PAD = 18;
const GC = {
  pendingStroke: '#5a6678', pendingFill: 'rgba(148,163,184,0.08)', pendingText: '#aeb8c7',
  doneStroke: '#34d399', doneFill: 'rgba(52,211,153,0.10)', doneText: '#34d399',
  curStroke: '#38bdf8', curFill: 'rgba(56,189,248,0.16)', curText: '#e5e7eb',
  errStroke: '#ef4444', errFill: 'rgba(239,68,68,0.12)', errText: '#fca5a5',
  edge: '#5a6678', edgeHot: '#38bdf8',
};
function gpretty(id) { if (id === '__start__') return 'START'; if (id === '__end__') return 'END'; return id; }

function computeStates(events, nodeIds) {
  const idSet = new Set(nodeIds);
  const lower = nodeIds.map((n) => n.toLowerCase());
  const findLike = (subs) => nodeIds.find((_, i) => subs.some((s) => lower[i].includes(s))) || null;
  const seq = [];
  const push = (n) => { if (n && idSet.has(n) && seq[seq.length - 1] !== n) seq.push(n); };
  let errored = false;
  for (const ev of events || []) {
    if (!ev) continue;
    const node = ev.node && idSet.has(ev.node) ? ev.node : null;
    switch (ev.phase) {
      case 'started': push('__start__'); break;
      case 'node': push(node || ev.label); break;
      case 'node_light': push(node); break;
      case 'tool_call': push(node || findLike(['tool'])); break;
      case 'tool_result': push(node || findLike(['tool'])); break;
      case 'thinking': push(node || findLike(['investigate', 'triage', 'agent', 'plan'])); break;
      case 'awaiting_approval': push(node || findLike(['review', 'approval', 'approve'])); break;
      case 'resumed': push(node || findLike(['finalize', 'apply', 'stage'])); break;
      case 'final': push(node); push('__end__'); break;
      case 'error': errored = true; if (node) push(node); break;
      default: break;
    }
  }
  const current = seq.length ? seq[seq.length - 1] : null;
  const done = new Set(seq.slice(0, -1));
  return { done, current, errored, reachedEnd: current === '__end__' };
}

function glayout(nodes, edges) {
  const adj = {};
  nodes.forEach((n) => { adj[n] = []; });
  for (const e of edges) if (adj[e.source]) adj[e.source].push(e.target);
  const depth = { __start__: 0 };
  const queue = ['__start__'];
  while (queue.length) {
    const n = queue.shift();
    for (const t of adj[n] || []) { if (depth[t] === undefined) { depth[t] = depth[n] + 1; queue.push(t); } }
  }
  nodes.forEach((n) => { if (depth[n] === undefined) depth[n] = 0; });
  const rows = {};
  for (const n of nodes) { (rows[depth[n]] = rows[depth[n]] || []).push(n); }
  const depths = Object.keys(rows).map(Number).sort((a, b) => a - b);
  const maxRow = Math.max(...depths.map((d) => rows[d].length));
  const width = Math.max(maxRow * COL_W, COL_W) + PAD * 2;
  const height = depths.length * ROW_H + PAD * 2;
  const pos = {};
  for (const d of depths) {
    const row = rows[d];
    row.forEach((n, i) => {
      pos[n] = { x: width / 2 + (i - (row.length - 1) / 2) * COL_W, y: PAD + d * ROW_H + NODE_H / 2 + 6 };
    });
  }
  return { pos, width, height, depth };
}

function gedgePath(s, t, back) {
  const h2 = NODE_H / 2;
  if (!back) {
    const sy = s.y + h2, ty = t.y - h2, my = (sy + ty) / 2;
    return `M ${s.x} ${sy} C ${s.x} ${my}, ${t.x} ${my}, ${t.x} ${ty}`;
  }
  const sx = s.x + NODE_W / 2, tx = t.x + NODE_W / 2, bow = 46;
  return `M ${sx} ${s.y} C ${sx + bow} ${s.y}, ${tx + bow} ${t.y}, ${tx} ${t.y}`;
}

function renderGraphSvg(topology, events) {
  if (!topology || !Array.isArray(topology.nodes) || !topology.nodes.length) return '';
  const { nodes, edges } = topology;
  const { pos, width, height, depth } = glayout(nodes, edges);
  const { done, current, errored } = computeStates(events, nodes);
  const reached = (n) => done.has(n) || n === current;
  const edgeSvg = edges.map((e) => {
    const s = pos[e.source], t = pos[e.target];
    if (!s || !t) return '';
    const back = depth[e.target] <= depth[e.source];
    const hot = reached(e.source) && reached(e.target);
    const color = hot ? GC.edgeHot : GC.edge;
    return `<path d="${gedgePath(s, t, back)}" fill="none" stroke="${color}" stroke-width="${hot ? 2 : 1.2}" marker-end="url(#lg-arrow${hot ? '-hot' : ''})" opacity="${hot ? 0.95 : 0.75}" />`;
  }).join('');
  const nodeSvg = nodes.map((n) => {
    const p = pos[n];
    let stroke = GC.pendingStroke, fill = GC.pendingFill, text = GC.pendingText, cls = '';
    if (n === current) {
      if (errored) { stroke = GC.errStroke; fill = GC.errFill; text = GC.errText; }
      else { stroke = GC.curStroke; fill = GC.curFill; text = GC.curText; cls = 'lg-node-cur'; }
    } else if (done.has(n)) { stroke = GC.doneStroke; fill = GC.doneFill; text = GC.doneText; }
    const term = (n === '__start__' || n === '__end__');
    const w = term ? 78 : NODE_W;
    const x = p.x - w / 2, y = p.y - NODE_H / 2;
    const rx = term ? NODE_H / 2 : 9;
    const checkmark = done.has(n) && !term ? `<text x="${x + w - 12}" y="${p.y + 4}" font-size="12" fill="${GC.doneStroke}">✓</text>` : '';
    return `<g class="${cls}"><rect x="${x}" y="${y}" width="${w}" height="${NODE_H}" rx="${rx}" ry="${rx}" fill="${fill}" stroke="${stroke}" stroke-width="${n === current ? 2 : 1.2}" /><text x="${p.x}" y="${p.y + 4}" text-anchor="middle" font-size="12" font-family="var(--font-mono)" font-weight="${term ? 700 : 600}" fill="${text}">${esc(gpretty(n))}</text>${checkmark}</g>`;
  }).join('');
  return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" style="max-width:100%;height:auto;display:block;margin:0 auto" preserveAspectRatio="xMidYMin meet"><defs><marker id="lg-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="#5a6678" /></marker><marker id="lg-arrow-hot" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="${GC.edgeHot}" /></marker></defs>${edgeSvg}${nodeSvg}</svg>`;
}

// ── Catalog ─────────────────────────────────────────────────────────────────

async function loadAgents() {
  try {
    const res = await jget(`${API}/agents`);
    _agents = res.agents || [];
    if (!_selectedAgentId && _agents.length) _selectedAgentId = res.default || _agents[0].id;
  } catch (e) {
    notify(`Could not load agents: ${e.message}`, 'error');
    _agents = [];
  }
  renderCatalog();
  await selectAgent(_selectedAgentId);
}

function renderCatalog() {
  const cat = $('lg-catalog');
  if (!cat) return;
  if (!_agents.length) { cat.innerHTML = `<div style="color:var(--text-muted);font-size:13px">No agents registered.</div>`; return; }
  cat.innerHTML = _agents.map((a) => {
    const active = a.id === _selectedAgentId;
    const open = _expandedCards.has(a.id);
    const badges = (a.badges || []).map(badgeChip).join(' ');
    return `<div class="lg-agent-card" data-agent="${esc(a.id)}" style="border-color:${active ? 'var(--accent,#60a5fa)' : 'var(--border-dim)'};${active ? 'box-shadow:0 0 0 1px var(--accent,#60a5fa) inset' : ''}">
      <div class="lg-card-head">
        <span style="font-size:13px;font-weight:600;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(a.name)}</span>
        ${a.hitl ? '<span title="Pauses for human approval" style="font-size:10px">⏸️</span>' : ''}
        ${modelChip(a.model)}
        <button class="lg-card-exp" data-exp="${esc(a.id)}" title="${open ? 'Hide details' : 'Show details'}">${open ? '▴' : '▾'}</button>
      </div>
      ${open ? `<div class="lg-card-body">
        <div style="font-size:11.5px;color:var(--text-muted);line-height:1.45;margin-bottom:7px">${esc(a.tagline)}</div>
        <div style="display:flex;gap:5px;flex-wrap:wrap">${badges}</div>
      </div>` : ''}
    </div>`;
  }).join('');
}

async function selectAgent(agentId) {
  if (!agentId) return;
  _selectedAgentId = agentId;
  _selectedId = null;
  renderCatalog();
  const composer = $('lg-composer');
  const agent = _agents.find((a) => a.id === _selectedAgentId) || null;
  if (composer) composer.style.display = agent ? 'flex' : 'none';
  if (agent) {
    const prompt = $('lg-prompt');
    if (prompt) prompt.placeholder = agent.run_hint || 'Optional: free-form request';
    const target = $('lg-target');
    if (target) target.style.display = agent.uses_errors === false ? 'none' : '';
  }
  loadGraph(agentId);
  await loadRuns();
}

async function loadGraph(agentId) {
  if (!agentId || _graphs[agentId]) return;
  try {
    const topo = await jget(`${API}/agents/${encodeURIComponent(agentId)}/graph`);
    if (topo && Array.isArray(topo.nodes)) { _graphs[agentId] = topo; if (_selectedId) renderDetail(); }
  } catch { /* no panel; timeline still renders */ }
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function loadRuns() {
  try {
    const res = await jget(`${API}/runs?agent_id=${encodeURIComponent(_selectedAgentId || '')}`);
    _runs = res.runs || [];
    _serverLive = res.live_run_id || null;
    renderList();
    updateRunButton();
    if (!_selectedId && _runs.length) await selectRun(_runs[0].id);
    else renderDetail();
    if (inflight()) startPolling();
  } catch (e) {
    notify(`Could not load runs: ${e.message}`, 'error');
  }
}

async function loadErrorPicker() {
  const sel = $('lg-target');
  if (!sel) return;
  try {
    const res = await jget('/api/errors?limit=15');
    _errors = res.errors || [];
  } catch { _errors = []; }
  const opts = ['<option value="">Most recent error</option>'];
  for (const err of _errors) {
    const label = `#${err.id} · ${err.workflow_name || err.workflow_id} · ${(err.error_message || '').slice(0, 60)}`;
    opts.push(`<option value="${esc(err.id)}">${esc(label)}</option>`);
  }
  sel.innerHTML = opts.join('');
}

// ── Run lifecycle + polling ─────────────────────────────────────────────────

function inflight() { return !!_serverLive || _runs.some((r) => r.status === 'running'); }

function mergeRun(full) {
  if (!full || !full.id) return;
  const idx = _runs.findIndex((r) => r.id === full.id);
  if (idx < 0) { _runs.unshift(full); return; }
  const local = _runs[idx];
  const merged = { ...local, ...full };
  if (rank(local.status) > rank(full.status)) merged.status = local.status;
  if ((local.events?.length || 0) > (full.events?.length || 0)) merged.events = local.events;
  for (const k of ['triage_md', 'trace_url', 'total_tokens', 'total_cost', 'usage_detail', 'proposal_md']) {
    if ((full[k] == null || full[k] === '') && local[k] != null) merged[k] = local[k];
  }
  _runs[idx] = merged;
}

async function pollTick() {
  if (!$('af-root')) { stopPolling(); return; }
  if (_selectedId) {
    try { mergeRun(await jget(`${API}/runs/${_selectedId}`)); } catch { /* retry next tick */ }
  }
  try {
    const res = await jget(`${API}/runs?agent_id=${encodeURIComponent(_selectedAgentId || '')}`);
    _serverLive = res.live_run_id || null;
    for (const fr of res.runs || []) {
      const idx = _runs.findIndex((r) => r.id === fr.id);
      if (idx >= 0 && rank(fr.status) >= rank(_runs[idx].status)) {
        _runs[idx] = { ..._runs[idx], status: fr.status };
      }
    }
  } catch { /* retry next tick */ }
  renderList();
  updateRunButton();
  if (_selectedId) renderDetail({ stickToBottom: true });
  if (!inflight()) stopPolling();
}

function startPolling() { if (!_poll) _poll = setInterval(pollTick, 1500); }
function stopPolling() { if (_poll) { clearInterval(_poll); _poll = null; } }

function updateRunButton() {
  const btn = $('lg-run');
  if (!btn) return;
  const busy = !!_serverLive;
  btn.disabled = busy;
  btn.textContent = busy ? 'Run in progress…' : 'Run agent';
  if (inflight()) startPolling();
}

async function startRun() {
  if (!_selectedAgentId) { notify('Pick an agent first.', 'warning'); return; }
  const prompt = ($('lg-prompt')?.value || '').trim();
  const targetVal = $('lg-target')?.value || '';
  const body = { agent_id: _selectedAgentId };
  if (prompt) body.prompt = prompt;
  else if (targetVal) body.error_id = parseInt(targetVal, 10);
  const btn = $('lg-run');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting...'; }
  try {
    const res = await jpost(`${API}/triage`, body);
    if (res.run) {
      const dup = _runs.find((r) => r.id === res.run.id);
      if (dup) Object.assign(dup, res.run);
      else _runs.unshift(res.run);
      _serverLive = res.run.id;
      await selectRun(res.run.id);
    }
    startPolling();
    notify('Run started.', 'success');
  } catch (e) {
    notify(e.message, 'error');
  } finally {
    updateRunButton();
  }
}

async function selectRun(id) {
  _selectedId = id;
  renderList();
  try { mergeRun(await jget(`${API}/runs/${id}`)); } catch { /* keep list version */ }
  renderList();
  renderDetail();
  updateRunButton();
}

// ── List pane ─────────────────────────────────────────────────────────────────

function renderList() {
  const list = $('lg-list');
  if (!list) return;
  if (!_runs.length) {
    const agent = _agents.find((a) => a.id === _selectedAgentId);
    list.innerHTML = `<div style="color:var(--text-muted);font-size:13px;padding:20px;text-align:center">No runs yet for ${esc(agent ? agent.name : 'this agent')}. Hit Run agent above.</div>`;
    return;
  }
  list.innerHTML = _runs.map((r) => {
    const active = r.id === _selectedId;
    return `<div class="lg-card" data-run="${esc(r.id)}" style="cursor:pointer;flex-shrink:0;min-width:210px;max-width:248px;background:var(--bg-panel);border:1px solid ${active ? 'var(--accent,#60a5fa)' : 'var(--border-dim)'};border-radius:var(--radius);padding:12px 14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:4px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${esc(runTitle(r))}</div>
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        ${statusBadge(r.status)}
        <span style="font-size:10px;color:var(--text-muted)">${fmtWhen(r.created_at)}</span>
      </div>
    </div>`;
  }).join('');
}

// ── Detail pane ───────────────────────────────────────────────────────────────

function timelineHtml(events) {
  const steps = [];
  for (const ev of events || []) {
    if (ev.phase === 'started') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">▶</span><div style="font-size:13px"><strong>${esc(ev.task || 'Run')}</strong><span style="color:var(--text-muted);font-size:11px"> · ${esc(ev.model || '')}</span></div></div>`);
    } else if (ev.phase === 'thinking') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">💭</span><div style="font-size:12px;color:var(--text-secondary);font-style:italic;white-space:pre-wrap">${esc(ev.text)}</div></div>`);
    } else if (ev.phase === 'node') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">◆</span><div style="font-size:12px"><span style="font-family:var(--font-mono);font-size:11px;color:#a78bfa;font-weight:700">${esc(ev.label)}</span><div style="color:var(--text-secondary);margin-top:2px;white-space:pre-wrap">${esc(ev.text)}</div></div></div>`);
    } else if (ev.phase === 'tool_call') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">→</span><span class="lg-chip">${esc(ev.tool)}(${esc(fmtArgs(ev.args))})</span></div>`);
    } else if (ev.phase === 'tool_result') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">←</span><details><summary>${esc(ev.tool)} result</summary><pre>${esc(ev.preview || '(empty)')}</pre></details></div>`);
    } else if (ev.phase === 'awaiting_approval') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">⏸️</span><div style="font-size:12px;color:#f59e0b;font-weight:600">Paused for human approval (interrupt)</div></div>`);
    } else if (ev.phase === 'resumed') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">▶</span><div style="font-size:12px;color:#38bdf8">Resumed, operator chose <strong>${esc(ev.action || 'approve')}</strong></div></div>`);
    } else if (ev.phase === 'error') {
      steps.push(`<div class="lg-step"><span style="font-size:13px">✕</span><div style="font-size:12px;color:#fca5a5">${esc(ev.message)}</div></div>`);
    }
  }
  return steps.join('');
}

function usageChip(run) {
  if (!run.total_tokens) return '';
  const parts = [`${fmtTokens(run.total_tokens)} tok`];
  const cost = fmtCost(run.total_cost);
  if (cost) parts.push(cost);
  return `<button class="lg-usage" title="Token + per-call breakdown" style="display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;color:#a78bfa;background:transparent;border:1px solid rgba(167,139,250,0.4);border-radius:10px;padding:2px 9px;cursor:pointer">⛁ ${parts.join(' · ')} ▾</button>`;
}

function openUsageModal(run) {
  const d = run.usage_detail || {};
  const inTok = d.input_tokens || 0, outTok = d.output_tokens || 0;
  const totTok = d.total_tokens || run.total_tokens || 0;
  const steps = Array.isArray(d.steps) ? d.steps : [];
  const stat = (label, val, color) =>
    `<div style="flex:1;min-width:90px;background:var(--bg-void);border:1px solid var(--border-dim);border-radius:var(--radius);padding:10px 12px"><div style="font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted)">${label}</div><div style="font-size:18px;font-weight:700;color:${color || 'var(--text-primary)'};margin-top:2px">${val}</div></div>`;
  const rows = steps.map((s, i) => `<tr style="border-top:1px solid var(--border-dim)"><td style="padding:6px 8px;color:var(--text-muted)">${i + 1}</td><td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px">${esc(s.name || 'llm')}</td><td style="padding:6px 8px;text-align:right">${(s.input || 0).toLocaleString()}</td><td style="padding:6px 8px;text-align:right">${(s.output || 0).toLocaleString()}</td><td style="padding:6px 8px;text-align:right;font-weight:600">${(s.total || 0).toLocaleString()}</td><td style="padding:6px 8px;text-align:right;color:#a78bfa">${fmtCost6(s.cost)}</td></tr>`).join('');
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9999;display:flex;align-items:center;justify-content:center;padding:24px';
  overlay.innerHTML = `<div style="background-color:var(--bg-void);background-image:linear-gradient(var(--bg-panel),var(--bg-panel));border:1px solid var(--border-dim);border-radius:var(--radius);max-width:680px;width:100%;max-height:82vh;overflow-y:auto;padding:20px 22px;box-shadow:0 20px 60px rgba(0,0,0,0.6)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px"><div style="font-size:16px;font-weight:700">Token &amp; cost breakdown</div><button class="lg-usage-close" style="background:var(--bg-void);border:1px solid var(--border-dim);border-radius:var(--radius);color:var(--text-muted);cursor:pointer;padding:4px 10px;font-size:13px">Close</button></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">${stat('Input', inTok.toLocaleString() + ' tok', '#38bdf8')}${stat('Output', outTok.toLocaleString() + ' tok', '#34d399')}${stat('Total', totTok.toLocaleString() + ' tok')}${stat('Cost', fmtCost6(run.total_cost), '#a78bfa')}</div>
      <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin:10px 0 6px">Per model call through the run (${steps.length})</div>
      ${steps.length ? `<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="color:var(--text-muted);font-size:10px;text-transform:uppercase"><th style="padding:4px 8px;text-align:left">#</th><th style="padding:4px 8px;text-align:left">Call</th><th style="padding:4px 8px;text-align:right">Input</th><th style="padding:4px 8px;text-align:right">Output</th><th style="padding:4px 8px;text-align:right">Total</th><th style="padding:4px 8px;text-align:right">Cost</th></tr></thead><tbody>${rows}</tbody></table>` : `<div style="color:var(--text-muted);font-size:12px;padding:8px 0">Per-call detail not available.</div>`}
      ${run.trace_url ? `<div style="font-size:11px;color:var(--text-muted);margin-top:12px"><a href="${esc(run.trace_url)}" target="_blank" style="color:var(--accent,#60a5fa)">Open full trace in LangSmith ↗</a></div>` : ''}
    </div>`;
  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('.lg-usage-close').addEventListener('click', close);
  document.body.appendChild(overlay);
}

async function submitResume(runId, action, edited, mode, choice) {
  const run = _runs.find((r) => r.id === runId);
  if (run) run.status = 'running';
  _serverLive = runId;
  renderList(); renderDetail(); updateRunButton();
  try {
    await jpost(`${API}/runs/${runId}/resume`, { action, edited, mode, choice });
    startPolling();
    notify(action === 'reject' ? 'Rejected.' : 'Approved, resuming.', 'success');
  } catch (e) {
    if (run) run.status = 'paused';
    renderList(); renderDetail();
    notify(e.message, 'error');
  }
}

function wireApproval(pane, run, proposalMd) {
  const editBox = pane.querySelector('#lg-edit');
  const toggle = pane.querySelector('.lg-edit-toggle');
  const approve = pane.querySelector('.lg-approve');
  const reject = pane.querySelector('.lg-reject');
  if (toggle) toggle.addEventListener('click', () => {
    const editing = editBox.style.display !== 'none';
    if (editing) { editBox.style.display = 'none'; approve.textContent = 'Approve'; }
    else { editBox.value = proposalMd || ''; editBox.style.display = 'block'; editBox.focus(); approve.textContent = 'Approve edited'; }
  });
  if (approve) approve.addEventListener('click', () => {
    const editing = editBox && editBox.style.display !== 'none';
    const edited = editing ? editBox.value.trim() : '';
    submitResume(run.id, edited ? 'edit' : 'approve', edited);
  });
  if (reject) reject.addEventListener('click', () => submitResume(run.id, 'reject', ''));
}

async function renderDetail({ stickToBottom = false } = {}) {
  const pane = $('lg-detail');
  if (!pane) return;
  const run = _runs.find((r) => r.id === _selectedId);
  if (!run) {
    const agent = _agents.find((a) => a.id === _selectedAgentId);
    pane.innerHTML = agent
      ? `<div style="color:var(--text-muted);font-size:13px"><div style="font-size:15px;font-weight:700;color:var(--text-primary);margin-bottom:6px">${esc(agent.name)}</div>${esc(agent.description)}<div style="margin-top:12px">Kick off a run above, or select a past run.</div></div>`
      : `<div style="color:var(--text-muted);font-size:13px">Select an agent.</div>`;
    return;
  }

  const header = `<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:10px">
      <div style="min-width:0"><div style="font-size:16px;font-weight:700;line-height:1.3">${esc(runTitle(run))}</div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:3px">${statusBadge(run.status)}${run.model ? ' · ' + esc(run.model) : ''}${run.created_at ? ' · ' + fmtWhen(run.created_at) : ''}</div></div>
      <div style="display:flex;gap:6px;flex-shrink:0;align-items:center">${usageChip(run)}${run.trace_url ? `<a href="${esc(run.trace_url)}" target="_blank" style="padding:7px 12px;background:rgba(52,211,153,0.12);color:#34d399;border:1px solid rgba(52,211,153,0.4);border-radius:var(--radius);font-size:12px;font-weight:700;text-decoration:none">View trace in LangSmith ↗</a>` : ''}<button class="lg-del" style="padding:7px 10px;background:var(--bg-void);border:1px solid var(--border-dim);border-radius:var(--radius);font-size:12px;color:var(--text-muted);cursor:pointer">Delete</button></div></div>`;

  const events = Array.isArray(run.events) ? run.events : [];
  const timeline = timelineHtml(events);
  const isPaused = run.status === 'paused';

  const topo = _graphs[run.agent_id || _selectedAgentId];
  const graphPanel = topo ? `<div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Live graph</div>
    <div id="lg-graph" style="border:1px solid var(--border-dim);border-radius:var(--radius);background:var(--bg-void);padding:14px 12px;max-height:520px;overflow:auto">${renderGraphSvg(topo, events)}</div>` : '';

  let proposalMd = run.proposal_md || '';
  if (!proposalMd) { for (const ev of events) if (ev.phase === 'awaiting_approval' && ev.proposal_md) proposalMd = ev.proposal_md; }
  if (!proposalMd && isPaused) proposalMd = run.triage_md || '';

  const triageMd = (!isPaused && run.triage_md) ? run.triage_md : '';
  const triageHtml = triageMd ? await renderMd(triageMd) : '';
  const proposalHtml = (isPaused && proposalMd) ? await renderMd(proposalMd) : '';

  const timelineCol = `<div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Investigation timeline</div>
    <div id="lg-timeline" style="border:1px solid var(--border-dim);border-radius:var(--radius);background:var(--bg-void);padding:6px 14px;max-height:520px;overflow-y:auto">${timeline || '<div style="color:var(--text-muted);font-size:12px;padding:8px 0">Waiting for the first event…</div>'}</div>`;
  const topRow = graphPanel
    ? `<div style="display:grid;grid-template-columns:minmax(340px,1fr) minmax(0,1fr);gap:16px;align-items:start;margin-bottom:14px"><div style="min-width:0">${graphPanel}</div><div style="min-width:0">${timelineCol}</div></div>`
    : `<div style="margin-bottom:14px">${timelineCol}</div>`;

  pane.innerHTML = `${header}${topRow}
    ${run.status === 'error' && run.error ? `<div style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:var(--radius);padding:12px;color:#fca5a5;font-size:13px;margin-top:12px"><strong>Failed:</strong> ${esc(run.error)}</div>` : ''}
    ${isPaused ? `<div style="border:1px solid rgba(245,158,11,0.45);border-radius:var(--radius);background:rgba(245,158,11,0.06);padding:14px;margin-top:14px">
        <div style="font-size:11px;font-weight:700;color:#f59e0b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">⏸️ Awaiting your approval</div>
        <div id="lg-proposal" style="font-size:14px;line-height:1.6">${proposalHtml || '<em style="color:var(--text-muted)">No proposal text.</em>'}</div>
        <textarea id="lg-edit" placeholder="Edit the fix, then Approve edited…" style="display:none;width:100%;box-sizing:border-box;margin-top:10px;min-height:120px;padding:10px;background:var(--bg-void);border:1px solid var(--border-dim);border-radius:var(--radius);color:var(--text-primary);font-size:13px;font-family:var(--font-mono)"></textarea>
        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
          <button class="lg-approve" style="padding:8px 16px;background:#34d399;color:#04231a;border:none;border-radius:var(--radius);font-size:13px;font-weight:700;cursor:pointer">Approve</button>
          <button class="lg-edit-toggle" style="padding:8px 14px;background:var(--bg-void);border:1px solid var(--border-dim);border-radius:var(--radius);font-size:13px;color:var(--text-primary);cursor:pointer">Edit</button>
          <button class="lg-reject" style="padding:8px 14px;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.4);border-radius:var(--radius);font-size:13px;color:#fca5a5;cursor:pointer">Reject</button>
        </div></div>` : ''}
    ${triageHtml ? `<div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;margin:14px 0 4px">Result</div><div id="lg-triage" style="font-size:14px;line-height:1.6">${triageHtml}</div>` : ''}`;

  if (stickToBottom) { const tl = pane.querySelector('#lg-timeline'); if (tl) tl.scrollTop = tl.scrollHeight; }
  if (isPaused) wireApproval(pane, run, proposalMd);
  const usageBtn = pane.querySelector('.lg-usage');
  if (usageBtn) usageBtn.addEventListener('click', () => openUsageModal(run));
  const delBtn = pane.querySelector('.lg-del');
  if (delBtn) delBtn.addEventListener('click', async () => {
    if (!window.confirm('Delete this run?')) return;
    try { await jdel(`${API}/runs/${run.id}`); _runs = _runs.filter((r) => r.id !== run.id); _selectedId = _runs[0]?.id || null; renderList(); renderDetail(); }
    catch (e) { notify(e.message, 'error'); }
  });
}

// ── Wiring + mount ──────────────────────────────────────────────────────────

document.addEventListener('click', (e) => {
  const t = e.target;
  if (!(t instanceof Element) || !$('af-root')) return;
  if (t.closest('#lg-run')) { startRun(); return; }
  const exp = t.closest('.lg-card-exp');
  if (exp) { const id = exp.dataset.exp; if (_expandedCards.has(id)) _expandedCards.delete(id); else _expandedCards.add(id); renderCatalog(); return; }
  const card = t.closest('.lg-agent-card');
  if (card) { selectAgent(card.dataset.agent); return; }
  const runCard = t.closest('.lg-card');
  if (runCard) { selectRun(runCard.dataset.run); return; }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target instanceof Element && e.target.id === 'lg-prompt') { e.preventDefault(); startRun(); }
});

async function mount() {
  await Promise.all([loadAgents(), loadErrorPicker()]);
}

if ($('af-root')) mount();
else document.addEventListener('DOMContentLoaded', mount);
