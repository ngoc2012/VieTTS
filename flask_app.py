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
import subprocess
import logging
import yaml
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from flask import Flask, request, jsonify, send_file, render_template, Response

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
        "cancelled": False,
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


@app.get("/api/busy")
def check_busy():
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
                logging.info("  Chunk %d/%d: %d chars → %.1fs audio in %.1fs (RTF %.2f)",
                             i, total, len(chunk), chunk_dur, chunk_time, chunk_time / chunk_dur if chunk_dur > 0 else 0)
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

        # Log summary
        total_time = time.time() - job_start
        audio_dur = len(audio) / tts.sample_rate
        avg_chunk = sum(chunk_times) / len(chunk_times) if chunk_times else 0
        logging.info("Job %s done — %d chars, %.1fs audio, %d chunks, %.1fs total, %.1fs avg/chunk, RTF %.2f",
                     job_id[:8], len(text), audio_dur, total, total_time, avg_chunk,
                     total_time / audio_dur if audio_dur > 0 else 0)

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
    app.run(host="0.0.0.0", port=5000, debug=False)
