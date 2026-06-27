/**
 * YouTube Research - community module front-end.
 *
 * Mirrors the main app's Research view (list + detail, single/deep, tabs,
 * markdown, deep dive, download, delete, move, destination picker) but runs as a
 * community view: AgeniusDesk injects research.html and loads this script ONCE,
 * so behavior is bound via document-level delegation and a MutationObserver
 * (re)mounts when the view appears. No WebSocket; in-flight jobs are polled.
 */

const API = '/api/youtube-research';
const CDN = 'https://esm.sh';

let _jobs = [];
let _selectedId = null;
let _detailTab = 'breakdown';
let _savedProvider = '';
let _marked = null;
let _poll = null;

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

function esc(s) { const el = document.createElement('span'); el.textContent = s == null ? '' : String(s); return el.innerHTML; }
function $(id) { return document.getElementById(id); }

async function renderMd(text) {
  if (!text) return '<p style="color:var(--text-muted)">(empty)</p>';
  if (!_marked) {
    try { const mod = await import(`${CDN}/marked`); _marked = mod.marked || mod.default; }
    catch { return `<pre style="white-space:pre-wrap;font-family:inherit">${esc(text)}</pre>`; }
  }
  return _marked.parse(text, { breaks: true });
}

const STATUS = {
  queued: { label: 'Queued', color: '#94a3b8', spin: false },
  transcribing: { label: 'Transcribing', color: '#38bdf8', spin: true },
  analyzing: { label: 'Analyzing', color: '#a78bfa', spin: true },
  deepdiving: { label: 'Deep dive', color: '#a78bfa', spin: true },
  done: { label: 'Done', color: '#34d399', spin: false },
  error: { label: 'Error', color: '#ef4444', spin: false },
};
function statusBadge(status, progress) {
  const s = STATUS[status] || STATUS.queued;
  const dot = s.spin
    ? `<span class="ytr-spin" style="display:inline-block;width:9px;height:9px;border:2px solid ${s.color};border-right-color:transparent;border-radius:50%"></span>`
    : `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${s.color}"></span>`;
  return `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:${s.color}">${dot}${esc(s.label)}${progress && s.spin ? `<span style="color:var(--text-muted);font-weight:400">· ${esc(progress)}</span>` : ''}</span>`;
}
function fmtDuration(secs) { if (!secs) return ''; const m = Math.floor(secs / 60), s = secs % 60; return `${m}:${String(s).padStart(2, '0')}`; }
function fmtWhen(iso) { if (!iso) return ''; try { return new Date(iso.replace(' ', 'T') + 'Z').toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); } catch { return iso; } }

function inflight() { return _jobs.some(j => ['queued', 'transcribing', 'analyzing', 'deepdiving'].includes(j.status)); }

// ── Model picker ─────────────────────────────────────────────────────────────

function activeProvider() { return ($('ytr-provider')?.value || '') || _savedProvider; }
function encodeModel() {
  const pv = $('ytr-provider')?.value || '';
  const mv = $('ytr-model')?.value || '';
  if (!pv) return mv;
  return `${pv}::${mv}`;
}
async function loadModels() {
  const sel = $('ytr-model');
  if (!sel) return;
  sel.innerHTML = '<option value="">Loading…</option>';
  let models = [];
  try { models = (await jget(`/api/assistant/models?provider=${encodeURIComponent(activeProvider())}`)).models || []; } catch { /* keep default */ }
  const opts = ['<option value="">Default model</option>'];
  for (const m of models) {
    const id = typeof m === 'string' ? m : (m.id || m.name || '');
    const label = typeof m === 'string' ? m : (m.label || m.name || m.id || '');
    if (id) opts.push(`<option value="${esc(id)}">${esc(label)}</option>`);
  }
  sel.innerHTML = opts.join('');
}
async function initProviderModel() {
  try { _savedProvider = (await jget('/api/assistant/config')).provider || 'anthropic'; } catch { _savedProvider = 'anthropic'; }
  await loadModels();
}
async function loadDestinations() {
  const dl = $('ytr-dest-list');
  if (!dl) return;
  try {
    const res = await jget(`${API}/folders`);
    dl.innerHTML = (res.folders || []).map(f => `<option value="${esc(f.path)}">`).join('');
  } catch { /* leave empty */ }
}

// ── Jobs ─────────────────────────────────────────────────────────────────────

async function loadJobs() {
  try {
    _jobs = (await jget(`${API}/jobs`)).jobs || [];
    renderList();
    if (!_selectedId && _jobs.length) await selectJob(_jobs[0].id);
    else await renderDetail();
  } catch { /* ignore */ }
}

function renderList() {
  const list = $('ytr-list');
  if (!list) return;
  if (!_jobs.length) { list.innerHTML = `<div style="color:var(--text-muted);font-size:13px;padding:20px;text-align:center">No research yet. Paste a link above.</div>`; return; }
  list.innerHTML = _jobs.map(j => {
    const active = j.id === _selectedId;
    return `<div class="ytr-card" data-id="${esc(j.id)}" style="cursor:pointer;background:var(--bg-panel);border:1px solid ${active ? 'var(--accent,#60a5fa)' : 'var(--border-dim)'};border-radius:var(--radius);padding:12px 14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:4px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${esc(j.title || j.url)}</div>
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        ${statusBadge(j.status, j.progress)}
        <span style="font-size:11px;color:var(--text-muted)">${esc(j.channel || '')}${j.duration_seconds ? ' · ' + fmtDuration(j.duration_seconds) : ''}</span>
      </div>
      <div style="font-size:10px;color:var(--text-muted);margin-top:4px">${fmtWhen(j.created_at)}${j.depth === 'deep' ? ' · deep' : ''}</div>
    </div>`;
  }).join('');
}

async function selectJob(id) {
  _selectedId = id;
  _detailTab = 'breakdown';
  renderList();
  try {
    const full = await jget(`${API}/jobs/${id}`);
    const idx = _jobs.findIndex(j => j.id === id);
    if (idx >= 0) _jobs[idx] = full; else _jobs.unshift(full);
  } catch { /* keep list version */ }
  await renderDetail();
}

function detailHeader(job) {
  const canDeep = job.status === 'done' && job.breakdown_md && !job.deepdive_md;
  const savedTo = job.artifact_dir ? (job.destination ? job.destination : '_inbox') : '';
  return `<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
    <div style="min-width:0">
      <div style="font-size:16px;font-weight:700;line-height:1.3">${esc(job.title || job.url)}</div>
      <div style="font-size:12px;color:var(--text-muted);margin-top:3px">
        ${esc(job.channel || '')}${job.duration_seconds ? ' · ' + fmtDuration(job.duration_seconds) : ''}
        ${job.engine_used ? ' · ' + esc(job.engine_used) : ''}
        ${savedTo ? ' · saved to research/' + esc(savedTo) : ''}
      </div>
      ${job.url ? `<a href="${esc(job.url)}" target="_blank" style="font-size:11px;color:var(--accent,#60a5fa)">open on YouTube</a>` : ''}
    </div>
    <div style="display:flex;gap:6px;flex-shrink:0">
      ${canDeep ? `<button id="ytr-deepdive" class="btn btn-sm" style="color:#a78bfa;border-color:#a78bfa55">Run deep dive</button>` : ''}
      ${job.artifact_dir ? `<button class="ytr-move btn btn-sm">Move</button>` : ''}
      ${job.breakdown_md ? `<button class="ytr-dl btn btn-sm" data-kind="breakdown">Download</button>` : ''}
      <button class="ytr-del btn btn-sm" style="color:var(--text-muted)">Delete</button>
    </div>
  </div>`;
}

async function renderDetail() {
  const pane = $('ytr-detail');
  if (!pane) return;
  const job = _jobs.find(j => j.id === _selectedId);
  if (!job) { pane.innerHTML = `<div style="color:var(--text-muted);font-size:13px">Select a research run.</div>`; return; }

  if (job.status === 'error') {
    pane.innerHTML = `${detailHeader(job)}<div style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:var(--radius);padding:14px;color:#fca5a5;font-size:13px;margin-top:12px"><strong>Failed:</strong> ${esc(job.error || 'Unknown error')}</div>`;
    return;
  }
  if (!job.breakdown_md && job.status !== 'done') {
    pane.innerHTML = `${detailHeader(job)}<div style="display:flex;align-items:center;gap:10px;color:var(--text-muted);font-size:13px;margin-top:20px">${statusBadge(job.status, job.progress)}</div><p style="color:var(--text-muted);font-size:12px;margin-top:10px">This updates live; longer videos take a bit.</p>`;
    return;
  }

  const tabs = [['breakdown', 'Breakdown', !!job.breakdown_md], ['deep', 'Deep dive', !!job.deepdive_md], ['transcript', 'Transcript', !!job.transcript_text]].filter(t => t[2]);
  if (!tabs.find(t => t[0] === _detailTab)) _detailTab = tabs[0]?.[0] || 'breakdown';

  let body = '';
  if (_detailTab === 'breakdown') body = await renderMd(job.breakdown_md);
  else if (_detailTab === 'deep') body = await renderMd(job.deepdive_md);
  else body = `<pre style="white-space:pre-wrap;font-family:inherit;font-size:13px;line-height:1.6">${esc(job.transcript_text)}</pre>`;

  pane.innerHTML = `${detailHeader(job)}
    <div style="display:flex;gap:6px;border-bottom:1px solid var(--border-dim);margin:14px 0 6px">
      ${tabs.map(([k, label]) => `<button class="ytr-tab" data-tab="${k}" style="background:none;border:none;border-bottom:2px solid ${k === _detailTab ? 'var(--accent,#60a5fa)' : 'transparent'};color:${k === _detailTab ? 'var(--text-primary)' : 'var(--text-muted)'};padding:8px 10px;font-size:13px;font-weight:600;cursor:pointer">${label}</button>`).join('')}
    </div>
    <div style="font-size:14px;line-height:1.6">${body}</div>`;
}

async function runJob() {
  const urlEl = $('ytr-url');
  const url = (urlEl?.value || '').trim();
  if (!url) { window.AgeniusDesk?.notify('Paste a YouTube link first.', 'warning'); return; }
  const depth = document.querySelector('.ytr-depth[data-active="1"]')?.dataset.depth || 'single';
  const btn = $('ytr-run');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting...'; }
  try {
    const job = await jpost(`${API}/jobs`, { url, depth, destination: ($('ytr-dest')?.value || '').trim(), model: encodeModel() });
    if (urlEl) urlEl.value = '';
    _jobs.unshift(job);
    await selectJob(job.id);
    startPolling();
  } catch (e) {
    window.AgeniusDesk?.notify(e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Run'; }
  }
}

function startPolling() {
  if (_poll) return;
  _poll = setInterval(async () => {
    if (!$('ytr-root')) { stopPolling(); return; }
    await loadJobs();
    if (!inflight()) stopPolling();
  }, 2500);
}
function stopPolling() { if (_poll) { clearInterval(_poll); _poll = null; } }

// ── Delegated wiring (survives view re-injection) ────────────────────────────

document.addEventListener('click', async (e) => {
  const t = e.target;
  if (!(t instanceof Element) || !$('ytr-root')) return;

  const depth = t.closest('.ytr-depth');
  if (depth) {
    document.querySelectorAll('.ytr-depth').forEach(o => { o.style.background = 'transparent'; o.style.color = 'var(--text-secondary)'; delete o.dataset.active; });
    depth.style.background = 'var(--accent,#60a5fa)'; depth.style.color = '#fff'; depth.dataset.active = '1';
    return;
  }
  if (t.closest('#ytr-run')) return void runJob();

  const card = t.closest('.ytr-card');
  if (card) return void selectJob(card.dataset.id);

  const tab = t.closest('.ytr-tab');
  if (tab) { _detailTab = tab.dataset.tab; return void renderDetail(); }

  if (t.closest('#ytr-deepdive')) {
    const id = _selectedId;
    try { await jpost(`${API}/jobs/${id}/deepdive`, { model: encodeModel() }); window.AgeniusDesk?.notify('Deep dive started.'); startPolling(); }
    catch (err) { window.AgeniusDesk?.notify(err.message, 'error'); }
    return;
  }
  const dl = t.closest('.ytr-dl');
  if (dl) return void window.open(`${API}/jobs/${_selectedId}/artifact?kind=${dl.dataset.kind}`, '_blank');

  if (t.closest('.ytr-move')) {
    const dest = window.prompt('Move to which research topic folder? (blank = _inbox)', _jobs.find(j => j.id === _selectedId)?.destination || '');
    if (dest === null) return;
    try { await jpost(`${API}/jobs/${_selectedId}/move`, { destination: dest.trim() }); window.AgeniusDesk?.notify('Moved.'); await loadJobs(); }
    catch (err) { window.AgeniusDesk?.notify(err.message, 'error'); }
    return;
  }
  if (t.closest('.ytr-del')) {
    if (!window.confirm('Delete this research run? The vault notes stay in your harness.')) return;
    try { await jdel(`${API}/jobs/${_selectedId}`); _jobs = _jobs.filter(j => j.id !== _selectedId); _selectedId = _jobs[0]?.id || null; renderList(); await renderDetail(); }
    catch (err) { window.AgeniusDesk?.notify(err.message, 'error'); }
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target instanceof Element && e.target.id === 'ytr-url') { e.preventDefault(); runJob(); }
});
document.addEventListener('change', (e) => {
  if (e.target instanceof Element && e.target.id === 'ytr-provider') loadModels();
});

async function mount() {
  await initProviderModel();
  await loadDestinations();
  await loadJobs();
  if (inflight()) startPolling();
}

const _obs = new MutationObserver(() => {
  const root = $('ytr-root');
  if (root && root.dataset.mounted !== '1') { root.dataset.mounted = '1'; mount(); }
});
_obs.observe(document.body, { childList: true, subtree: true });
if ($('ytr-root')) { $('ytr-root').dataset.mounted = '1'; mount(); }
