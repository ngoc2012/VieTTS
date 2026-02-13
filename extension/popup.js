// ---- Base URL (read from input, persisted in localStorage) ----
const BASE_URL_KEY = 'vieneu_base_url';

function getBaseUrl() {
  return document.getElementById('inp-server-url').value.replace(/\/+$/, '');
}

// Persist server URL on change
document.getElementById('inp-server-url').addEventListener('input', () => {
  localStorage.setItem(BASE_URL_KEY, getBaseUrl());
});

// Restore saved server URL
(function restoreBaseUrl() {
  const saved = localStorage.getItem(BASE_URL_KEY);
  if (saved) document.getElementById('inp-server-url').value = saved;
})();

// ---- State persistence via localStorage ----
const STORAGE_KEY = 'vieneu_state';
const JOBS_KEY = 'vieneu_jobs'; // {rowId: jobId, ...}

function saveState() {
  const rows = [];
  document.querySelectorAll('.text-row').forEach(row => {
    rows.push({ id: row.dataset.id, text: row.querySelector('textarea').value });
  });
  const state = {
    backbone: document.getElementById('sel-backbone').value,
    codec: document.getElementById('sel-codec').value,
    voice: document.getElementById('sel-voice').value,
    temperature: document.getElementById('inp-temp').value,
    tab: document.getElementById('panel-clone').classList.contains('active') ? 'clone' : 'preset',
    ref_text: document.getElementById('inp-ref-text').value,
    rows: rows,
  };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function getSavedState() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; } catch { return {}; }
}

function saveJobMap(map) { localStorage.setItem(JOBS_KEY, JSON.stringify(map)); }
function getJobMap() {
  try { return JSON.parse(localStorage.getItem(JOBS_KEY)) || {}; } catch { return {}; }
}

// Auto-save on any input change
document.addEventListener('input', saveState);
document.addEventListener('change', saveState);

// ---- Rows ----
let rowCounter = 0;
const pollTimers = {};       // rowId -> intervalId
const streamAborts = {};     // rowId -> AbortController (for PCM stream fetch)

function addRow(text, rowId) {
  if (!rowId) rowId = 'r' + (++rowCounter);
  else { const n = parseInt(rowId.slice(1)); if (n >= rowCounter) rowCounter = n; }
  const container = document.getElementById('text-rows');
  const div = document.createElement('div');
  div.className = 'text-row';
  div.dataset.id = rowId;
  div.innerHTML = `
    <div class="text-row-input">
      <textarea rows="2" placeholder="Nhập văn bản tiếng Việt...">${esc(text || '')}</textarea>
      <div class="row-btns">
        <button class="btn-clear" data-action="clear">Clear</button>
        <button class="btn-success row-gen" data-action="gen">Gen</button>
        <button class="btn-stop" data-action="stop">Stop</button>
      </div>
    </div>
    <div class="row-result">
      <div class="status" data-role="status"></div>
      <audio controls style="display:none" data-role="player"></audio>
    </div>`;
  container.appendChild(div);
  saveState();
  return rowId;
}

function stopStream(rowId) {
  if (streamAborts[rowId]) {
    streamAborts[rowId].abort();
    delete streamAborts[rowId];
  }
  removeFromPlayQueue(rowId);
  const el = getRowEl(rowId);
  if (el) {
    el.player.onended = null;
    el.player.pause();
    if (el.player.src && el.player.src.startsWith('blob:')) {
      URL.revokeObjectURL(el.player.src);
    }
  }
}

function stopRow(rowId) {
  stopStream(rowId);
  cancelRetry(rowId);
  if (pollTimers[rowId]) { clearInterval(pollTimers[rowId]); delete pollTimers[rowId]; }
  // Cancel server-side generation
  const jobMap = getJobMap();
  const jobId = jobMap[rowId];
  if (jobId) {
    fetch(`${getBaseUrl()}/api/cancel/${jobId}`, { method: 'POST' }).catch(() => {});
  }
  const el = getRowEl(rowId);
  if (el) {
    el.btn.disabled = false;
    setStatus(el.st, 'info', 'Stopped');
  }
}

function clearRow(rowId) {
  stopStream(rowId);
  cancelRetry(rowId);
  const allRows = document.querySelectorAll('.text-row');
  if (allRows.length > 1) {
    // Remove this row entirely
    const row = document.querySelector(`.text-row[data-id="${rowId}"]`);
    if (row) row.remove();
    if (pollTimers[rowId]) { clearInterval(pollTimers[rowId]); delete pollTimers[rowId]; }
    const jm = getJobMap(); delete jm[rowId]; saveJobMap(jm);
  } else {
    // Last row — just clear content
    const el = getRowEl(rowId);
    if (!el) return;
    el.textarea.value = '';
    el.st.className = 'status'; el.st.textContent = '';
    el.player.style.display = 'none'; el.player.removeAttribute('src');
  }
  saveState();
}

function stopAll() {
  const jobMap = getJobMap();
  for (const id of Object.keys(pollTimers)) { clearInterval(pollTimers[id]); delete pollTimers[id]; }
  for (const id of Object.keys(streamAborts)) stopStream(id);
  for (const id of Object.keys(retryTimers)) cancelRetry(id);
  playQueue = []; activePlayer = null;
  // Cancel all server-side jobs
  for (const [rowId, jobId] of Object.entries(jobMap)) {
    fetch(`${getBaseUrl()}/api/cancel/${jobId}`, { method: 'POST' }).catch(() => {});
  }
  // Re-enable all Gen buttons and show Stopped status
  document.querySelectorAll('.text-row').forEach(row => {
    const rowId = row.dataset.id;
    const el = getRowEl(rowId);
    if (el) {
      el.btn.disabled = false;
      if (el.st.classList.contains('info')) setStatus(el.st, 'info', 'Stopped');
    }
  });
  document.getElementById('btn-gen-all').disabled = false;
}

function clearAll() {
  // Stop all poll timers, streams, retries, and playback queue
  for (const id of Object.keys(pollTimers)) { clearInterval(pollTimers[id]); delete pollTimers[id]; }
  for (const id of Object.keys(streamAborts)) stopStream(id);
  for (const id of Object.keys(retryTimers)) cancelRetry(id);
  playQueue = []; activePlayer = null;
  // Remove all rows except keep one empty
  document.getElementById('text-rows').innerHTML = '';
  saveJobMap({});
  addRow('');
}

function downloadAll() {
  const jobMap = getJobMap();
  let i = 0;
  for (const [rowId, jobId] of Object.entries(jobMap)) {
    i++;
    setTimeout(() => {
      const a = document.createElement('a');
      a.href = `${getBaseUrl()}/api/audio/${jobId}`;
      a.download = `vieneu_${rowId}.wav`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }, i * 500);
  }
}


function preprocessText(text) {
  // Remove URLs, hashtags, emoji, repeated special chars
  text = text.replace(/https?:\/\/\S+/gi, '');
  text = text.replace(/#\S+/g, '');
  text = text.replace(/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{FE00}-\u{FE0F}\u{200D}\u{20E3}\u{E0020}-\u{E007F}]/gu, '');
  text = text.replace(/([^a-zA-Z0-9\s\u{00C0}-\u{024F}\u{1E00}-\u{1EFF}])\1{2,}/gu, '');
  text = text.replace(/[ \t]+/g, ' ');
  text = text.replace(/\n\s*\n+/g, '\n');
  return text.trim();
}

function getRowEl(rowId) {
  const row = document.querySelector(`.text-row[data-id="${rowId}"]`);
  if (!row) return null;
  return {
    row,
    textarea: row.querySelector('textarea'),
    btn: row.querySelector('.row-gen'),
    st: row.querySelector('[data-role="status"]'),
    player: row.querySelector('[data-role="player"]'),
  };
}

function setStatus(el, cls, msg) {
  el.className = 'status ' + cls;
  el.textContent = msg;
}

// ---- Playback queue: only one row plays at a time ----
let playQueue = [];       // rowIds waiting to play
let activePlayer = null;  // rowId currently playing
const pendingPlay = {};   // rowId -> function to call when it's this row's turn

function onPlayerFinished(rowId) {
  if (activePlayer === rowId) activePlayer = null;
  if (playQueue.length > 0) {
    const nextId = playQueue.shift();
    const fn = pendingPlay[nextId];
    if (fn) { delete pendingPlay[nextId]; fn(); }
  }
}

function requestPlay(rowId, playFn) {
  if (!activePlayer || activePlayer === rowId) {
    activePlayer = rowId;
    playFn();
  } else {
    if (!playQueue.includes(rowId)) playQueue.push(rowId);
    pendingPlay[rowId] = () => { activePlayer = rowId; playFn(); };
  }
}

function removeFromPlayQueue(rowId) {
  playQueue = playQueue.filter(id => id !== rowId);
  delete pendingPlay[rowId];
  if (activePlayer === rowId) activePlayer = null;
}

// ---- MediaSource streaming (WebM/Opus) ----
const MSE_MIME = 'audio/webm; codecs="opus"';
const MIN_BUFFER_SEC = 20.0;

async function startPcmStream(rowId, jobId) {
  stopStream(rowId);
  const el = getRowEl(rowId);
  if (!el) return;

  if (!window.MediaSource || !MediaSource.isTypeSupported(MSE_MIME)) {
    setStatus(el.st, 'error', 'Browser does not support MediaSource with WebM/Opus');
    return;
  }

  const abort = new AbortController();
  streamAborts[rowId] = abort;

  const mediaSource = new MediaSource();
  el.player.src = URL.createObjectURL(mediaSource);
  el.player.style.display = 'block';

  await new Promise(resolve => mediaSource.addEventListener('sourceopen', resolve, { once: true }));

  const sourceBuffer = mediaSource.addSourceBuffer(MSE_MIME);
  let totalBytes = 0;
  let playStarted = false;

  // When audio ends, switch to server WAV for lossless replay and advance queue
  el.player.onended = () => {
    const serverUrl = el.row.dataset.serverAudio;
    if (serverUrl) {
      if (el.player.src && el.player.src.startsWith('blob:')) URL.revokeObjectURL(el.player.src);
      el.player.src = serverUrl;
      el.player.onended = null;
    }
    onPlayerFinished(rowId);
  };

  async function waitForBuffer() {
    if (sourceBuffer.updating) {
      await new Promise(resolve => sourceBuffer.addEventListener('updateend', resolve, { once: true }));
    }
  }

  try {
    const resp = await fetch(`${getBaseUrl()}/api/stream/${jobId}`, { signal: abort.signal });
    if (!resp.ok || !resp.body) return;
    const reader = resp.body.getReader();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      await waitForBuffer();
      try {
        sourceBuffer.appendBuffer(value);
      } catch (e) {
        console.error('SourceBuffer append error:', e);
        break;
      }
      totalBytes += value.length;
      await waitForBuffer();

      // Check buffered duration
      if (sourceBuffer.buffered.length > 0) {
        const bufferedEnd = sourceBuffer.buffered.end(0);
        const bufferedSec = bufferedEnd - el.player.currentTime;

        if (!playStarted) {
          setStatus(el.st, 'info',
            `Buffering ${bufferedSec.toFixed(1)}s / ${MIN_BUFFER_SEC.toFixed(1)}s — ${(totalBytes / 1024).toFixed(0)}KB`);

          if (bufferedSec >= MIN_BUFFER_SEC) {
            playStarted = true;
            setStatus(el.st, 'info', `Playing — buffered ${bufferedSec.toFixed(1)}s`);
            requestPlay(rowId, () => el.player.play().catch(() => {}));
          }
        }
      }
    }

    // Signal end of stream
    await waitForBuffer();
    if (mediaSource.readyState === 'open') {
      mediaSource.endOfStream();
    }

    // Very short audio — never hit buffer threshold
    if (!playStarted && sourceBuffer.buffered.length > 0) {
      playStarted = true;
      requestPlay(rowId, () => el.player.play().catch(() => {}));
    }

  } catch (e) {
    if (e.name !== 'AbortError') console.error('MSE stream error:', e);
  } finally {
    delete streamAborts[rowId];
  }
}

// ---- Init ----
const DEFAULT_BACKBONE = "VieNeu-TTS-0.3B-q4-gguf";
const DEFAULT_CODEC = "NeuCodec ONNX (Fast CPU)";
const DEFAULT_VOICE = "Binh";

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function init() {
  const saved = getSavedState();

  try {
    const [models, codecs] = await Promise.all([
      fetch(`${getBaseUrl()}/api/models`).then(r => r.json()),
      fetch(`${getBaseUrl()}/api/codecs`).then(r => r.json()),
    ]);

    const pickBackbone = saved.backbone || DEFAULT_BACKBONE;
    const selB = document.getElementById('sel-backbone');
    selB.innerHTML = models.map(m =>
      `<option value="${esc(m.name)}" title="${esc(m.description)}"${m.name === pickBackbone ? ' selected' : ''}>${esc(m.name)}</option>`
    ).join('');

    const pickCodec = saved.codec || DEFAULT_CODEC;
    const selC = document.getElementById('sel-codec');
    selC.innerHTML = codecs.map(c =>
      `<option value="${esc(c.name)}" title="${esc(c.description)}"${c.name === pickCodec ? ' selected' : ''}>${esc(c.name)}</option>`
    ).join('');

    const pickVoice = saved.voice || DEFAULT_VOICE;
    const voices = await fetch(`${getBaseUrl()}/api/voices`).then(r => r.json());
    const selV = document.getElementById('sel-voice');
    if (voices.length > 0) {
      selV.innerHTML = voices.map(v =>
        `<option value="${esc(v.id)}"${v.id === pickVoice ? ' selected' : ''}>${esc(v.description)} (${esc(v.id)})</option>`
      ).join('');
    }
    setStatus(document.getElementById('model-status'), 'success', 'Model preloaded and ready.');
  } catch (e) {
    setStatus(document.getElementById('model-status'), 'error', 'Cannot connect to server: ' + e.message);
  }

  if (saved.temperature) document.getElementById('inp-temp').value = saved.temperature;
  if (saved.ref_text) document.getElementById('inp-ref-text').value = saved.ref_text;
  if (saved.tab === 'clone') switchTab('clone');

  // Restore rows
  if (saved.rows && saved.rows.length > 0) {
    saved.rows.forEach(r => addRow(r.text, r.id));
  } else {
    addRow('');
  }

  // Restore jobs per row
  const jobMap = getJobMap();
  for (const [rowId, jobId] of Object.entries(jobMap)) {
    const el = getRowEl(rowId);
    if (!el) continue;
    try {
      const r = await fetch(`${getBaseUrl()}/api/status/${jobId}`);
      if (r.ok) {
        const data = await r.json();
        if (data.status === 'done') {
          setStatus(el.st, 'success', data.progress || 'Done!');
          el.player.src = `${getBaseUrl()}${data.audio_url}`;
          el.player.style.display = 'block';
        } else if (data.status === 'processing' || data.status === 'pending') {
          setStatus(el.st, 'info', 'Resuming...');
          pollRow(rowId, jobId);
        } else if (data.status === 'error') {
          setStatus(el.st, 'error', 'Error: ' + (data.error || 'Unknown'));
        }
      } else {
        delete jobMap[rowId]; saveJobMap(jobMap);
      }
    } catch { delete jobMap[rowId]; saveJobMap(jobMap); }
  }
}

// ---- Tabs ----
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.getElementById('panel-preset').classList.toggle('active', tab === 'preset');
  document.getElementById('panel-clone').classList.toggle('active', tab === 'clone');
  saveState();
}

// ---- Load model ----
async function loadModel() {
  const btn = document.getElementById('btn-load');
  const st = document.getElementById('model-status');
  btn.disabled = true;
  setStatus(st, 'info', 'Loading model...');

  try {
    const resp = await fetch(`${getBaseUrl()}/api/load_model`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        backbone: document.getElementById('sel-backbone').value,
        codec: document.getElementById('sel-codec').value,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Failed');

    setStatus(st, 'success',
      `Model loaded: ${data.backbone} (${data.backbone_device}) + ${data.codec} (${data.codec_device})`);

    const voices = await fetch(`${getBaseUrl()}/api/voices`).then(r => r.json());
    const selV = document.getElementById('sel-voice');
    const savedVoice = getSavedState().voice || DEFAULT_VOICE;
    if (voices.length > 0) {
      selV.innerHTML = voices.map(v =>
        `<option value="${esc(v.id)}"${v.id === savedVoice ? ' selected' : ''}>${esc(v.description)} (${esc(v.id)})</option>`
      ).join('');
    } else {
      selV.innerHTML = '<option value="">No preset voices available</option>';
    }
    saveState();
  } catch (e) {
    setStatus(st, 'error', 'Error: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

// ---- Submit synthesis request, retrying every 5s when server is busy ----
const retryTimers = {};  // rowId -> timer id

function cancelRetry(rowId) {
  if (retryTimers[rowId]) { clearTimeout(retryTimers[rowId]); delete retryTimers[rowId]; }
}

async function submitSynthesize(rowId, text) {
  const presetActive = document.getElementById('panel-preset').classList.contains('active');
  let resp;
  if (presetActive) {
    resp = await fetch(`${getBaseUrl()}/api/synthesize`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        text: text,
        voice_id: document.getElementById('sel-voice').value,
        temperature: parseFloat(document.getElementById('inp-temp').value) || 1.0,
      }),
    });
  } else {
    const fd = new FormData();
    fd.append('text', text);
    fd.append('temperature', document.getElementById('inp-temp').value);
    fd.append('ref_text', document.getElementById('inp-ref-text').value);
    const fileInput = document.getElementById('inp-ref-audio');
    if (fileInput.files.length > 0) fd.append('ref_audio', fileInput.files[0]);
    resp = await fetch(`${getBaseUrl()}/api/synthesize`, { method: 'POST', body: fd });
  }
  return resp;
}

// ---- Per-row generate ----
async function generateRow(rowId) {
  const el = getRowEl(rowId);
  if (!el) return;
  stopStream(rowId);
  cancelRetry(rowId);
  el.btn.disabled = true;
  el.player.style.display = 'none';

  // Preprocess text before generating
  el.textarea.value = preprocessText(el.textarea.value);
  saveState();

  const text = el.textarea.value.trim();
  if (!text) { setStatus(el.st, 'error', 'Please enter text'); el.btn.disabled = false; return; }

  try {
    const resp = await submitSynthesize(rowId, text);
    const data = await resp.json();
    if (resp.status === 503 && data.busy) {
      setStatus(el.st, 'info', 'Queued — waiting for server...');
      retryTimers[rowId] = setTimeout(() => generateRow(rowId), 5000);
      return;
    }
    if (!resp.ok) throw new Error(data.error || 'Failed');

    const jobMap = getJobMap(); jobMap[rowId] = data.job_id; saveJobMap(jobMap);
    setStatus(el.st, 'info', 'Processing...');
    startPcmStream(rowId, data.job_id);
    pollRow(rowId, data.job_id);
  } catch (e) {
    setStatus(el.st, 'error', 'Error: ' + e.message);
    el.btn.disabled = false;
  }
}

// ---- Generate all rows sequentially ----
async function generateAll() {
  const rows = document.querySelectorAll('.text-row');
  const btn = document.getElementById('btn-gen-all');
  btn.disabled = true;
  for (const row of rows) {
    const rowId = row.dataset.id;
    const el = getRowEl(rowId);
    if (!el) continue;
    const text = el.textarea.value.trim();
    if (!text) continue;
    await generateRowAsync(rowId);
  }
  btn.disabled = false;
}

function generateRowAsync(rowId) {
  return new Promise(async (resolve) => {
    const el = getRowEl(rowId);
    if (!el) { resolve(); return; }
    stopStream(rowId);
    cancelRetry(rowId);
    el.btn.disabled = true;
    el.player.style.display = 'none';


    // Preprocess text before generating
    el.textarea.value = preprocessText(el.textarea.value);
    saveState();

    const text = el.textarea.value.trim();
    if (!text) { el.btn.disabled = false; resolve(); return; }

    async function trySubmit() {
      try {
        const resp = await submitSynthesize(rowId, text);
        const data = await resp.json();
        if (resp.status === 503 && data.busy) {
          setStatus(el.st, 'info', 'Queued — waiting for server...');
          retryTimers[rowId] = setTimeout(trySubmit, 5000);
          return;
        }
        if (!resp.ok) throw new Error(data.error || 'Failed');

        const jobMap = getJobMap(); jobMap[rowId] = data.job_id; saveJobMap(jobMap);
        setStatus(el.st, 'info', 'Processing...');
        startPcmStream(rowId, data.job_id);
        pollRowAsync(rowId, data.job_id, resolve);
      } catch (e) {
        setStatus(el.st, 'error', 'Error: ' + e.message);
        el.btn.disabled = false; resolve();
      }
    }
    trySubmit();
  });
}

function pollRowAsync(rowId, jobId, onDone) {
  const el = getRowEl(rowId);
  if (!el) { onDone(); return; }
  el.btn.disabled = true;

  if (pollTimers[rowId]) clearInterval(pollTimers[rowId]);
  pollTimers[rowId] = setInterval(async () => {
    const el = getRowEl(rowId);
    if (!el) { clearInterval(pollTimers[rowId]); delete pollTimers[rowId]; onDone(); return; }
    try {
      const r = await fetch(`${getBaseUrl()}/api/status/${jobId}`);
      if (r.status === 404) {
        clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
        setStatus(el.st, 'error', 'Job expired');
        el.btn.disabled = false; onDone(); return;
      }
      const data = await r.json();
      if (data.status === 'processing' || data.status === 'pending') {
        setStatus(el.st, 'info', data.progress || 'Processing...');
      } else if (data.status === 'done') {
        clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
        setStatus(el.st, 'success', data.progress || 'Done!');
        el.btn.disabled = false;
        // Store server URL; onended handler will switch to it
        el.row.dataset.serverAudio = `${getBaseUrl()}${data.audio_url}`;
        // If no MSE stream active, set server WAV now
        if (el.player.paused && !(el.player.src && el.player.src.startsWith('blob:'))) {
          el.player.src = `${getBaseUrl()}${data.audio_url}`;
          el.player.style.display = 'block';
        }
        onDone();
      } else if (data.status === 'error') {
        clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
        stopStream(rowId);
        setStatus(el.st, 'error', 'Error: ' + (data.error || 'Unknown'));
        el.btn.disabled = false; onDone();
      }
    } catch (e) {
      clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
      setStatus(el.st, 'error', 'Polling error: ' + e.message);
      el.btn.disabled = false; onDone();
    }
  }, 1000);
}

function pollRow(rowId, jobId) {
  const el = getRowEl(rowId);
  if (!el) return;
  el.btn.disabled = true;

  if (pollTimers[rowId]) clearInterval(pollTimers[rowId]);
  pollTimers[rowId] = setInterval(async () => {
    const el = getRowEl(rowId);
    if (!el) { clearInterval(pollTimers[rowId]); delete pollTimers[rowId]; return; }
    try {
      const r = await fetch(`${getBaseUrl()}/api/status/${jobId}`);
      if (r.status === 404) {
        clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
        const jm = getJobMap(); delete jm[rowId]; saveJobMap(jm);
        setStatus(el.st, 'error', 'Job expired (server may have restarted)');
        el.btn.disabled = false; return;
      }
      const data = await r.json();
      if (data.status === 'processing' || data.status === 'pending') {
        setStatus(el.st, 'info', data.progress || 'Processing...');
      } else if (data.status === 'done') {
        clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
        setStatus(el.st, 'success', data.progress || 'Done!');
        el.btn.disabled = false;
        // Store server URL; onended handler will switch to it
        el.row.dataset.serverAudio = `${getBaseUrl()}${data.audio_url}`;
        // If no MSE stream active, set server WAV now
        if (el.player.paused && !(el.player.src && el.player.src.startsWith('blob:'))) {
          el.player.src = `${getBaseUrl()}${data.audio_url}`;
          el.player.style.display = 'block';
        }
      } else if (data.status === 'error') {
        clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
        stopStream(rowId);
        setStatus(el.st, 'error', 'Error: ' + (data.error || 'Unknown'));
        el.btn.disabled = false;
      }
    } catch (e) {
      clearInterval(pollTimers[rowId]); delete pollTimers[rowId];
      setStatus(el.st, 'error', 'Polling error: ' + e.message);
      el.btn.disabled = false;
    }
  }, 1000);
}

// ---- Inspect mode: inject content script into active tab ----
function toggleInspect() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (!tabs[0]) return;
    chrome.scripting.executeScript({
      target: { tabId: tabs[0].id },
      files: ['content.js'],
    });
  });
}

// ---- Bind all event listeners (no inline handlers) ----
document.getElementById('btn-load').addEventListener('click', loadModel);
document.getElementById('btn-add').addEventListener('click', () => addRow());
document.getElementById('btn-inspect').addEventListener('click', toggleInspect);
document.getElementById('btn-gen-all').addEventListener('click', generateAll);
document.getElementById('btn-download-all').addEventListener('click', downloadAll);
document.getElementById('btn-stop-all').addEventListener('click', stopAll);
document.getElementById('btn-clear-all').addEventListener('click', clearAll);

// Tabs
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => switchTab(tab.dataset.tab));
});

// Event delegation for dynamic row buttons
document.getElementById('text-rows').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const row = btn.closest('.text-row');
  if (!row) return;
  const rowId = row.dataset.id;
  const action = btn.dataset.action;
  if (action === 'clear') clearRow(rowId);
  else if (action === 'gen') generateRow(rowId);
  else if (action === 'stop') stopRow(rowId);
});

init();
