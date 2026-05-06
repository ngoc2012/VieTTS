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
import yaml
import requests
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from flask import Flask, request, jsonify, send_file, render_template, Response, url_for
import pymupdf as fitz
  # PyMuPDF
import json
import easyocr

# Initialize EasyOCR reader (Vietnamese and English)
ocr_reader = None

def get_ocr_reader():
    global ocr_reader
    if ocr_reader is None:
        logging.info("Initializing EasyOCR reader...")
        ocr_reader = easyocr.Reader(['vi', 'en'])
    return ocr_reader

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
def viewer(pdf_id):
    pdf_dir = IMAGE_EXPORT_DIR / pdf_id
    if not pdf_dir.exists() or not pdf_dir.is_dir():
        return "Not found", 404
    
    def sort_key(p):
        match = _re.search(r'page_(\d+)', p.name)
        return int(match.group(1)) if match else 0

    images = sorted(pdf_dir.glob("page_*.png"), key=sort_key)
    page_count = len(images)
    
    # Get display name
    name_parts = pdf_id.rsplit('_', 1)
    display_name = name_parts[0] if len(name_parts) > 1 else pdf_id
    
    return render_template("viewer.html", pdf_id=pdf_id, display_name=display_name, page_count=page_count)

@app.get("/api/ocr/<pdf_id>/<int:page_num>")
def get_ocr_text(pdf_id, page_num):
    pdf_dir = IMAGE_EXPORT_DIR / pdf_id
    if not pdf_dir.exists() or not pdf_dir.is_dir():
        return jsonify({"error": "PDF not found"}), 404
        
    ocr_cache_path = pdf_dir / "ocr.json"
    ocr_data = {}
    if ocr_cache_path.exists():
        try:
            with open(ocr_cache_path, 'r', encoding='utf-8') as f:
                ocr_data = json.load(f)
        except Exception:
            pass
            
    page_key = str(page_num)
    if page_key in ocr_data:
        return jsonify({"ok": True, "text": ocr_data[page_key]})
        
    # Perform OCR
    img_path = pdf_dir / f"page_{page_num}.png"
    if not img_path.exists():
        return jsonify({"error": "Page not found"}), 404
        
    try:
        reader = get_ocr_reader()
        results = reader.readtext(str(img_path), detail=0)
        text = "\n".join(results)
        
        # Update cache
        ocr_data[page_key] = text
        with open(ocr_cache_path, 'w', encoding='utf-8') as f:
            json.dump(ocr_data, f, ensure_ascii=False, indent=2)
            
        return jsonify({"ok": True, "text": text})
    except Exception as e:
        logging.error("OCR error: %s", str(e))
        return jsonify({"error": f"OCR failed: {str(e)}"}), 500

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

    app.run(host="0.0.0.0", port=PORT, debug=False)
