"""
VieNeu-TTS Flask App — Simple TTS web interface with polling.

Run:  uv run --with flask flask_app.py
Open: http://127.0.0.1:5000
"""

import os
import sys
import uuid
import tempfile
import threading
import time
import queue
import yaml
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template_string, Response

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
tts = None
model_loaded = False
current_backbone = None
current_codec = None

# In-memory job store: {job_id: {status, progress, audio_path, error, ...}}
jobs = {}

# Only one synthesis at a time
active_job_id = None
active_lock = threading.Lock()

# Load config
CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

BACKBONE_CONFIGS = config["backbone_configs"]
CODEC_CONFIGS = config["codec_configs"]

DEFAULT_BACKBONE = "VieNeu-TTS-0.3B-q4-gguf"
DEFAULT_CODEC = "NeuCodec ONNX (Fast CPU)"
DEFAULT_VOICE = "Binh"

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/models")
def list_models():
    models = []
    for name, cfg in BACKBONE_CONFIGS.items():
        models.append({"name": name, "repo": cfg["repo"], "description": cfg["description"]})
    return jsonify(models)


@app.get("/api/codecs")
def list_codecs():
    codecs = []
    for name, cfg in CODEC_CONFIGS.items():
        codecs.append({"name": name, "repo": cfg["repo"], "description": cfg["description"]})
    return jsonify(codecs)


@app.post("/api/load_model")
def load_model():
    global tts, model_loaded, current_backbone, current_codec

    data = request.get_json()
    backbone_choice = data.get("backbone")
    codec_choice = data.get("codec")

    if backbone_choice not in BACKBONE_CONFIGS:
        return jsonify({"error": f"Unknown backbone: {backbone_choice}"}), 400
    if codec_choice not in CODEC_CONFIGS:
        return jsonify({"error": f"Unknown codec: {codec_choice}"}), 400

    backbone_cfg = BACKBONE_CONFIGS[backbone_choice]
    codec_cfg = CODEC_CONFIGS[codec_choice]

    # Determine devices
    import torch

    if "gguf" in backbone_cfg["repo"].lower():
        backbone_device = "cpu"
    elif sys.platform == "darwin":
        backbone_device = "mps" if torch.backends.mps.is_available() else "cpu"
    else:
        backbone_device = "cuda" if torch.cuda.is_available() else "cpu"

    if "ONNX" in codec_choice:
        codec_device = "cpu"
    elif sys.platform == "darwin":
        codec_device = "mps" if torch.backends.mps.is_available() else "cpu"
    else:
        codec_device = "cuda" if torch.cuda.is_available() else "cpu"

    if "gguf" in backbone_cfg["repo"].lower() and backbone_device == "cuda":
        backbone_device = "gpu"

    # Close previous model
    if tts is not None:
        try:
            tts.close()
        except Exception:
            pass

    try:
        from vieneu import VieNeuTTS

        tts = VieNeuTTS(
            backbone_repo=backbone_cfg["repo"],
            backbone_device=backbone_device,
            codec_repo=codec_cfg["repo"],
            codec_device=codec_device,
        )
        model_loaded = True
        current_backbone = backbone_choice
        current_codec = codec_choice

        return jsonify({
            "ok": True,
            "backbone": backbone_choice,
            "codec": codec_choice,
            "backbone_device": backbone_device,
            "codec_device": codec_device,
        })
    except Exception as e:
        model_loaded = False
        tts = None
        return jsonify({"error": str(e)}), 500


@app.get("/api/voices")
def list_voices():
    if tts is None:
        return jsonify([])
    try:
        voices = tts.list_preset_voices()
        return jsonify([{"description": desc, "id": vid} for desc, vid in voices])
    except Exception:
        return jsonify([])


@app.post("/api/synthesize")
def synthesize():
    global active_job_id

    if not model_loaded or tts is None:
        return jsonify({"error": "Model not loaded"}), 400

    # Check if another job is already running
    with active_lock:
        if active_job_id is not None:
            job = jobs.get(active_job_id, {})
            if job.get("status") in ("pending", "processing"):
                return jsonify({
                    "error": "Server is busy generating audio for another client. Please wait and try again.",
                    "busy": True,
                    "active_progress": job.get("progress", ""),
                }), 503

    # Support both JSON and multipart form (for file uploads)
    if request.content_type and "multipart/form-data" in request.content_type:
        text = request.form.get("text", "").strip()
        voice_id = request.form.get("voice_id", "")
        ref_text = request.form.get("ref_text", "")
        temperature = float(request.form.get("temperature", "1.0"))
        ref_audio_file = request.files.get("ref_audio")
    else:
        data = request.get_json()
        text = data.get("text", "").strip()
        voice_id = data.get("voice_id", "")
        ref_text = data.get("ref_text", "")
        temperature = data.get("temperature", 1.0)
        ref_audio_file = None

    if not text:
        return jsonify({"error": "Text is required"}), 400

    # Save uploaded ref audio to temp file if present
    ref_audio_path = None
    if ref_audio_file:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        ref_audio_file.save(tmp.name)
        tmp.close()
        ref_audio_path = tmp.name

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "pending", "progress": "Queued",
        "audio_path": None, "error": None,
        "chunks_total": 0, "chunks_done": 0,
        "pcm_queue": queue.Queue(maxsize=200),
    }

    with active_lock:
        active_job_id = job_id

    thread = threading.Thread(
        target=_run_synthesis,
        args=(job_id, text, voice_id, ref_audio_path, ref_text, temperature),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.get("/api/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404

    resp = {"status": job["status"], "progress": job["progress"]}
    if job["status"] == "done":
        resp["audio_url"] = f"/api/audio/{job_id}"
    if job["error"]:
        resp["error"] = job["error"]
    resp["chunks_done"] = job.get("chunks_done", 0)
    resp["chunks_total"] = job.get("chunks_total", 0)
    return jsonify(resp)


@app.get("/api/audio/<job_id>")
def get_audio(job_id):
    job = jobs.get(job_id)
    if job is None or job["audio_path"] is None:
        return jsonify({"error": "Audio not available"}), 404
    return send_file(job["audio_path"], mimetype="audio/wav", as_attachment=False)


@app.get("/api/stream/<job_id>")
def stream_audio(job_id):
    """Stream raw PCM (16-bit signed LE, 24kHz mono) as chunked HTTP response."""
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404

    pcm_queue = job.get("pcm_queue")
    if pcm_queue is None:
        return jsonify({"error": "No stream available"}), 404

    def generate():
        while True:
            try:
                data = pcm_queue.get(timeout=60)
            except queue.Empty:
                break
            if data is None:
                break
            yield data

    return Response(generate(), mimetype="application/octet-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------------------
# Background synthesis worker
# ---------------------------------------------------------------------------

def _run_synthesis(job_id, text, voice_id, ref_audio_path, ref_text, temperature):
    global active_job_id
    import numpy as np
    import torch

    job = jobs[job_id]
    job["status"] = "processing"

    try:
        # Resolve reference
        ref_codes = None
        ref_text_resolved = None

        if ref_audio_path:
            job["progress"] = "Encoding reference audio..."
            ref_codes = tts.encode_reference(ref_audio_path)
            if isinstance(ref_codes, torch.Tensor):
                ref_codes = ref_codes.cpu().numpy()
            ref_text_resolved = ref_text or ""
            # Clean up temp file
            try:
                os.unlink(ref_audio_path)
            except OSError:
                pass
        elif voice_id:
            job["progress"] = "Loading preset voice..."
            voice_data = tts.get_preset_voice(voice_id)
            ref_codes = voice_data["codes"]
            if isinstance(ref_codes, torch.Tensor):
                ref_codes = ref_codes.cpu().numpy()
            ref_text_resolved = voice_data["text"]

        # Split text into chunks and synthesize one by one
        from vieneu_utils.core_utils import split_text_into_chunks, join_audio_chunks
        import soundfile as sf

        chunks = split_text_into_chunks(text, max_chars=256)
        total = len(chunks)
        job["chunks_total"] = total
        all_wavs = []

        for i, chunk in enumerate(chunks, 1):
            job["progress"] = f"Generating chunk {i}/{total}..."
            chunk_wav = tts.infer(
                text=chunk,
                ref_codes=ref_codes,
                ref_text=ref_text_resolved,
                temperature=temperature,
            )
            if chunk_wav is not None and len(chunk_wav) > 0:
                all_wavs.append(chunk_wav)
                job["chunks_done"] = i
                # Push raw PCM (int16 LE) to stream queue
                pcm_int16 = (chunk_wav * 32767).clip(-32768, 32767).astype(np.int16)
                try:
                    job["pcm_queue"].put(pcm_int16.tobytes(), timeout=10)
                except queue.Full:
                    pass
                # Add silence between chunks (0.15s)
                if i < total:
                    silence = np.zeros(int(0.15 * tts.sample_rate), dtype=np.int16)
                    try:
                        job["pcm_queue"].put(silence.tobytes(), timeout=5)
                    except queue.Full:
                        pass

        # Signal end of PCM stream
        try:
            job["pcm_queue"].put(None, timeout=5)
        except queue.Full:
            pass

        if not all_wavs:
            job["status"] = "error"
            job["error"] = "No audio generated"
            return

        job["progress"] = f"Joining {total} chunks..."
        audio = join_audio_chunks(all_wavs, sr=tts.sample_rate, silence_p=0.15)

        # Save joined final WAV
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        sf.write(tmp.name, audio, tts.sample_rate)
        tmp.close()

        job["audio_path"] = tmp.name
        job["status"] = "done"
        job["progress"] = f"Done — {total} chunks"

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        # Signal end of stream on error too
        try:
            job["pcm_queue"].put(None, timeout=1)
        except Exception:
            pass
    finally:
        with active_lock:
            if active_job_id == job_id:
                active_job_id = None


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VieNeu-TTS</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    max-width: 720px; margin: 2rem auto; padding: 0 1rem;
    background: #f8f9fa; color: #212529;
  }
  h1 { margin-bottom: 0.25rem; }
  h1 small { font-weight: normal; color: #6c757d; font-size: 0.5em; }
  .card {
    background: #fff; border-radius: 8px; padding: 1.25rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 1rem;
  }
  .card h2 { margin-top: 0; font-size: 1.1rem; }
  label { display: block; font-weight: 600; margin-bottom: 0.25rem; margin-top: 0.75rem; }
  label:first-child { margin-top: 0; }
  select, textarea, input[type=number] {
    width: 100%; padding: 0.5rem; border: 1px solid #ced4da; border-radius: 4px;
    font-size: 0.95rem; font-family: inherit;
  }
  textarea { resize: vertical; min-height: 100px; }
  button {
    padding: 0.5rem 1.25rem; border: none; border-radius: 4px;
    font-size: 0.95rem; cursor: pointer; margin-top: 0.75rem;
    font-weight: 600;
  }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-primary { background: #0d6efd; color: #fff; }
  .btn-primary:hover:not(:disabled) { background: #0b5ed7; }
  .btn-success { background: #198754; color: #fff; }
  .btn-success:hover:not(:disabled) { background: #157347; }
  .status {
    margin-top: 0.75rem; padding: 0.5rem 0.75rem; border-radius: 4px;
    font-size: 0.9rem; display: none;
  }
  .status.info { display: block; background: #cff4fc; color: #055160; }
  .status.success { display: block; background: #d1e7dd; color: #0f5132; }
  .status.error { display: block; background: #f8d7da; color: #842029; }
  audio { width: 100%; margin-top: 0.75rem; }
  .row { display: flex; gap: 0.75rem; }
  .row > * { flex: 1; }
  .separator { border: none; border-top: 1px solid #dee2e6; margin: 1rem 0; }
  .tabs { display: flex; gap: 0.5rem; margin-bottom: 0.75rem; }
  .tab {
    padding: 0.4rem 0.75rem; border-radius: 4px; cursor: pointer;
    background: #e9ecef; border: 1px solid #ced4da; font-size: 0.9rem;
  }
  .tab.active { background: #0d6efd; color: #fff; border-color: #0d6efd; }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .btn-download {
    display: inline-block; margin-top: 0.5rem; padding: 0.4rem 1rem;
    background: #6c757d; color: #fff; border-radius: 4px;
    text-decoration: none; font-size: 0.9rem; font-weight: 600;
  }
  .btn-download:hover { background: #565e64; }
  .btn-clear { background: #dc3545; color: #fff; }
  .btn-clear:hover { background: #bb2d3b; }
  .text-row { margin-bottom: 0.75rem; }
  .text-row-input { display: flex; gap: 0.4rem; align-items: center; }
  .text-row-input textarea { flex: 1; min-width: 0; min-height: 5rem; }
  .text-row-input .row-btns { display: flex; flex-direction: column; gap: 0.25rem; width: 12.5%; min-width: 60px; }
  .text-row-input .row-btns button { font-size: 0.95rem; padding: 0.35rem; }
  .text-row .row-result { margin-top: 0.35rem; }
  .text-row audio { width: 100%; margin-top: 0.25rem; }
  .text-row .btn-download { margin-top: 0.25rem; }
</style>
</head>
<body>

<!-- Model loading card -->
<div class="card">
  <div class="row">
    <div>
      <label for="sel-backbone">Backbone</label>
      <select id="sel-backbone"></select>
    </div>
    <div>
      <label for="sel-codec">Codec</label>
      <select id="sel-codec"></select>
    </div>
  </div>
  <button class="btn-primary" id="btn-load" onclick="loadModel()">Load Model</button>
  <div id="model-status" class="status"></div>
</div>

<!-- Synthesis card -->
<div class="card">
  <div class="tabs">
    <div class="tab active" data-tab="preset" onclick="switchTab('preset')">Preset Voice</div>
    <div class="tab" data-tab="clone" onclick="switchTab('clone')">Voice Cloning</div>
  </div>

  <div id="panel-preset" class="tab-panel active">
    <div class="row">
      <div>
        <label for="sel-voice">Voice</label>
        <select id="sel-voice"><option value="">-- Load a model first --</option></select>
      </div>
      <div>
        <label for="inp-temp">Temperature</label>
        <input type="number" id="inp-temp" value="1.0" min="0.1" max="2.0" step="0.1">
      </div>
    </div>
  </div>

  <div id="panel-clone" class="tab-panel">
    <label for="inp-ref-audio">Reference Audio (3-5s WAV)</label>
    <input type="file" id="inp-ref-audio" accept="audio/*">
    <label for="inp-ref-text">Reference Text</label>
    <textarea id="inp-ref-text" rows="2" placeholder="Transcription of the reference audio..."></textarea>
  </div>

  <hr class="separator">

  <div id="text-rows"></div>
  <div style="display:flex;gap:0.5rem;margin-top:0.5rem">
    <button class="btn-primary" onclick="addRow()">+ Add</button>
    <button class="btn-primary" onclick="preprocessAll()">Preprocess</button>
    <button class="btn-success" id="btn-gen-all" onclick="generateAll()">Generate All</button>
    <button class="btn-success" onclick="downloadAll()">Download All</button>
    <button class="btn-clear" onclick="clearAll()">Clear All</button>

  </div>
</div>

<script>
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
const streamContexts = {};   // rowId -> AudioContext (for PCM streaming)

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
        <button class="btn-clear" onclick="clearRow('${rowId}')">Clear</button>
        <button class="btn-success row-gen" onclick="generateRow('${rowId}')">Gen</button>
      </div>
    </div>
    <div class="row-result">
      <div class="status" data-role="status"></div>
      <audio controls style="display:none" data-role="player"></audio>
      <a class="btn-download" style="display:none" download="vieneu_output.wav" data-role="dl">Download</a>
    </div>`;
  container.appendChild(div);
  saveState();
  return rowId;
}

function stopStream(rowId) {
  if (streamContexts[rowId]) {
    try { streamContexts[rowId].close(); } catch {}
    delete streamContexts[rowId];
  }
  // Revoke any blob URL to free memory
  const el = getRowEl(rowId);
  if (el && el.player.src && el.player.src.startsWith('blob:')) {
    URL.revokeObjectURL(el.player.src);
  }
}

function clearRow(rowId) {
  stopStream(rowId);
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
    el.dl.style.display = 'none';
  }
  saveState();
}

function clearAll() {
  // Stop all poll timers and streams
  for (const id of Object.keys(pollTimers)) { clearInterval(pollTimers[id]); delete pollTimers[id]; }
  for (const id of Object.keys(streamContexts)) stopStream(id);
  // Remove all rows except keep one empty
  document.getElementById('text-rows').innerHTML = '';
  saveJobMap({});
  addRow('');
}

function downloadAll() {
  const jobMap = getJobMap();
  let i = 0;
  for (const [rowId, jobId] of Object.entries(jobMap)) {
    const el = getRowEl(rowId);
    if (!el || el.dl.style.display === 'none') continue;
    i++;
    setTimeout(() => {
      const a = document.createElement('a');
      a.href = `/api/audio/${jobId}`;
      a.download = `vieneu_${rowId}.wav`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }, i * 500);
  }
}

function preprocessAll() {
  document.querySelectorAll('.text-row').forEach(row => {
    const ta = row.querySelector('textarea');
    if (!ta) return;
    // Remove URLs, hashtags, emoji, repeated special chars
    let text = ta.value;
    text = text.replace(/https?:\/\/\S+/gi, '');
    text = text.replace(/#\S+/g, '');
    text = text.replace(/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{FE00}-\u{FE0F}\u{200D}\u{20E3}\u{E0020}-\u{E007F}]/gu, '');
    text = text.replace(/([^a-zA-Z0-9\s\u{00C0}-\u{024F}\u{1E00}-\u{1EFF}])\1{2,}/gu, '');
    text = text.replace(/[ \t]+/g, ' ');
    text = text.replace(/\n\s*\n+/g, '\n');
    text = text.trim();
    ta.value = text;
  });
  saveState();
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
    dl: row.querySelector('[data-role="dl"]'),
  };
}

function setStatus(el, cls, msg) {
  el.className = 'status ' + cls;
  el.textContent = msg;
}

// ---- WAV Blob builder (from raw PCM int16 LE) ----
function createWavBlob(pcmBytes, sampleRate) {
  const buf = new ArrayBuffer(44 + pcmBytes.length);
  const v = new DataView(buf);
  const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  v.setUint32(4, 36 + pcmBytes.length, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);   // PCM
  v.setUint16(22, 1, true);   // mono
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true);
  v.setUint16(32, 2, true);   // block align
  v.setUint16(34, 16, true);  // bits per sample
  writeStr(36, 'data');
  v.setUint32(40, pcmBytes.length, true);
  new Uint8Array(buf, 44).set(pcmBytes);
  return new Blob([buf], { type: 'audio/wav' });
}

// ---- PCM Streaming via Web Audio API ----
async function startPcmStream(rowId, jobId) {
  stopStream(rowId);

  const el = getRowEl(rowId);
  if (!el) return;

  const ctx = new AudioContext({ sampleRate: 24000 });
  streamContexts[rowId] = ctx;

  let nextTime = 0;
  let chunksScheduled = 0;
  const allPcmChunks = []; // Accumulate PCM bytes for WAV blob
  const BUFFER_CHUNKS = 5;
  const CHUNK_SAMPLES = 4800; // 200ms at 24kHz
  const CHUNK_BYTES = CHUNK_SAMPLES * 2; // int16 = 2 bytes per sample

  try {
    const resp = await fetch(`/api/stream/${jobId}`);
    if (!resp.ok || !resp.body) return;
    const reader = resp.body.getReader();
    let buf = new Uint8Array(0);

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      // Append incoming bytes to buffer
      const tmp = new Uint8Array(buf.length + value.length);
      tmp.set(buf);
      tmp.set(value, buf.length);
      buf = tmp;

      // Schedule complete 200ms audio chunks for real-time playback
      while (buf.length >= CHUNK_BYTES) {
        const slice = buf.slice(0, CHUNK_BYTES);
        buf = buf.slice(CHUNK_BYTES);
        allPcmChunks.push(slice); // Accumulate for WAV blob

        const int16 = new Int16Array(slice.buffer);
        const audioBuffer = ctx.createBuffer(1, CHUNK_SAMPLES, 24000);
        const ch = audioBuffer.getChannelData(0);
        for (let j = 0; j < CHUNK_SAMPLES; j++) ch[j] = int16[j] / 32768;

        const src = ctx.createBufferSource();
        src.buffer = audioBuffer;
        src.connect(ctx.destination);
        if (nextTime < ctx.currentTime) nextTime = ctx.currentTime;
        src.start(nextTime);
        nextTime += audioBuffer.duration;
        chunksScheduled++;
      }
    }

    // Process remaining samples
    if (buf.length >= 2) {
      const samples = Math.floor(buf.length / 2);
      const padded = buf.slice(0, samples * 2);
      allPcmChunks.push(padded);
      const int16 = new Int16Array(padded.buffer);
      const audioBuffer = ctx.createBuffer(1, samples, 24000);
      const ch = audioBuffer.getChannelData(0);
      for (let j = 0; j < samples; j++) ch[j] = int16[j] / 32768;

      const src = ctx.createBufferSource();
      src.buffer = audioBuffer;
      src.connect(ctx.destination);
      if (nextTime < ctx.currentTime) nextTime = ctx.currentTime;
      src.start(nextTime);
      nextTime += audioBuffer.duration;
    }

    // Build WAV blob from accumulated PCM and show player with seek bar
    if (allPcmChunks.length > 0) {
      const totalLen = allPcmChunks.reduce((s, c) => s + c.length, 0);
      const allPcm = new Uint8Array(totalLen);
      let off = 0;
      for (const chunk of allPcmChunks) { allPcm.set(chunk, off); off += chunk.length; }
      const wavBlob = createWavBlob(allPcm, 24000);
      el.player.src = URL.createObjectURL(wavBlob);
      el.player.style.display = 'block';
      // Don't auto-play — Web Audio API is already playing in real-time
    }

  } catch (e) {
    console.error('PCM stream error:', e);
  }
  // NOTE: AudioContext is NOT closed here — scheduled buffers continue playing.
  // Cleanup happens via stopStream() on next generate/clear.
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

  const [models, codecs] = await Promise.all([
    fetch('/api/models').then(r => r.json()),
    fetch('/api/codecs').then(r => r.json()),
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
  const voices = await fetch('/api/voices').then(r => r.json());
  const selV = document.getElementById('sel-voice');
  if (voices.length > 0) {
    selV.innerHTML = voices.map(v =>
      `<option value="${esc(v.id)}"${v.id === pickVoice ? ' selected' : ''}>${esc(v.description)} (${esc(v.id)})</option>`
    ).join('');
  }
  setStatus(document.getElementById('model-status'), 'success', 'Model preloaded and ready.');

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
      const r = await fetch(`/api/status/${jobId}`);
      if (r.ok) {
        const data = await r.json();
        if (data.status === 'done') {
          setStatus(el.st, 'success', data.progress || 'Done!');
          el.player.src = data.audio_url;
          el.player.style.display = 'block';
          el.dl.href = data.audio_url;
          el.dl.style.display = 'inline-block';
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
    const resp = await fetch('/api/load_model', {
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

    const voices = await fetch('/api/voices').then(r => r.json());
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

// ---- Per-row generate ----
async function generateRow(rowId) {
  const el = getRowEl(rowId);
  if (!el) return;
  stopStream(rowId);
  el.btn.disabled = true;
  el.player.style.display = 'none';
  el.dl.style.display = 'none';

  const presetActive = document.getElementById('panel-preset').classList.contains('active');
  const text = el.textarea.value.trim();
  if (!text) { setStatus(el.st, 'error', 'Please enter text'); el.btn.disabled = false; return; }

  try {
    let resp;
    if (presetActive) {
      resp = await fetch('/api/synthesize', {
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
      resp = await fetch('/api/synthesize', { method: 'POST', body: fd });
    }

    const data = await resp.json();
    if (resp.status === 503 && data.busy) {
      setStatus(el.st, 'error', data.error + (data.active_progress ? ' (' + data.active_progress + ')' : ''));
      el.btn.disabled = false;
      return;
    }
    if (!resp.ok) throw new Error(data.error || 'Failed');

    const jobMap = getJobMap(); jobMap[rowId] = data.job_id; saveJobMap(jobMap);
    setStatus(el.st, 'info', 'Processing...');
    startPcmStream(rowId, data.job_id); // Fire-and-forget: streams audio via Web Audio API
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
    el.btn.disabled = true;
    el.player.style.display = 'none';
    el.dl.style.display = 'none';

    const presetActive = document.getElementById('panel-preset').classList.contains('active');
    const text = el.textarea.value.trim();
    if (!text) { el.btn.disabled = false; resolve(); return; }

    try {
      let resp;
      if (presetActive) {
        resp = await fetch('/api/synthesize', {
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
        resp = await fetch('/api/synthesize', { method: 'POST', body: fd });
      }

      const data = await resp.json();
      if (resp.status === 503 && data.busy) {
        setStatus(el.st, 'error', data.error);
        el.btn.disabled = false; resolve(); return;
      }
      if (!resp.ok) throw new Error(data.error || 'Failed');

      const jobMap = getJobMap(); jobMap[rowId] = data.job_id; saveJobMap(jobMap);
      setStatus(el.st, 'info', 'Processing...');
      startPcmStream(rowId, data.job_id); // Fire-and-forget: streams audio via Web Audio API
      pollRowAsync(rowId, data.job_id, resolve);
    } catch (e) {
      setStatus(el.st, 'error', 'Error: ' + e.message);
      el.btn.disabled = false; resolve();
    }
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
      const r = await fetch(`/api/status/${jobId}`);
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
        // Revoke blob URL before switching to server WAV
        if (el.player.src && el.player.src.startsWith('blob:')) URL.revokeObjectURL(el.player.src);
        el.player.src = data.audio_url;
        el.player.style.display = 'block';
        el.dl.href = data.audio_url;
        el.dl.style.display = 'inline-block';
        el.btn.disabled = false; onDone();
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
      const r = await fetch(`/api/status/${jobId}`);
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
        // Revoke blob URL before switching to server WAV
        if (el.player.src && el.player.src.startsWith('blob:')) URL.revokeObjectURL(el.player.src);
        el.player.src = data.audio_url;
        el.player.style.display = 'block';
        el.dl.href = data.audio_url;
        el.dl.style.display = 'inline-block';
        el.btn.disabled = false;
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

init();
</script>
</body>
</html>"""


@app.get("/")
def index():
    return render_template_string(HTML_TEMPLATE)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def preload_model():
    """Load default model at startup so it's ready when the UI opens."""
    global tts, model_loaded, current_backbone, current_codec
    import torch
    from vieneu import VieNeuTTS

    backbone_cfg = BACKBONE_CONFIGS[DEFAULT_BACKBONE]
    codec_cfg = CODEC_CONFIGS[DEFAULT_CODEC]

    backbone_device = "cpu"
    if "gguf" not in backbone_cfg["repo"].lower():
        if sys.platform == "darwin":
            backbone_device = "mps" if torch.backends.mps.is_available() else "cpu"
        else:
            backbone_device = "cuda" if torch.cuda.is_available() else "cpu"

    codec_device = "cpu"  # ONNX codec always CPU

    print(f"Preloading: {backbone_cfg['repo']} ({backbone_device}) + {codec_cfg['repo']} ({codec_device})")
    tts = VieNeuTTS(
        backbone_repo=backbone_cfg["repo"],
        backbone_device=backbone_device,
        codec_repo=codec_cfg["repo"],
        codec_device=codec_device,
    )
    model_loaded = True
    current_backbone = DEFAULT_BACKBONE
    current_codec = DEFAULT_CODEC
    print("Model preloaded and ready.")


if __name__ == "__main__":
    preload_model()
    app.run(host="127.0.0.1", port=5000, debug=False)
