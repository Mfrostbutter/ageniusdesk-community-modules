/**
 * YouTube Research - community module front-end.
 *
 * AgeniusDesk injects research.html into #app-content and loads this script ONCE
 * per session. So all behavior is bound via document-level event delegation, and
 * a MutationObserver (re)loads the recent list each time the view is mounted.
 * Network goes through window.AgeniusDesk.fetch (same-origin, CSRF-aware).
 */

const API = '/api/youtube-research';

function af(path, opts) {
  const f = (window.AgeniusDesk && window.AgeniusDesk.fetch) || window.fetch;
  return f(path, opts);
}

async function jget(path) {
  const r = await af(path);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function jpost(path, body) {
  const r = await af(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

const STATUS_COLORS = {
  queued: 'var(--text-secondary)',
  transcribing: '#fbbf24',
  analyzing: '#fbbf24',
  filing: '#fbbf24',
  done: '#34d399',
  error: '#ff6d5a',
};

function setStatus(msg, color) {
  const el = document.getElementById('yt-status');
  if (!el) return;
  el.style.color = color || 'var(--text-secondary)';
  el.textContent = msg || '';
}

function renderResult(job) {
  const wrap = document.getElementById('yt-result');
  const head = document.getElementById('yt-result-head');
  const body = document.getElementById('yt-result-body');
  if (!wrap || !head || !body) return;
  if (!job || !job.breakdown_md) {
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = 'block';
  const filed = job.filed_path
    ? `Filed: <code>${job.filed_path}</code>${job.topic ? ` · topic <strong>${job.topic}</strong>` : ' · stayed in inbox'}`
    : '';
  const tags = (job.tags || []).length ? ` · tags: ${(job.tags || []).join(', ')}` : '';
  head.innerHTML = `<strong>${job.title || job.video_id || 'Breakdown'}</strong>` +
    (job.channel ? ` <span style="opacity:0.6">- ${job.channel}</span>` : '') +
    `<div style="font-size:11px;opacity:0.7;margin-top:4px">${filed}${tags}</div>`;
  body.textContent = job.breakdown_md;
}

async function pollJob(id) {
  // Poll until the job reaches a terminal state.
  for (;;) {
    let job;
    try {
      job = await jget(`${API}/jobs/${id}`);
    } catch {
      setStatus('Lost track of the job.', '#ff6d5a');
      return;
    }
    setStatus(job.progress || job.status, STATUS_COLORS[job.status]);
    if (job.status === 'done' || job.status === 'error') {
      if (job.status === 'error') setStatus(job.error || 'Failed.', '#ff6d5a');
      renderResult(job);
      refreshJobs();
      return;
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

async function runJob() {
  const input = document.getElementById('yt-url');
  const btn = document.getElementById('yt-run');
  if (!input) return;
  const url = input.value.trim();
  if (!url) {
    setStatus('Paste a YouTube URL or video id.', '#ff6d5a');
    return;
  }
  if (btn) btn.disabled = true;
  setStatus('Starting…');
  renderResult(null);
  try {
    const job = await jpost(`${API}/jobs`, { url });
    await pollJob(job.id);
  } catch (e) {
    setStatus(`Failed: ${e.message}`, '#ff6d5a');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function jobRow(job) {
  const color = STATUS_COLORS[job.status] || 'var(--text-secondary)';
  const where = job.filed_path
    ? (job.topic ? `research/${job.topic}` : 'research/inbox')
    : '';
  return `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 10px;border:1px solid var(--border-dim);border-radius:var(--radius);margin-bottom:6px">
      <div style="min-width:0;flex:1">
        <div style="font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${job.title || job.url || job.video_id}</div>
        <div style="font-size:11px;opacity:0.65;margin-top:2px">
          <span style="color:${color}">${job.status}</span>
          ${where ? ` · <code>${where}</code>` : ''}
          ${job.channel ? ` · ${job.channel}` : ''}
        </div>
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-sm btn-ghost" data-yt-open="${job.id}">Open</button>
        <button class="btn btn-sm btn-ghost" data-yt-del="${job.id}" style="color:#ff6d5a">×</button>
      </div>
    </div>`;
}

async function refreshJobs() {
  const list = document.getElementById('yt-jobs');
  if (!list) return;
  try {
    const data = await jget(`${API}/jobs`);
    const jobs = data.jobs || [];
    list.innerHTML = jobs.length
      ? jobs.map(jobRow).join('')
      : '<div style="font-size:12px;opacity:0.5">No runs yet.</div>';
  } catch {
    list.innerHTML = '<div style="font-size:12px;color:#ff6d5a">Could not load recent runs.</div>';
  }
}

async function openJob(id) {
  try {
    const job = await jget(`${API}/jobs/${id}`);
    renderResult(job);
    setStatus(job.progress || job.status, STATUS_COLORS[job.status]);
    document.getElementById('yt-result')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (e) {
    setStatus(`Could not open: ${e.message}`, '#ff6d5a');
  }
}

async function deleteJob(id) {
  try {
    await af(`${API}/jobs/${id}`, { method: 'DELETE' });
  } catch { /* ignore */ }
  refreshJobs();
}

// ── Wiring (delegated, survives view re-injection) ───────────────────────────

document.addEventListener('click', (e) => {
  const t = e.target;
  if (!(t instanceof Element)) return;
  if (t.closest('#yt-run')) return void runJob();
  if (t.closest('#yt-refresh')) return void refreshJobs();
  const open = t.closest('[data-yt-open]');
  if (open) return void openJob(open.getAttribute('data-yt-open'));
  const del = t.closest('[data-yt-del]');
  if (del) return void deleteJob(del.getAttribute('data-yt-del'));
});

// Enter in the URL field runs it.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target instanceof Element && e.target.id === 'yt-url') {
    e.preventDefault();
    runJob();
  }
});

// The view is re-injected on each navigation; load the recent list when our
// root (re)appears, once per mount.
const _obs = new MutationObserver(() => {
  const root = document.getElementById('yt-root');
  if (root && root.dataset.mounted !== '1') {
    root.dataset.mounted = '1';
    refreshJobs();
  }
});
_obs.observe(document.body, { childList: true, subtree: true });

// In case the view is already present when this script first loads.
if (document.getElementById('yt-root')) {
  document.getElementById('yt-root').dataset.mounted = '1';
  refreshJobs();
}
