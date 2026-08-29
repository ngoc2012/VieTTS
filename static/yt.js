// ── Utilities ──────────────────────────────────────────────────────────────
function extractVideoId(url) {
  try {
    const u = new URL(url.trim());
    if (u.searchParams.get('v')) return u.searchParams.get('v');
    if (u.hostname === 'youtu.be') return u.pathname.slice(1).split('?')[0];
    const m = u.pathname.match(/\/(embed|shorts|v)\/([^/?&]+)/);
    if (m) return m[2];
  } catch {}
  const m = url.trim().match(/^[A-Za-z0-9_-]{11}$/);
  if (m) return url.trim();
  return null;
}

function getDirectBase() {
  return window.location.origin;
}

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}
function fmtDate(ts) { return new Date(ts * 1000).toLocaleString(); }

function stemOf(name) { return name.replace(/\.[^.]+$/, ''); }

// ── File list ──────────────────────────────────────────────────────────────
let _fileNames = [];

async function loadFiles() {
  const list = document.getElementById('file-list');
  const empty = document.getElementById('files-empty');
  try {
    const r = await fetch('/api/yt/files');
    const files = await r.json();
    _fileNames = files.map(f => f.name);
    list.innerHTML = '';
    if (!files.length) { empty.style.display = ''; return; }
    empty.style.display = 'none';
    for (const f of files) {
      if (f.name.endsWith('.srt') || f.name.endsWith('.wav')) continue;
      const enc = encodeURIComponent(f.name);
      const li = document.createElement('li');
      li.className = 'file-item';
      li.innerHTML = `
        <span class="file-name">${f.name}</span>
        <span class="file-meta">${fmtSize(f.size)} &middot; ${fmtDate(f.mtime)}</span>
        <button class="btn-play" data-name="${enc}">Play</button>
        <button class="btn-dl"  data-name="${enc}">Download</button>
        <button class="btn-del" data-name="${enc}">Delete</button>
      `;
      list.appendChild(li);
    }
  } catch (e) {
    empty.textContent = 'Failed to load files: ' + e.message;
    empty.style.display = '';
  }
}

// ── Inline player ──────────────────────────────────────────────────────────
async function playVideo(name) {
  const base = await getDirectBase();
  const enc = encodeURIComponent(name);
  const stem = stemOf(name);
  const srtName = stem + '.fr.srt';

  const playerCard  = document.getElementById('player-card');
  const playerTitle = document.getElementById('player-title');
  const video       = document.getElementById('player-video');
  const src         = document.getElementById('player-src');
  const track       = document.getElementById('player-track');

  src.src   = `${base}/api/yt/stream/${enc}`;
  track.src = _fileNames.includes(srtName)
    ? `/api/yt/vtt/${encodeURIComponent(srtName)}`
    : '';
  track.default = !!track.src;

  video.load();
  playerTitle.textContent = name;
  playerCard.style.display = 'block';
  playerCard.scrollIntoView({ behavior: 'smooth' });
  video.play().catch(() => {});
}

// ── File list click handler ────────────────────────────────────────────────
document.getElementById('file-list').addEventListener('click', async (e) => {
  const el = e.target.closest('button');
  if (!el) return;
  const name = decodeURIComponent(el.dataset.name);

  if (el.classList.contains('btn-play')) {
    el.disabled = true;
    try { await playVideo(name); } catch(e) { alert('Player error: ' + e.message); }
    el.disabled = false;
    return;
  }

  if (el.classList.contains('btn-srt')) {
    el.disabled = true;
    try {
      const base = await getDirectBase();
      window.open(`${base}/api/yt/stream/${encodeURIComponent(name)}`, '_blank');
    } catch (e) { alert('Could not resolve direct URL: ' + e.message); }
    el.disabled = false;
    return;
  }

  if (el.classList.contains('btn-dl')) {
    el.disabled = true;
    try {
      const r = await fetch(`/api/yt/file/${encodeURIComponent(name)}`);
      if (!r.ok) throw new Error('Server error');
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = name; a.click();
      URL.revokeObjectURL(url);
    } catch (e) { alert('Download failed: ' + e.message); }
    el.disabled = false;
    return;
  }

  if (el.classList.contains('btn-del')) {
    if (!confirm(`Delete "${name}"?`)) return;
    el.disabled = true;
    try {
      const r = await fetch(`/api/yt/file/${encodeURIComponent(name)}`, {method: 'DELETE'});
      if (!r.ok) throw new Error('Server error');
      loadFiles();
    } catch (e) { alert('Delete failed: ' + e.message); el.disabled = false; }
  }
});

// ── YouTube URL input ──────────────────────────────────────────────────────
const inp      = document.getElementById('inp-url');
const ytPrev   = document.getElementById('yt-preview');
const frame    = document.getElementById('yt-frame');
const msg      = document.getElementById('msg');
const btn      = document.getElementById('btn-process');
const statusEl = document.getElementById('status');

function setStatus(cls, text) { statusEl.className = cls; statusEl.textContent = text; }

inp.addEventListener('input', () => {
  const id = extractVideoId(inp.value);
  if (id) {
    frame.src = `https://www.youtube.com/embed/${id}`;
    ytPrev.style.display = 'block';
    msg.textContent = '';
    btn.disabled = false;
  } else {
    ytPrev.style.display = 'none';
    frame.src = '';
    msg.textContent = inp.value.trim() ? 'Could not detect a YouTube video ID.' : '';
    btn.disabled = true;
  }
  statusEl.className = '';
  statusEl.textContent = '';
});

// ── Process button ─────────────────────────────────────────────────────────
btn.addEventListener('click', async () => {
  const url = inp.value.trim();
  if (!url) return;
  btn.disabled = true;
  setStatus('info', 'Starting download...');

  let jobId;
  try {
    const r = await fetch('/api/yt/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url }),
    });
    if (!r.ok) throw new Error(`Server error: ${r.status} ${r.statusText}`);
    const d = await r.json();
    jobId = d.job_id;
  } catch (e) { setStatus('error', e.message); btn.disabled = false; return; }

  // Poll every 3 seconds
  const timer = setInterval(async () => {
    try {
      const r = await fetch(`/api/yt/status/${jobId}`);
      if (!r.ok) throw new Error(`Server error: ${r.status} ${r.statusText}`);
      const d = await r.json();
      if (d.status === 'processing') {
        setStatus('info', d.progress || 'Working...');
      } else if (d.status === 'done') {
        clearInterval(timer);
        setStatus('done', d.progress || 'Done');
        btn.disabled = false;
        loadFiles();
      } else if (d.status === 'error') {
        clearInterval(timer);
        setStatus('error', d.error || 'Failed');
        btn.disabled = false;
      }
    } catch (e) {
      clearInterval(timer);
      setStatus('error', 'Polling error: ' + e.message);
      btn.disabled = false;
    }
  }, 3000);
});

// ── Persist URL ────────────────────────────────────────────────────────────
const YT_URL_KEY = 'vieneu_yt_url';
inp.addEventListener('change', () => { if (inp.value.trim()) localStorage.setItem(YT_URL_KEY, inp.value.trim()); });

const params = new URLSearchParams(location.search);
const savedUrl = params.get('v') || localStorage.getItem(YT_URL_KEY) || '';
if (savedUrl) { inp.value = savedUrl; inp.dispatchEvent(new Event('input')); }

loadFiles();
