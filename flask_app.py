"""
VieNeu-TTS Flask App — Simple TTS web interface with polling.

Run:  uv run --with flask flask_app.py
Open: http://127.0.0.1:5008
"""

import os
import sys
import uuid
import tempfile
import threading
import time
import queue
import subprocess
import logging
import socket
import concurrent.futures
import yaml
import requests
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
# Disable Werkzeug verbose logs
logging.getLogger('werkzeug').setLevel(logging.WARNING)

from flask import Flask, request, jsonify, send_file, render_template, Response, url_for
import pymupdf as fitz
  # PyMuPDF
import json
import tempfile
import shutil
import subprocess

app = Flask(__name__)

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.route("/", methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def handle_preflight(path=""):
    resp = Response()
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

@app.route("/external-ip", methods=["GET"])
def external_ip():
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
        return jsonify({"ip": ip})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
tts = None
model_loaded = False
current_backbone = None
current_codec = None

# In-memory job store: {job_id: {status, progress, audio_path, error, ...}}
jobs = {}

# OCR progress tracking: {pdf_id: {total_pages, processed_pages, current_page, status, error}}
ocr_progress = {}

# Lazy EasyOCR reader (fallback for scanned/image-based PDFs)
_easyocr_reader = None
_easyocr_lock = threading.Lock()


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is not None:
        return _easyocr_reader
    with _easyocr_lock:
        if _easyocr_reader is None:
            import easyocr
            _easyocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
            logging.info("[OCR-FALLBACK] EasyOCR reader initialized")
    return _easyocr_reader


def _ocr_fallback_easyocr(pdf_id: str, page_num: int) -> list:
    """EasyOCR fallback when opendataloader extracts no text (scanned PDF)."""
    img_path = IMAGE_EXPORT_DIR / pdf_id / f"page_{page_num}.png"
    if not img_path.exists():
        logging.warning("[OCR-FALLBACK] PNG not found: %s", img_path)
        return []
    try:
        reader = _get_easyocr_reader()
        results = reader.readtext(str(img_path))

        def _flat_bbox(bbox):
            xs = [float(p[0]) for p in bbox]
            ys = [float(p[1]) for p in bbox]
            return [min(xs), min(ys), max(xs), max(ys)]

        lines = []
        for bbox, text, _conf in results:
            if text.strip():
                lines.append({"bbox": _flat_bbox(bbox), "text": text.strip()})

        if not lines:
            return []

        lines.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))

        # Group lines into paragraphs: gap > 30px = new paragraph
        groups = []
        cur = [lines[0]]
        for line in lines[1:]:
            if line["bbox"][1] - cur[-1]["bbox"][3] > 30:
                groups.append(cur)
                cur = [line]
            else:
                cur.append(line)
        groups.append(cur)

        elements = []
        for idx, group in enumerate(groups, start=1):
            bboxes = [l["bbox"] for l in group]
            merged = [min(b[0] for b in bboxes), min(b[1] for b in bboxes),
                      max(b[2] for b in bboxes), max(b[3] for b in bboxes)]
            elements.append({
                "type": "paragraph",
                "id": idx,
                "page number": page_num,
                "bounding box": merged,
                "font": "Unknown",
                "font size": 12.0,
                "text color": "[0.0, 0.0, 0.0]",
                "content": " ".join(l["text"] for l in group),
                "translation": None,
            })

        logging.info("[OCR-FALLBACK] EasyOCR produced %d paragraphs for page %d", len(elements), page_num)
        return elements
    except Exception as e:
        logging.error("[OCR-FALLBACK] EasyOCR failed on page %d: %s", page_num, e, exc_info=True)
        return []


def _save_and_queue_elements(pdf_id: str, page_num: int, elements: list, cache_path, total_pages: int):
    """Cache elements to disk and queue background translation."""
    page_key = f"{pdf_id}_{page_num}"
    with translation_queue_lock:
        already_queued = any(k.startswith(f"{page_key}_") for k in translation_queue.keys())
    if not already_queued:
        threading.Thread(target=translate_elements_background, args=(pdf_id, page_num, elements), daemon=True).start()
    tmp_path = Path(str(cache_path) + ".tmp")
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(elements, f, ensure_ascii=False, indent=2)
    tmp_path.replace(cache_path)  # atomic on same filesystem
    ocr_progress[pdf_id]["processed_pages"] = ocr_progress[pdf_id].get("processed_pages", 0) + 1
    ocr_progress[pdf_id]["status"] = "idle"
    logging.info("[OCR] Cached+queued page %d (%d/%d)", page_num, ocr_progress[pdf_id]["processed_pages"], total_pages)

# Translation model (lazy load)
translation_model = None
translation_tokenizer = None

# Translation queue: {f"{pdf_id}_{page_num}_{element_id}": {"status": "pending/processing/done/error", "error": None}}
# Also track page-level status for backwards compatibility: {f"{pdf_id}_{page_num}": "idle/processing/done"}
translation_queue = {}
translation_queue_lock = threading.Lock()
page_translation_status = {}  # Track page-level completion for UI purposes
translate_all_jobs = {}  # {pdf_id: {status, total, done, failed}}

# Base directory for saving user audio outputs
OUTPUTS_DIR = Path(__file__).parent / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

import re as _re
def _safe_username(name: str) -> str:
    """Sanitize username to a safe directory name."""
    name = _re.sub(r'[^\w\-]', '_', name.strip())
    return name[:64] or "anonymous"

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
        username = request.form.get("username", "")
        audio_name = request.form.get("audio_name", "").strip()
        ref_audio_file = request.files.get("ref_audio")
    else:
        data = request.get_json()
        text = data.get("text", "").strip()
        voice_id = data.get("voice_id", "")
        ref_text = data.get("ref_text", "")
        temperature = data.get("temperature", 1.0)
        username = data.get("username", "")
        audio_name = data.get("audio_name", "").strip()
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
        "cancelled": False,
    }

    with active_lock:
        active_job_id = job_id

    thread = threading.Thread(
        target=_run_synthesis,
        args=(job_id, text, voice_id, ref_audio_path, ref_text, temperature, _safe_username(username), audio_name),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.get("/api/busy")
def check_busy():
    if not model_loaded:
        return jsonify({"busy": True, "reason": "Model loading..."})
    with active_lock:
        if active_job_id is not None:
            job = jobs.get(active_job_id, {})
            if job.get("status") in ("pending", "processing"):
                return jsonify({"busy": True, "active_progress": job.get("progress", "")})
    return jsonify({"busy": False})


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


def _wav_duration(path):
    """Return duration in seconds of a WAV file without external deps."""
    import wave
    try:
        with wave.open(str(path), 'r') as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return None


@app.get("/api/history")
def get_history():
    username = _safe_username(request.args.get("username", "anonymous"))
    user_dir = OUTPUTS_DIR / username
    if not user_dir.exists():
        return jsonify([])
    files = sorted(user_dir.glob("*.wav"), key=lambda f: f.stat().st_mtime, reverse=True)
    result = []
    for f in files:
        st = f.stat()
        dur = _wav_duration(f)
        entry = {
            "filename": f.name,
            "url": f"/api/history/file/{username}/{f.name}",
            "duration": round(dur, 1) if dur is not None else None,
            "timestamp": st.st_mtime,
        }
        if f.with_suffix(".txt").exists():
            entry["text_url"] = f"/api/history/text/{username}/{f.stem}"
        result.append(entry)
    return jsonify(result)


@app.get("/api/history/file/<username>/<filename>")
def get_history_file(username, filename):
    username = _safe_username(username)
    path = OUTPUTS_DIR / username / filename
    if not path.exists() or path.suffix != ".wav":
        return jsonify({"error": "File not found"}), 404
    return send_file(str(path), mimetype="audio/wav", as_attachment=False)


@app.get("/api/history/text/<username>/<stem>")
def get_history_text(username, stem):
    username = _safe_username(username)
    path = OUTPUTS_DIR / username / (stem + ".txt")
    if not path.exists():
        return jsonify({"error": "Text file not found"}), 404
    return send_file(str(path), mimetype="text/plain; charset=utf-8", as_attachment=False)


@app.post("/api/history/rename/<username>/<filename>")
def rename_history_file(username, filename):
    username = _safe_username(username)
    new_name = (request.get_json() or {}).get("new_name", "").strip()
    if not new_name:
        return jsonify({"error": "new_name is required"}), 400
    # Ensure .wav extension and safe name
    new_name = _re.sub(r'[^\w\-. ]', '_', new_name)
    if not new_name.lower().endswith(".wav"):
        new_name += ".wav"
    src = OUTPUTS_DIR / username / filename
    dst = OUTPUTS_DIR / username / new_name
    if not src.exists():
        return jsonify({"error": "File not found"}), 404
    if dst.exists():
        return jsonify({"error": "Name already exists"}), 409
    src.rename(dst)

    # Rename companion text file if it exists
    src_txt = src.with_suffix(".txt")
    if src_txt.exists():
        dst_txt = dst.with_suffix(".txt")
        src_txt.rename(dst_txt)

    return jsonify({"ok": True, "filename": new_name})


@app.delete("/api/history/file/<username>/<filename>")
def delete_history_file(username, filename):
    username = _safe_username(username)
    path = OUTPUTS_DIR / username / filename
    if not path.exists() or not path.is_file():
        return jsonify({"error": "File not found"}), 404
    path.unlink()

    # Delete companion text file if it exists
    txt_path = path.with_suffix(".txt")
    if txt_path.exists():
        txt_path.unlink()

    return jsonify({"ok": True})


@app.post("/api/history/move/<username>/<filename>")
def move_history_file(username, filename):
    username = _safe_username(username)
    direction = (request.get_json() or {}).get("direction", "").lower()
    if direction not in ("up", "down"):
        return jsonify({"error": "direction must be 'up' or 'down'"}), 400

    path = OUTPUTS_DIR / username / filename
    if not path.exists() or not path.is_file():
        return jsonify({"error": "File not found"}), 404

    # Get sorted list of files (newest first, same as history display)
    files = sorted((OUTPUTS_DIR / username).glob("*.wav"), key=lambda f: f.stat().st_mtime, reverse=True)
    file_list = [f for f in files]

    # Find current file index
    try:
        idx = next(i for i, f in enumerate(file_list) if f.name == filename)
    except StopIteration:
        return jsonify({"error": "File not found in list"}), 404

    # Determine swap index
    if direction == "up":
        swap_idx = idx - 1  # Swap with file above (newer)
    else:  # down
        swap_idx = idx + 1  # Swap with file below (older)

    if swap_idx < 0 or swap_idx >= len(file_list):
        return jsonify({"error": "Cannot move further"}), 400

    # Swap mtimes
    swap_file = file_list[swap_idx]
    current_mtime = path.stat().st_mtime
    swap_mtime = swap_file.stat().st_mtime

    os.utime(path, (current_mtime, swap_mtime))
    os.utime(swap_file, (swap_mtime, current_mtime))

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Trash (deleted texts) — one .txt file per entry in outputs/<user>/trash/
# ---------------------------------------------------------------------------
_MAX_TRASH = 100


def _trash_dir(user_dir: Path) -> Path:
    d = user_dir / "trash"
    d.mkdir(exist_ok=True)
    return d


@app.get("/api/trash")
def get_trash():
    username = _safe_username(request.args.get("username", "anonymous"))
    user_dir = OUTPUTS_DIR / username
    user_dir.mkdir(exist_ok=True)
    td = _trash_dir(user_dir)
    files = sorted(td.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
    result = []
    for f in files:
        result.append({
            "id": f.stem,
            "text": f.read_text(encoding="utf-8"),
            "timestamp": f.stat().st_mtime,
        })
    return jsonify(result)


@app.post("/api/trash")
def add_trash():
    data = request.get_json() or {}
    username = _safe_username(data.get("username", "anonymous"))
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": True})
    user_dir = OUTPUTS_DIR / username
    user_dir.mkdir(exist_ok=True)
    td = _trash_dir(user_dir)
    # Write new entry
    entry_id = str(uuid.uuid4())
    (td / f"{entry_id}.txt").write_text(text, encoding="utf-8")
    # Prune oldest beyond limit
    files = sorted(td.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in files[_MAX_TRASH:]:
        old.unlink(missing_ok=True)
    return jsonify({"ok": True, "id": entry_id})


@app.patch("/api/trash/<item_id>")
def update_trash(item_id):
    data = request.get_json() or {}
    username = _safe_username(data.get("username", "anonymous"))
    text = (data.get("text") or "").strip()
    safe_id = _re.sub(r'[^\w\-]', '', item_id)
    td = _trash_dir(OUTPUTS_DIR / username)
    p = td / f"{safe_id}.txt"
    if text:
        p.write_text(text, encoding="utf-8")
    elif p.exists():
        p.unlink()
    return jsonify({"ok": True})


@app.delete("/api/trash/<item_id>")
def delete_trash(item_id):
    username = _safe_username(request.args.get("username", "anonymous"))
    # Sanitize: item_id should be a UUID (no path separators)
    safe_id = _re.sub(r'[^\w\-]', '', item_id)
    td = _trash_dir(OUTPUTS_DIR / username)
    p = td / f"{safe_id}.txt"
    if p.exists():
        p.unlink()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# History merge
# ---------------------------------------------------------------------------
@app.post("/api/history/merge")
def merge_history():
    import wave as _wave
    data = request.get_json() or {}
    username = _safe_username(data.get("username", "anonymous"))
    files = data.get("files", [])
    output_name = (data.get("output_name") or "merged").strip()
    if not files:
        return jsonify({"error": "No files provided"}), 400
    user_dir = OUTPUTS_DIR / username
    paths = []
    for fname in files:
        safe = _re.sub(r'[^\w\-. ]', '_', fname)
        p = user_dir / safe
        if not p.exists() or p.suffix.lower() != ".wav":
            return jsonify({"error": f"File not found: {fname}"}), 404
        paths.append(p)
    output_name = _re.sub(r'[^\w\-. ]', '_', output_name)
    if not output_name.lower().endswith(".wav"):
        output_name += ".wav"
    out_path = user_dir / output_name
    try:
        with _wave.open(str(paths[0]), 'rb') as w0:
            params = w0.getparams()
        with _wave.open(str(out_path), 'wb') as wout:
            wout.setparams(params)
            for p in paths:
                with _wave.open(str(p), 'rb') as wi:
                    wout.writeframes(wi.readframes(wi.getnframes()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "ok": True,
        "filename": output_name,
        "url": f"/api/history/file/{username}/{output_name}",
    })


@app.post("/api/cancel/<job_id>")
def cancel_job(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    job["cancelled"] = True
    # Kill any running ffmpeg stream process
    proc = job.get("ffmpeg_proc")
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    # Unblock the feeder thread by signalling end-of-stream
    pcm_q = job.get("pcm_queue")
    if pcm_q:
        try:
            pcm_q.put_nowait(None)
        except queue.Full:
            pass
    return jsonify({"ok": True})


@app.get("/api/stream/<job_id>")
def stream_audio(job_id):
    """Stream audio as WebM/Opus for MediaSource API consumption."""
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404

    pcm_queue = job.get("pcm_queue")
    if pcm_queue is None:
        return jsonify({"error": "No stream available"}), 404

    proc = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "64k",
            "-f", "webm", "pipe:1",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    job["ffmpeg_proc"] = proc

    def feed_pcm():
        try:
            while True:
                if job.get("cancelled"):
                    break
                try:
                    data = pcm_queue.get(timeout=2)
                except queue.Empty:
                    if job.get("cancelled"):
                        break
                    continue
                if data is None:
                    break
                proc.stdin.write(data)
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    feeder = threading.Thread(target=feed_pcm, daemon=True)
    feeder.start()

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
            proc.wait()
            job.pop("ffmpeg_proc", None)

    return Response(generate(), mimetype="audio/webm",
                    headers={"Cache-Control": "no-cache",
                             "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------------------
# Background synthesis worker
# ---------------------------------------------------------------------------

def _run_synthesis(job_id, text, voice_id, ref_audio_path, ref_text, temperature, username="anonymous", audio_name=""):
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
        chunk_times = []
        job_start = time.time()

        logging.info("Job %s started — %d chars, %d chunk(s)", job_id[:8], len(text), total)

        for i, chunk in enumerate(chunks, 1):
            if job.get("cancelled"):
                job["status"] = "error"
                job["error"] = "Cancelled"
                try:
                    job["pcm_queue"].put(None, timeout=1)
                except Exception:
                    pass
                elapsed = time.time() - job_start
                logging.info("Job %s cancelled after %.1fs (%d/%d chunks)", job_id[:8], elapsed, i - 1, total)
                return
            job["progress"] = f"Generating chunk {i}/{total}..."
            t0 = time.time()
            chunk_wav = tts.infer(
                text=chunk,
                ref_codes=ref_codes,
                ref_text=ref_text_resolved,
                temperature=temperature,
            )
            chunk_time = time.time() - t0
            chunk_times.append(chunk_time)
            if chunk_wav is not None and len(chunk_wav) > 0:
                chunk_dur = len(chunk_wav) / tts.sample_rate
                logging.info("  Chunk %d/%d: %d chars → %.1fs audio in %.1fs (RTF %.2f, %.1f chars/s)",
                             i, total, len(chunk), chunk_dur, chunk_time,
                             chunk_time / chunk_dur if chunk_dur > 0 else 0,
                             len(chunk) / chunk_dur if chunk_dur > 0 else 0)
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

        # Save joined final WAV to user's output directory
        user_dir = OUTPUTS_DIR / username
        user_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        if audio_name:
            safe_name = _re.sub(r'[^\w\-]', '_', audio_name)[:64]
            audio_path = str(user_dir / f"{safe_name}.wav")
        else:
            audio_path = str(user_dir / f"{timestamp}_{job_id[:8]}.wav")
        sf.write(audio_path, audio, tts.sample_rate)

        # Save original text alongside the audio
        txt_path = str(user_dir / f"{timestamp}_{job_id[:8]}.txt")
        with open(txt_path, "w", encoding="utf-8") as tf:
            tf.write(text)

        job["audio_path"] = audio_path
        job["status"] = "done"
        job["progress"] = f"Done — {total} chunks"

        # Log summary
        total_time = time.time() - job_start
        audio_dur = len(audio) / tts.sample_rate
        avg_chunk = sum(chunk_times) / len(chunk_times) if chunk_times else 0
        logging.info("Job %s done — %d chars, %.1fs audio, %d chunks, %.1fs total, %.1fs avg/chunk, RTF %.2f, %.1f chars/s",
                     job_id[:8], len(text), audio_dur, total, total_time, avg_chunk,
                     total_time / audio_dur if audio_dur > 0 else 0,
                     len(text) / audio_dur if audio_dur > 0 else 0)

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


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/yt")
def yt():
    return render_template("yt.html")


YT_DOWNLOAD_DIR = Path(__file__).parent / "downloads"
YT_DOWNLOAD_DIR.mkdir(exist_ok=True)
yt_jobs = {}  # job_id -> {status, progress, error, filename}


# ---------------------------------------------------------------------------
# PDF Reader & Conversion
# ---------------------------------------------------------------------------
PDF_UPLOAD_DIR = Path(__file__).parent / "static" / "uploads" / "pdfs"
IMAGE_EXPORT_DIR = Path(__file__).parent / "static" / "uploads" / "images"

PDF_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
@app.get("/read")
def read_pdf_page():
    # List available PDF folders
    uploads = []
    if IMAGE_EXPORT_DIR.exists():
        for d in sorted(IMAGE_EXPORT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir():
                # Try to find page_1.png for thumbnail
                thumb = None
                p1 = d / "page_1.png"
                if p1.exists():
                    thumb = f"/static/uploads/images/{d.name}/page_1.png"
                
                # Try to extract the original name (safe_stem + _ + pdf_id)
                name_parts = d.name.rsplit('_', 1)
                display_name = name_parts[0] if len(name_parts) > 1 else d.name
                
                uploads.append({
                    "id": d.name,
                    "name": display_name,
                    "thumbnail": thumb,
                    "mtime": d.stat().st_mtime
                })
    
    return render_template("read.html", uploads=uploads)

@app.get("/api/pdf_images/<pdf_id>")
def get_pdf_images(pdf_id):
    pdf_dir = IMAGE_EXPORT_DIR / pdf_id
    if not pdf_dir.exists() or not pdf_dir.is_dir():
        return jsonify({"error": "PDF not found"}), 404
    
    images = []
    def sort_key(p):
        match = _re.search(r'page_(\d+)', p.name)
        return int(match.group(1)) if match else 0
    
    files = sorted(pdf_dir.glob("page_*.png"), key=sort_key)
    
    for f in files:
        images.append(f"/static/uploads/images/{pdf_id}/{f.name}")
        
    return jsonify({
        "ok": True,
        "images": images
    })


@app.get("/viewer/<pdf_id>")
@app.get("/viewer/<pdf_id>/<int:page_num>")
def viewer(pdf_id, page_num=1):
    pdf_dir = IMAGE_EXPORT_DIR / pdf_id
    if not pdf_dir.exists() or not pdf_dir.is_dir():
        return "Not found", 404

    def sort_key(p):
        match = _re.search(r'page_(\d+)', p.name)
        return int(match.group(1)) if match else 0

    images = sorted(pdf_dir.glob("page_*.png"), key=sort_key)
    page_count = len(images)

    # Validate page number
    if page_num < 1 or page_num > page_count:
        page_num = 1

    # Get display name
    name_parts = pdf_id.rsplit('_', 1)
    display_name = name_parts[0] if len(name_parts) > 1 else pdf_id

    return render_template("viewer.html", pdf_id=pdf_id, display_name=display_name, page_count=page_count, start_page=page_num)

def load_translation_model():
    """Lazy load translation model."""
    global translation_model, translation_tokenizer
    if translation_model is not None:
        return

    try:
        logging.info("[TRANSLATE] Loading HY-MT1.5-1.8B model...")
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch

        model_path = Path(__file__).parent / "models" / "HY-MT1.5-1.8B"
        if not model_path.exists():
            logging.error("[TRANSLATE] Model path not found: %s", model_path)
            return False

        translation_tokenizer = AutoTokenizer.from_pretrained(str(model_path))
        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32

        translation_model = AutoModelForCausalLM.from_pretrained(
            str(model_path),
            dtype=dtype,
        ).to(device)

        logging.info("[TRANSLATE] Model loaded on device: %s", device)
        return True
    except Exception as e:
        logging.error("[TRANSLATE] Failed to load model: %s", str(e))
        return False


def translate_to_vietnamese(text: str) -> str:
    """Translate English text to Vietnamese."""
    if not text or not text.strip():
        return ""

    if translation_model is None:
        if not load_translation_model():
            logging.error("[TRANSLATE] Translation model not available")
            return text

    try:
        logging.info("[TRANSLATE] Translating %d chars (source: text input)", len(text))
        prompt = f"Translate the following segment into Vietnamese, without additional explanation.\n\n{text}"
        messages = [{"role": "user", "content": prompt}]

        tokenized_chat = translation_tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=False,
            return_tensors="pt",
        )

        outputs = translation_model.generate(
            tokenized_chat.to(translation_model.device),
            max_new_tokens=512,
            top_k=20,
            top_p=0.6,
            repetition_penalty=1.05,
            temperature=0.7,
        )

        output_text = translation_tokenizer.decode(outputs[0])
        # Extract translation (after the prompt)
        translation = output_text.split(text)[-1].strip()
        # Remove placeholder tokens
        translation = translation.replace("<｜hy_place▁holder▁no▁8｜>", "")
        translation = translation.replace("<｜hy_place▁holder▁no▁2｜>", "")
        translation = translation.strip()

        logging.info("[TRANSLATE] Translation complete: %d chars input → %d chars output",
                    len(text), len(translation))
        return translation
    except Exception as e:
        logging.error("[TRANSLATE] Translation error: %s", str(e))
        return text


def _save_translated_elements_to_cache(pdf_id: str, page_num: int, elements: list):
    """Save translated elements to ocr.json cache file."""
    try:
        pdf_dir = IMAGE_EXPORT_DIR / pdf_id
        ocr_cache_path = pdf_dir / f"page_{page_num}_ocr.json"
        with open(ocr_cache_path, 'w', encoding='utf-8') as f:
            json.dump(elements, f, ensure_ascii=False, indent=2)
        logging.info("[TRANSLATE_BG] Saved translations to %s", ocr_cache_path.name)
    except Exception as e:
        logging.error("[TRANSLATE_BG] Failed to save translations to cache: %s", str(e))


def _translate_node(node: dict) -> None:
    """Recursively translate content fields in an element node."""
    if node.get("content"):
        node["translation"] = translate_to_vietnamese(node["content"])
    for item in node.get("list items", []):
        _translate_node(item)
    for kid in node.get("kids", []):
        _translate_node(kid)


def translate_single_element(pdf_id: str, page_num: int, element_id: int, element: dict):
    """Translate single element (including nested list items/kids). Saves to cache immediately."""
    element_key = f"{pdf_id}_{page_num}_{element_id}"
    try:
        with translation_queue_lock:
            if element_key not in translation_queue:
                return
            translation_queue[element_key]["status"] = "processing"

        _translate_node(element)
        logging.info("[TRANSLATE_PARA] Translated element %d (type=%s) | Book: %s, Page: %d",
                     element_id, element.get("type", "?"), pdf_id, page_num)

        with translation_queue_lock:
            translation_queue[element_key]["status"] = "done"

        # Save updated element to cache (replace full element to preserve nested translations)
        pdf_dir = IMAGE_EXPORT_DIR / pdf_id
        ocr_cache_path = pdf_dir / f"page_{page_num}_ocr.json"
        try:
            with open(ocr_cache_path, 'r', encoding='utf-8') as f:
                cached_elements = json.load(f)
            for i, cached_el in enumerate(cached_elements):
                if cached_el.get("id") == element_id:
                    cached_elements[i] = element
                    break
            with open(ocr_cache_path, 'w', encoding='utf-8') as f:
                json.dump(cached_elements, f, ensure_ascii=False, indent=2)
            logging.debug("[TRANSLATE_PARA] Saved element %d to cache", element_id)
        except Exception as e:
            logging.warning("[TRANSLATE_PARA] Could not update cache for element %d: %s", element_id, str(e))

    except Exception as e:
        logging.error("[TRANSLATE_PARA] Error translating element %d: %s", element_id, str(e))
        with translation_queue_lock:
            translation_queue[element_key]["status"] = "error"
            translation_queue[element_key]["error"] = str(e)


def translate_elements_background(pdf_id: str, page_num: int, elements: list):
    """Background worker to queue translations for each element separately (paragraph-by-paragraph)."""
    page_key = f"{pdf_id}_{page_num}"

    logging.info("[TRANSLATE_BG] Queuing %d elements | Book: %s, Page: %d", len(elements), pdf_id, page_num)

    with translation_queue_lock:
        page_translation_status[page_key] = "processing"

    def _needs_translation(node):
        if node.get("content") and not node.get("translation"):
            return True
        return any(_needs_translation(i) for i in node.get("list items", []) + node.get("kids", []))

    # Queue each element for translation
    for element in elements:
        if not _needs_translation(element):
            continue  # Skip elements with no content at any nesting level

        element_id = element.get("id")
        element_key = f"{pdf_id}_{page_num}_{element_id}"

        with translation_queue_lock:
            if element_key not in translation_queue:
                translation_queue[element_key] = {
                    "status": "pending",
                    "error": None
                }

        # Start background thread for this element
        worker_thread = threading.Thread(
            target=translate_single_element,
            args=(pdf_id, page_num, element_id, element),
            daemon=True
        )
        worker_thread.start()

    logging.info("[TRANSLATE_BG] Queued all elements | Book: %s, Page: %d", pdf_id, page_num)

    # Mark page as done queuing (not translation complete, just queued)
    with translation_queue_lock:
        page_translation_status[page_key] = "processing"


@app.post("/api/translate")
def translate():
    """Translate text to Vietnamese."""
    data = request.get_json() or {}
    text = data.get("text", "").strip()

    if not text:
        return jsonify({"error": "No text provided"}), 400

    logging.info("[TRANSLATE] Translate request: %d chars", len(text))
    result = translate_to_vietnamese(text)

    return jsonify({
        "ok": True,
        "original": text,
        "translation": result
    })


@app.get("/api/ocr_progress/<pdf_id>")
def get_ocr_progress(pdf_id):
    """Get OCR progress for a PDF."""
    if not _re.match(r'^[\w\-]+$', pdf_id):
        return jsonify({"error": "Invalid pdf_id"}), 400

    progress_data = ocr_progress.get(pdf_id, {
        "total_pages": 0,
        "processed_pages": 0,
        "current_page": 0,
        "status": "idle",
        "error": None
    })
    return jsonify(progress_data)


@app.get("/api/ocr/<pdf_id>/<int:page_num>/dimensions")
def get_page_dimensions(pdf_id, page_num):
    """Get PDF page dimensions (width, height in points)."""
    if not _re.match(r'^[\w\-]+$', pdf_id):
        return jsonify({"error": "Invalid pdf_id"}), 400

    pdf_dir = IMAGE_EXPORT_DIR / pdf_id
    if not pdf_dir.exists():
        return jsonify({"error": "PDF not found"}), 404

    pdf_files = list(PDF_UPLOAD_DIR.glob(f"{pdf_id}.pdf"))
    if not pdf_files:
        pdf_files = list(PDF_UPLOAD_DIR.glob(f"*_{pdf_id}.pdf"))
    if not pdf_files:
        return jsonify({"error": "PDF file not found"}), 404

    try:
        doc = fitz.open(str(pdf_files[0]))
        if page_num < 1 or page_num > len(doc):
            doc.close()
            return jsonify({"error": "Page out of range"}), 400

        page = doc[page_num - 1]
        rect = page.rect
        doc.close()

        return jsonify({
            "width": rect.width,
            "height": rect.height,
            "page": page_num
        })
    except Exception as e:
        logging.error("Failed to get page dimensions: %s", str(e))
        return jsonify({"error": str(e)}), 500


@app.get("/api/ocr/<pdf_id>/<int:page_num>/status")
def get_translation_status(pdf_id, page_num):
    """Check translation status for a page. Returns element-level status and progress."""
    if not _re.match(r'^[\w\-]+$', pdf_id):
        return jsonify({"error": "Invalid pdf_id"}), 400

    page_key = f"{pdf_id}_{page_num}"

    with translation_queue_lock:
        # Get all element statuses for this page
        element_statuses = {}
        for key, status in translation_queue.items():
            if key.startswith(f"{page_key}_"):
                # Extract element_id from key format: "pdf_id_page_num_element_id"
                parts = key.rsplit("_", 1)
                if len(parts) == 2:
                    try:
                        element_id = int(parts[1])
                        element_statuses[element_id] = status["status"]
                    except ValueError:
                        pass

        if not element_statuses:
            return jsonify({"status": "not_queued", "elements": {}})

        # Calculate overall page status
        statuses = list(element_statuses.values())
        all_done = all(s == "done" for s in statuses)
        any_processing = any(s == "processing" for s in statuses)
        any_pending = any(s == "pending" for s in statuses)
        any_error = any(s == "error" for s in statuses)

        if all_done:
            overall_status = "done"
        elif any_processing or any_pending:
            overall_status = "processing"
        elif any_error:
            overall_status = "error"
        else:
            overall_status = "pending"

        done_count = sum(1 for s in statuses if s == "done")
        total_count = len(statuses)

        return jsonify({
            "status": overall_status,
            "elements": element_statuses,
            "progress": f"{done_count}/{total_count}"
        })


@app.get("/api/ocr/<pdf_id>/<int:page_num>/cached")
def get_ocr_cached(pdf_id, page_num):
    """Return cached OCR elements only — never triggers OCR or translation queuing."""
    if not _re.match(r'^[\w\-]+$', pdf_id):
        return jsonify({"error": "Invalid pdf_id"}), 400
    pdf_dir = IMAGE_EXPORT_DIR / pdf_id
    ocr_cache_path = pdf_dir / f"page_{page_num}_ocr.json"
    if not ocr_cache_path.exists():
        return jsonify({"ok": False, "error": "Not cached yet"}), 404
    try:
        with open(ocr_cache_path, 'r', encoding='utf-8') as f:
            elements = json.load(f)
        return jsonify({"ok": True, "page": page_num, "elements": elements})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/ocr/<pdf_id>/<int:page_num>")
def get_ocr_text(pdf_id, page_num):
    """Extract structured text from single PDF page using opendataloader."""
    # Validate pdf_id format
    if not _re.match(r'^[\w\-]+$', pdf_id):
        return jsonify({"error": "Invalid pdf_id"}), 400

    logging.info("[OCR] Requested page %d for PDF %s", page_num, pdf_id)

    pdf_dir = IMAGE_EXPORT_DIR / pdf_id
    if not pdf_dir.exists() or not pdf_dir.is_dir():
        logging.error("[OCR] PDF directory not found: %s", pdf_id)
        return jsonify({"error": "PDF not found"}), 404

    # Find PDF file matching pattern
    pdf_files = list(PDF_UPLOAD_DIR.glob(f"{pdf_id}.pdf"))
    if not pdf_files:
        # Try to find by prefix
        pdf_files = list(PDF_UPLOAD_DIR.glob(f"*_{pdf_id}.pdf"))

    if not pdf_files:
        logging.error("[OCR] PDF file not found for ID: %s", pdf_id)
        return jsonify({"error": "PDF file not found"}), 404

    pdf_path = pdf_files[0]
    logging.info("[OCR] Found PDF: %s", pdf_path.name)

    # Initialize progress if first request
    if pdf_id not in ocr_progress:
        try:
            doc = fitz.open(str(pdf_path))
            total = len(doc)
            doc.close()
            ocr_progress[pdf_id] = {
                "total_pages": total,
                "processed_pages": 0,
                "current_page": 0,
                "status": "idle",
                "error": None
            }
            logging.info("[OCR] Initialized progress: %d pages for %s", total, pdf_id)
        except Exception as e:
            logging.error("[OCR] Failed to get page count: %s", str(e))

    # Check cache first
    ocr_cache_path = pdf_dir / f"page_{page_num}_ocr.json"
    if ocr_cache_path.exists():
        try:
            with open(ocr_cache_path, 'r', encoding='utf-8') as f:
                cached_data = json.load(f)
                logging.info("[OCR] Returning cached page %d (%d elements)", page_num, len(cached_data))

                # Check if any elements need translation
                page_key = f"{pdf_id}_{page_num}"
                needs_translation = False

                def _node_needs_translation(node):
                    if node.get("content") and not node.get("translation"):
                        return True
                    return any(_node_needs_translation(i) for i in node.get("list items", []) + node.get("kids", []))

                with translation_queue_lock:
                    # page_translation_status is set before any thread spawns — no race
                    already_queued = page_translation_status.get(page_key) in ("processing", "queued")
                    has_untranslated = any(_node_needs_translation(el) for el in cached_data)
                    if not already_queued and has_untranslated:
                        needs_translation = True
                        page_translation_status[page_key] = "queued"  # Guard before thread spawns

                # If translations needed, queue them
                if needs_translation:
                    logging.info("[OCR] Cached page %d has untranslated elements, queueing translations | Book: %s", page_num, pdf_id)
                    worker_thread = threading.Thread(
                        target=translate_elements_background,
                        args=(pdf_id, page_num, cached_data),
                        daemon=True
                    )
                    worker_thread.start()

                return jsonify({
                    "ok": True,
                    "page": page_num,
                    "elements": cached_data,
                    "cached": True,
                    "translation_status": "pending" if needs_translation else "none"
                })
        except Exception as e:
            logging.error("[OCR] Error reading cache for page %d: %s", page_num, str(e))

    # Update progress: processing started
    ocr_progress[pdf_id]["status"] = "processing"
    ocr_progress[pdf_id]["current_page"] = page_num
    logging.info("[OCR] Starting processing for page %d", page_num)

    # Extract single page from PDF and run OCR
    try:
        # Extract page from PDF
        logging.info("[OCR] Opening PDF %s", pdf_path.name)
        doc = fitz.open(str(pdf_path))
        if page_num < 1 or page_num > len(doc):
            logging.error("[OCR] Page %d out of range (total: %d)", page_num, len(doc))
            doc.close()
            return jsonify({"error": "Page out of range"}), 400

        total_pages = len(doc)
        logging.info("[OCR] PDF has %d pages, extracting page %d", total_pages, page_num)

        # Create temporary PDF with just this page
        temp_pdf_dir = tempfile.mkdtemp()
        logging.info("[OCR] Created temp dir: %s", temp_pdf_dir)

        if not Path(temp_pdf_dir).exists():
            logging.error("[OCR] Temp dir creation failed: %s", temp_pdf_dir)
            return jsonify({"error": "Failed to create temp directory"}), 500

        temp_pdf_path = Path(temp_pdf_dir) / f"page_{page_num}.pdf"
        temp_output_dir = Path(temp_pdf_dir) / "output"
        temp_output_dir.mkdir(parents=True, exist_ok=True)
        logging.info("[OCR] Output dir: %s", temp_output_dir)

        # Copy single page to temp PDF
        logging.info("[OCR] Extracting page %d to temp PDF", page_num)
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=page_num - 1, to_page=page_num - 1)
        new_doc.save(str(temp_pdf_path))
        new_doc.close()
        doc.close()

        # Verify temp PDF was created
        if not temp_pdf_path.exists():
            logging.error("[OCR] Temp PDF file was not created: %s", temp_pdf_path)
            shutil.rmtree(temp_pdf_dir, ignore_errors=True)
            return jsonify({"error": "Failed to create temporary PDF"}), 500

        logging.info("[OCR] Temp PDF created successfully: %s (size: %d bytes)",
                    temp_pdf_path, temp_pdf_path.stat().st_size)

        logging.info("[OCR] Running opendataloader-pdf on page %d", page_num)
        logging.info("[OCR] Command: opendataloader-pdf %s -o %s -f json", temp_pdf_path, temp_output_dir)

        # Find opendataloader-pdf executable
        odl_cmd = "opendataloader-pdf"
        if not shutil.which(odl_cmd):
            # Try absolute path from venv
            venv_odl = Path(__file__).parent / ".venv" / "bin" / "opendataloader-pdf"
            if venv_odl.exists():
                odl_cmd = str(venv_odl)
                logging.info("[OCR] Using venv opendataloader-pdf: %s", odl_cmd)
            else:
                logging.error("[OCR] opendataloader-pdf not found in PATH or venv")
                shutil.rmtree(temp_pdf_dir, ignore_errors=True)
                ocr_progress[pdf_id]["error"] = "opendataloader-pdf command not found"
                return jsonify({"error": "OCR tool not installed"}), 500

        try:
            result = subprocess.run(
                [odl_cmd, str(temp_pdf_path), "-o", str(temp_output_dir), "-f", "json"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            logging.info("[OCR] opendataloader-pdf completed: return_code=%d", result.returncode)
            if result.stdout:
                logging.info("[OCR] stdout (first 300 chars): %s", result.stdout[:300])
            if result.stderr:
                logging.warning("[OCR] stderr (first 300 chars): %s", result.stderr[:300])
        except FileNotFoundError as fnf_err:
            shutil.rmtree(temp_pdf_dir, ignore_errors=True)
            logging.error("[OCR] opendataloader-pdf executable not found: %s", str(fnf_err))
            ocr_progress[pdf_id]["error"] = "opendataloader-pdf command not found"
            return jsonify({"error": "OCR tool not installed"}), 500

        if result.returncode != 0:
            logging.warning("[OCR] opendataloader-pdf failed (rc=%d) — trying EasyOCR fallback for page %d", result.returncode, page_num)
            shutil.rmtree(temp_pdf_dir, ignore_errors=True)
            elements = _ocr_fallback_easyocr(pdf_id, page_num)
            if not elements:
                ocr_progress[pdf_id]["error"] = f"opendataloader-pdf failed: {result.stderr}"
                return jsonify({"error": f"OCR failed: {result.stderr}"}), 500
            # Skip JSON parsing block below — jump straight to translation/cache
            _save_and_queue_elements(pdf_id, page_num, elements, ocr_cache_path, total_pages)
            return jsonify({"ok": True, "page": page_num, "elements": elements, "cached": False, "translation_status": "pending"})

        # Find generated JSON file
        json_files = list(temp_output_dir.glob("*.json"))
        if not json_files:
            shutil.rmtree(temp_pdf_dir, ignore_errors=True)
            logging.error("[OCR] No JSON output generated by opendataloader-pdf")
            ocr_progress[pdf_id]["error"] = "No JSON output generated"
            return jsonify({"error": "OCR did not generate output"}), 500

        json_file = json_files[0]
        logging.info("[OCR] Found output file: %s", json_file.name)

        # Parse JSON
        logging.info("[OCR] Parsing JSON output from %s", json_file.name)
        with open(json_file, 'r', encoding='utf-8') as f:
            content = f.read()
            # opendataloader returns object with "kids" key containing elements
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    # Array format (unlikely but handle it)
                    elements = data
                elif isinstance(data, dict):
                    # Object format with "kids" array
                    if 'kids' in data:
                        elements = data['kids']
                        logging.info("[OCR] Found 'kids' array with %d elements", len(elements))
                    elif 'elements' in data:
                        elements = data['elements']
                        logging.info("[OCR] Found 'elements' array with %d elements", len(elements))
                    else:
                        # Fallback: treat entire object as single element (shouldn't happen)
                        elements = []
                        logging.warning("[OCR] No 'kids' or 'elements' found in JSON")
                else:
                    elements = []
                    logging.error("[OCR] Unexpected JSON structure type: %s", type(data))
            except json.JSONDecodeError as je:
                shutil.rmtree(temp_pdf_dir, ignore_errors=True)
                logging.error("[OCR] JSON parse error: %s", str(je))
                return jsonify({"error": f"Failed to parse OCR output: {str(je)}"}), 500

        logging.info("[OCR] Extracted %d text elements from page %d", len(elements), page_num)

        # No text content — scanned PDF: try EasyOCR fallback
        has_text = any(e.get('content') for e in elements)
        if not has_text:
            logging.warning("[OCR] No text content in %d elements (all images?) — trying EasyOCR fallback for page %d", len(elements), page_num)
            shutil.rmtree(temp_pdf_dir, ignore_errors=True)
            elements = _ocr_fallback_easyocr(pdf_id, page_num)
            _save_and_queue_elements(pdf_id, page_num, elements, ocr_cache_path, total_pages)
            return jsonify({"ok": True, "page": page_num, "elements": elements, "cached": False, "translation_status": "pending"})

        shutil.rmtree(temp_pdf_dir, ignore_errors=True)
        logging.info("[OCR] Cleaned up temp directory")

        _save_and_queue_elements(pdf_id, page_num, elements, ocr_cache_path, total_pages)

        return jsonify({
            "ok": True,
            "page": page_num,
            "elements": elements,
            "cached": False,
            "translation_status": "pending"
        })

    except subprocess.TimeoutExpired:
        logging.error("[OCR] opendataloader-pdf timeout on page %d", page_num)
        ocr_progress[pdf_id]["error"] = "OCR timeout"
        ocr_progress[pdf_id]["status"] = "error"
        return jsonify({"error": "OCR timeout"}), 500
    except Exception as e:
        logging.error("[OCR] Unexpected error on page %d: %s", page_num, str(e), exc_info=True)
        ocr_progress[pdf_id]["error"] = str(e)
        ocr_progress[pdf_id]["status"] = "error"
        return jsonify({"error": f"OCR failed: {str(e)}"}), 500

def _wait_for_page_translation(pdf_id: str, page_num: int, timeout: int = 600):
    """Block until all queued elements for a page are done/error, or timeout."""
    import time
    page_key = f"{pdf_id}_{page_num}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        with translation_queue_lock:
            element_statuses = [
                v["status"] for k, v in translation_queue.items()
                if k.startswith(f"{page_key}_")
            ]
        if not element_statuses:
            break
        if all(s in ("done", "error") for s in element_statuses):
            break
        time.sleep(1)


def _translate_all_worker(pdf_id: str, pdf_path: str, total_pages: int):
    """Background worker: OCR then translate each page sequentially."""
    translate_all_jobs[pdf_id] = {'status': 'running', 'total': total_pages, 'done': 0, 'failed': 0}

    def _needs_translation(node):
        if node.get("content") and not node.get("translation"):
            return True
        return any(_needs_translation(i) for i in node.get("list items", []) + node.get("kids", []))

    odl_cmd = "opendataloader-pdf"
    if not shutil.which(odl_cmd):
        venv_odl = Path(__file__).parent / ".venv" / "bin" / "opendataloader-pdf"
        if venv_odl.exists():
            odl_cmd = str(venv_odl)

    for page_num in range(1, total_pages + 1):
        try:
            pdf_dir = IMAGE_EXPORT_DIR / pdf_id
            ocr_cache_path = pdf_dir / f"page_{page_num}_ocr.json"

            # --- Step 1: ensure OCR cache exists ---
            if ocr_cache_path.exists():
                with open(ocr_cache_path, 'r', encoding='utf-8') as f:
                    elements = json.load(f)
            else:
                temp_pdf_dir = tempfile.mkdtemp()
                try:
                    temp_pdf_path_p = Path(temp_pdf_dir) / f"page_{page_num}.pdf"
                    temp_output_dir_p = Path(temp_pdf_dir) / "output"
                    temp_output_dir_p.mkdir(parents=True, exist_ok=True)

                    doc = fitz.open(pdf_path)
                    new_doc = fitz.open()
                    new_doc.insert_pdf(doc, from_page=page_num - 1, to_page=page_num - 1)
                    new_doc.save(str(temp_pdf_path_p))
                    new_doc.close()
                    doc.close()

                    result = subprocess.run(
                        [odl_cmd, str(temp_pdf_path_p), "-o", str(temp_output_dir_p), "-f", "json"],
                        capture_output=True, text=True, timeout=60,
                    )

                    if result.returncode != 0:
                        raise RuntimeError(f"OCR subprocess failed: {result.stderr[:200]}")

                    json_files = list(temp_output_dir_p.glob("*.json"))
                    if not json_files:
                        raise RuntimeError("No JSON output from OCR")

                    with open(json_files[0], 'r', encoding='utf-8') as f:
                        data = json.load(f)

                    if isinstance(data, list):
                        elements = data
                    elif isinstance(data, dict):
                        elements = data.get('kids') or data.get('elements') or []
                    else:
                        elements = []

                    with open(ocr_cache_path, 'w', encoding='utf-8') as f:
                        json.dump(elements, f, ensure_ascii=False, indent=2)

                    logging.info("[TRANSLATE_ALL] OCR done page %d | %s", page_num, pdf_id)
                finally:
                    shutil.rmtree(temp_pdf_dir, ignore_errors=True)

            # --- Step 2: queue translation and wait for completion ---
            page_key = f"{pdf_id}_{page_num}"
            needs_trans = False
            with translation_queue_lock:
                already_queued = page_translation_status.get(page_key) in ("processing", "queued")
                if not already_queued and any(_needs_translation(el) for el in elements):
                    page_translation_status[page_key] = "queued"
                    needs_trans = True

            if needs_trans:
                translate_elements_background(pdf_id, page_num, elements)  # runs inline (queues threads)
                _wait_for_page_translation(pdf_id, page_num)
                logging.info("[TRANSLATE_ALL] Translation done page %d | %s", page_num, pdf_id)

            translate_all_jobs[pdf_id]['done'] += 1
            logging.info("[TRANSLATE_ALL] Page %d/%d complete | %s", page_num, total_pages, pdf_id)

        except Exception as e:
            logging.error("[TRANSLATE_ALL] Failed page %d: %s", page_num, str(e))
            translate_all_jobs[pdf_id]['failed'] += 1

    translate_all_jobs[pdf_id]['status'] = 'done'
    logging.info("[TRANSLATE_ALL] Completed all %d pages for %s", total_pages, pdf_id)


@app.post("/api/ocr/<pdf_id>/translate_all")
def start_translate_all(pdf_id):
    """Queue OCR + translation for all pages of a PDF."""
    if not _re.match(r'^[\w\-]+$', pdf_id):
        return jsonify({"error": "Invalid pdf_id"}), 400

    existing = translate_all_jobs.get(pdf_id)
    if existing and existing.get('status') == 'running':
        return jsonify({"ok": True, "status": "already_running", **existing})

    pdf_files = list(PDF_UPLOAD_DIR.glob(f"{pdf_id}.pdf"))
    if not pdf_files:
        pdf_files = list(PDF_UPLOAD_DIR.glob(f"*_{pdf_id}.pdf"))
    if not pdf_files:
        return jsonify({"error": "PDF file not found"}), 404

    pdf_path = str(pdf_files[0])
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    threading.Thread(target=_translate_all_worker, args=(pdf_id, pdf_path, total_pages), daemon=True).start()
    return jsonify({"ok": True, "status": "started", "total": total_pages})


@app.get("/api/ocr/<pdf_id>/translate_all/status")
def get_translate_all_status(pdf_id):
    """Get translate-all job status."""
    if not _re.match(r'^[\w\-]+$', pdf_id):
        return jsonify({"error": "Invalid pdf_id"}), 400
    job = translate_all_jobs.get(pdf_id)
    if not job:
        return jsonify({"status": "idle"})
    return jsonify({"ok": True, **job})


@app.post("/api/upload_pdf")
def upload_pdf():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400
    
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are allowed"}), 400

    # Save PDF
    pdf_id = str(uuid.uuid4())[:8]
    safe_stem = _re.sub(r'[^\w\-]', '_', Path(file.filename).stem)
    pdf_filename = f"{safe_stem}_{pdf_id}.pdf"
    pdf_path = PDF_UPLOAD_DIR / pdf_filename
    file.save(str(pdf_path))

    # Create image directory
    pdf_image_dir = IMAGE_EXPORT_DIR / f"{safe_stem}_{pdf_id}"
    pdf_image_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = fitz.open(str(pdf_path))
        image_urls = []
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) # higher resolution
            img_filename = f"page_{i+1}.png"
            img_path = pdf_image_dir / img_filename
            pix.save(str(img_path))
            
            # Construct relative URL
            image_url = f"/static/uploads/images/{safe_stem}_{pdf_id}/{img_filename}"
            image_urls.append(image_url)
        
        doc.close()
        return jsonify({
            "ok": True,
            "pdf_name": file.filename,
            "pdf_id": f"{safe_stem}_{pdf_id}",
            "images": image_urls
        })
    except Exception as e:
        logging.error("PDF Conversion error: %s", str(e))
        return jsonify({"error": f"Failed to convert PDF: {str(e)}"}), 500


@app.get("/api/pdf_text/<pdf_id>")
def get_pdf_text_pages(pdf_id):
    """Extract text from all pages. Uses PyMuPDF first, falls back to EasyOCR for image-only pages."""
    if not _re.match(r'^[\w\-]+$', pdf_id):
        return jsonify({"error": "Invalid pdf_id"}), 400

    pdf_path = PDF_UPLOAD_DIR / f"{pdf_id}.pdf"
    if not pdf_path.exists():
        return jsonify({"error": "PDF not found"}), 404

    pdf_dir = IMAGE_EXPORT_DIR / pdf_id
    ocr_cache_path = pdf_dir / "ocr.json" if pdf_dir.exists() else None
    ocr_data = {}
    if ocr_cache_path and ocr_cache_path.exists():
        try:
            with open(ocr_cache_path, 'r', encoding='utf-8') as f:
                ocr_data = json.load(f)
        except Exception:
            pass

    try:
        doc = fitz.open(str(pdf_path))
        pages = []
        for i, page in enumerate(doc):
            page_num = i + 1
            text = page.get_text("text").strip()
            if not text:
                page_key = str(page_num)
                if page_key in ocr_data:
                    text = ocr_data[page_key]
                elif pdf_dir.exists():
                    img_path = pdf_dir / f"page_{page_num}.png"
                    if img_path.exists():
                        try:
                            reader = get_ocr_reader()
                            results = reader.readtext(str(img_path), detail=0)
                            text = "\n".join(results)
                            ocr_data[page_key] = text
                        except Exception as ocr_err:
                            logging.error("OCR fallback error page %d: %s", page_num, ocr_err)
                            text = ""
            pages.append({"page": page_num, "text": text})
        doc.close()

        if ocr_cache_path and ocr_data:
            try:
                with open(ocr_cache_path, 'w', encoding='utf-8') as f:
                    json.dump(ocr_data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        return jsonify({"ok": True, "pages": pages})
    except Exception as e:
        logging.error("PDF text extraction error: %s", e)
        return jsonify({"error": str(e)}), 500


SEAMLESS_SCRIPT = Path(__file__).parent / "seamless-m4t-medium.py"


def _run_yt_download(job_id, url):
    job = yt_jobs[job_id]
    try:
        # ── Probe expected filename (no download) ─────────────────────────────
        job["progress"] = "Checking existing files..."
        probe = subprocess.run(
            ["/usr/local/bin/yt-dlp", "--print", "filename",
             "-o", str(YT_DOWNLOAD_DIR / "%(title)s.%(ext)s"), url],
            capture_output=True, text=True,
        )
        expected_file = probe.stdout.strip().splitlines()[0] if probe.returncode == 0 else None

        # ── Stage 1: yt-dlp ──────────────────────────────────────────────────
        downloaded_file = None
        if expected_file and Path(expected_file).exists():
            downloaded_file = expected_file
            job["progress"] = f"[yt-dlp] Already downloaded: {Path(expected_file).name}"
        else:
            proc = subprocess.Popen(
                ["/usr/local/bin/yt-dlp", "--newline", "-o", str(YT_DOWNLOAD_DIR / "%(title)s.%(ext)s"), url],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            last_line = ""
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                last_line = line
                job["progress"] = "[yt-dlp] " + line
                m = _re.search(r'\[Merger\] Merging formats into "(.+)"', line)
                if m:
                    downloaded_file = m.group(1).strip()
                if not downloaded_file:
                    m = _re.search(r'\[download\] Destination: (.+)', line)
                    if m:
                        downloaded_file = m.group(1).strip()
            proc.wait()
            if proc.returncode != 0:
                job["status"] = "error"
                job["error"] = last_line or "yt-dlp exited with error"
                return
            if not downloaded_file or not Path(downloaded_file).exists():
                job["status"] = "error"
                job["error"] = "Could not detect downloaded file path from yt-dlp output"
                return

        stem = Path(downloaded_file).stem
        wav_path = YT_DOWNLOAD_DIR / (stem + ".wav")
        srt_path = YT_DOWNLOAD_DIR / (stem + ".fr.srt")

        # ── Stage 2: ffmpeg → WAV ─────────────────────────────────────────────
        if wav_path.exists():
            job["progress"] = f"[ffmpeg] WAV already exists: {wav_path.name}"
        else:
            job["progress"] = f"[ffmpeg] Converting to WAV: {wav_path.name}"
            ffmpeg = subprocess.run(
                ["ffmpeg", "-y", "-i", downloaded_file,
                 "-ar", "16000", "-ac", "1", "-vn", str(wav_path)],
                capture_output=True, text=True,
            )
            if ffmpeg.returncode != 0:
                job["status"] = "error"
                job["error"] = "[ffmpeg] " + (ffmpeg.stderr[-500:] or "conversion failed")
                return

        # ── Stage 3: seamless-m4t → SRT ───────────────────────────────────────
        if srt_path.exists():
            job["progress"] = f"[seamless] SRT already exists: {srt_path.name}"
        else:
            job["progress"] = "[seamless] Loading model..."
            seamless = subprocess.Popen(
                [sys.executable, str(SEAMLESS_SCRIPT),
                 "--input", str(wav_path), "--output", str(srt_path)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in seamless.stdout:
                line = line.strip()
                if line:
                    job["progress"] = "[seamless] " + line
            seamless.wait()
            if seamless.returncode != 0:
                job["status"] = "error"
                job["error"] = job["progress"] + " (seamless-m4t failed)"
                return

        job["status"] = "done"
        job["progress"] = f"Done — SRT: {srt_path.name}"
        job["srt"] = srt_path.name

    except FileNotFoundError as e:
        job["status"] = "error"
        job["error"] = f"Command not found: {e}"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.post("/api/yt/download")
def yt_download():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    job_id = str(uuid.uuid4())
    yt_jobs[job_id] = {"status": "processing", "progress": "Starting...", "error": None}
    threading.Thread(target=_run_yt_download, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/yt/status/<job_id>")
def yt_status(job_id):
    job = yt_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.get("/api/yt/files")
def yt_files():
    files = []
    for f in sorted(YT_DOWNLOAD_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.is_file():
            stat = f.stat()
            files.append({"name": f.name, "size": stat.st_size, "mtime": stat.st_mtime})
    return jsonify(files)


@app.get("/api/yt/file/<filename>")
def yt_file(filename):
    path = YT_DOWNLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        return jsonify({"error": "Not found"}), 404
    return send_file(path, as_attachment=True)


@app.get("/api/yt/stream/<filename>")
def yt_stream(filename):
    path = YT_DOWNLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        return jsonify({"error": "Not found"}), 404
    mime = "text/plain; charset=utf-8" if filename.endswith(".srt") else None
    return send_file(path, conditional=True, mimetype=mime)


@app.get("/api/yt/vtt/<filename>")
def yt_vtt(filename):
    """Serve an SRT file converted to WebVTT (needed for <track> in browsers)."""
    path = YT_DOWNLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        return "Not found", 404
    srt = path.read_text(encoding="utf-8")
    # SRT timestamps use comma; VTT requires period
    vtt = "WEBVTT\n\n" + _re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", srt)
    return Response(vtt, mimetype="text/vtt")


@app.delete("/api/yt/file/<filename>")
def yt_delete(filename):
    path = YT_DOWNLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        return jsonify({"error": "Not found"}), 404
    path.unlink()
    return jsonify({"ok": True})


@app.delete("/api/pdf/<pdf_id>")
def delete_pdf(pdf_id):
    if not _re.match(r'^[\w\-]+$', pdf_id):
        return jsonify({"error": "Invalid pdf_id"}), 400

    # Delete PDF file
    pdf_path = PDF_UPLOAD_DIR / f"{pdf_id}.pdf"
    if pdf_path.exists():
        pdf_path.unlink()

    # Delete image directory and all contents
    pdf_dir = IMAGE_EXPORT_DIR / pdf_id
    if pdf_dir.exists():
        import shutil
        shutil.rmtree(str(pdf_dir))

    return jsonify({"ok": True})


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

SERVICES = [
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
]

def _detect_local_ip():
    """Return the local LAN IP this machine uses to reach the internet."""
    for url in SERVICES:
        try:
            return requests.get(url, timeout=5).text.strip()
        except Exception:
            continue
    return "127.0.0.1"


_tiny_tts_instance = None
_tiny_tts_lock = threading.Lock()
_tiny_tts_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def _get_tiny_tts():
    global _tiny_tts_instance
    if _tiny_tts_instance is None:
        with _tiny_tts_lock:
            if _tiny_tts_instance is None:
                from tiny_tts import TinyTTS
                logging.info("Loading TinyTTS model...")
                _tiny_tts_instance = TinyTTS(device="cpu")
                logging.info("TinyTTS model ready.")
    return _tiny_tts_instance


def _preload_tiny_tts():
    try:
        _get_tiny_tts()
    except Exception as e:
        logging.warning("tiny-tts preload error: %s", e)


@app.post("/api/tts_read")
def tts_read():
    data = request.get_json()
    text = (data or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        tts = _get_tiny_tts()
        future = _tiny_tts_executor.submit(tts.speak, text, tmp.name)
        future.result(timeout=60)
        with open(tmp.name, "rb") as f:
            audio_bytes = f.read()
        return Response(audio_bytes, mimetype="audio/wav")
    except concurrent.futures.TimeoutError:
        return jsonify({"error": "TTS timeout"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    # DIRECT_HOST can be set to force a specific IP/hostname for direct audio URLs.
    # If not set, auto-detect the local network IP.
    DIRECT_HOST = os.environ.get("DIRECT_HOST") or _detect_local_ip()
    DIRECT_BASE = f"http://{DIRECT_HOST}:{PORT}"
    logging.info("Direct audio URL base: %s", DIRECT_BASE)

    @app.get("/api/direct_url")
    def direct_url():
        return jsonify({"url": DIRECT_BASE})

    try:
        preload_model()
    except Exception as e:
        logging.error("Model preloading failed: %s", str(e))
        print(f"Warning: Model preloading failed. TTS features may be unavailable. Error: {e}")

    try:
        load_translation_model()
    except Exception as e:
        logging.error("Translation model preloading failed: %s", str(e))
        print(f"Warning: Translation model preloading failed. Translation features may be unavailable. Error: {e}")

    threading.Thread(target=_preload_tiny_tts, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT, debug=False)
