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
import yaml
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template_string

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
tts = None
model_loaded = False
current_backbone = None
current_codec = None

# In-memory job store: {job_id: {status, progress, audio_path, error}}
jobs = {}

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
    if not model_loaded or tts is None:
        return jsonify({"error": "Model not loaded"}), 400

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
    jobs[job_id] = {"status": "pending", "progress": "Queued", "audio_path": None, "error": None}

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
    return jsonify(resp)


@app.get("/api/audio/<job_id>")
def get_audio(job_id):
    job = jobs.get(job_id)
    if job is None or job["audio_path"] is None:
        return jsonify({"error": "Audio not available"}), 404
    return send_file(job["audio_path"], mimetype="audio/wav", as_attachment=False)


# ---------------------------------------------------------------------------
# Background synthesis worker
# ---------------------------------------------------------------------------

def _run_synthesis(job_id, text, voice_id, ref_audio_path, ref_text, temperature):
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

        chunks = split_text_into_chunks(text, max_chars=256)
        total = len(chunks)
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

        if not all_wavs:
            job["status"] = "error"
            job["error"] = "No audio generated"
            return

        job["progress"] = f"Joining {total} chunks..."
        audio = join_audio_chunks(all_wavs, sr=tts.sample_rate, silence_p=0.15)

        # Save to temp WAV
        import soundfile as sf

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        sf.write(tmp.name, audio, tts.sample_rate)
        tmp.close()

        job["audio_path"] = tmp.name
        job["status"] = "done"
        job["progress"] = f"Done — {total} chunks"

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


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
    <label for="sel-voice">Voice</label>
    <select id="sel-voice"><option value="">-- Load a model first --</option></select>
  </div>

  <div id="panel-clone" class="tab-panel">
    <label for="inp-ref-audio">Reference Audio (3-5s WAV)</label>
    <input type="file" id="inp-ref-audio" accept="audio/*">
    <label for="inp-ref-text">Reference Text</label>
    <textarea id="inp-ref-text" rows="2" placeholder="Transcription of the reference audio..."></textarea>
  </div>

  <hr class="separator">

  <label for="inp-text">Text</label>
  <textarea id="inp-text" rows="4" placeholder="Nhập văn bản tiếng Việt..."></textarea>

  <div class="row">
    <div>
      <label for="inp-temp">Temperature</label>
      <input type="number" id="inp-temp" value="1.0" min="0.1" max="2.0" step="0.1">
    </div>
    <div style="display:flex;align-items:flex-end">
      <button class="btn-success" id="btn-generate" onclick="generate()" style="width:100%">Generate</button>
    </div>
  </div>

  <div id="gen-status" class="status"></div>
  <audio id="audio-player" controls style="display:none"></audio>
  <a id="btn-download" class="btn-download" style="display:none" download="vieneu_output.wav">Download WAV</a>
</div>

<script>
// ---- Init ----
let pollTimer = null;

const DEFAULT_BACKBONE = "VieNeu-TTS-0.3B-q4-gguf";
const DEFAULT_CODEC = "NeuCodec ONNX (Fast CPU)";
const DEFAULT_VOICE = "Binh";

async function init() {
  const [models, codecs] = await Promise.all([
    fetch('/api/models').then(r => r.json()),
    fetch('/api/codecs').then(r => r.json()),
  ]);

  const selB = document.getElementById('sel-backbone');
  selB.innerHTML = models.map(m =>
    `<option value="${esc(m.name)}" title="${esc(m.description)}"${m.name === DEFAULT_BACKBONE ? ' selected' : ''}>${esc(m.name)}</option>`
  ).join('');

  const selC = document.getElementById('sel-codec');
  selC.innerHTML = codecs.map(c =>
    `<option value="${esc(c.name)}" title="${esc(c.description)}"${c.name === DEFAULT_CODEC ? ' selected' : ''}>${esc(c.name)}</option>`
  ).join('');

  // Model is preloaded — fetch voices and set status
  const voices = await fetch('/api/voices').then(r => r.json());
  const selV = document.getElementById('sel-voice');
  if (voices.length > 0) {
    selV.innerHTML = voices.map(v =>
      `<option value="${esc(v.id)}"${v.id === DEFAULT_VOICE ? ' selected' : ''}>${esc(v.description)} (${esc(v.id)})</option>`
    ).join('');
  }
  setStatus(document.getElementById('model-status'), 'success', 'Model preloaded and ready.');
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ---- Tabs ----
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.getElementById('panel-preset').classList.toggle('active', tab === 'preset');
  document.getElementById('panel-clone').classList.toggle('active', tab === 'clone');
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

    // Refresh voices
    const voices = await fetch('/api/voices').then(r => r.json());
    const selV = document.getElementById('sel-voice');
    if (voices.length > 0) {
      selV.innerHTML = voices.map(v =>
        `<option value="${esc(v.id)}">${esc(v.description)} (${esc(v.id)})</option>`
      ).join('');
    } else {
      selV.innerHTML = '<option value="">No preset voices available</option>';
    }
  } catch (e) {
    setStatus(st, 'error', 'Error: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

// ---- Generate ----
async function generate() {
  const btn = document.getElementById('btn-generate');
  const st = document.getElementById('gen-status');
  const player = document.getElementById('audio-player');
  const dlBtn = document.getElementById('btn-download');
  btn.disabled = true;
  player.style.display = 'none';
  dlBtn.style.display = 'none';

  const presetActive = document.getElementById('panel-preset').classList.contains('active');
  const text = document.getElementById('inp-text').value.trim();
  if (!text) { setStatus(st, 'error', 'Please enter text'); btn.disabled = false; return; }

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
    if (!resp.ok) throw new Error(data.error || 'Failed');

    setStatus(st, 'info', 'Processing...');
    pollJob(data.job_id, st, player, dlBtn, btn);
  } catch (e) {
    setStatus(st, 'error', 'Error: ' + e.message);
    btn.disabled = false;
  }
}

function pollJob(jobId, st, player, dlBtn, btn) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const data = await fetch(`/api/status/${jobId}`).then(r => r.json());
      if (data.status === 'processing' || data.status === 'pending') {
        setStatus(st, 'info', data.progress || 'Processing...');
      } else if (data.status === 'done') {
        clearInterval(pollTimer);
        pollTimer = null;
        setStatus(st, 'success', data.progress || 'Done!');
        player.src = data.audio_url;
        player.style.display = 'block';
        dlBtn.href = data.audio_url;
        dlBtn.style.display = 'inline-block';
        btn.disabled = false;
      } else if (data.status === 'error') {
        clearInterval(pollTimer);
        pollTimer = null;
        setStatus(st, 'error', 'Error: ' + (data.error || 'Unknown'));
        btn.disabled = false;
      }
    } catch (e) {
      clearInterval(pollTimer);
      pollTimer = null;
      setStatus(st, 'error', 'Polling error: ' + e.message);
      btn.disabled = false;
    }
  }, 1000);
}

function setStatus(el, cls, msg) {
  el.className = 'status ' + cls;
  el.textContent = msg;
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
