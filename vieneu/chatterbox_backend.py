"""Chatterbox backend — proxies to an isolated worker process.

chatterbox-tts conflicts with the main project's torch/transformers pins, so it
runs in its own uv environment (see chatterbox_worker.py). This module speaks to
that worker over local HTTP and adapts it to the VieNeuTTS interface.
"""
import atexit
import base64
import json
import os
import subprocess
import time
import urllib.request

import numpy as np

_PORT = int(os.environ.get("CHATTERBOX_PORT", "5099"))
_URL = f"http://127.0.0.1:{_PORT}"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "chatterbox_worker.py")
_VENV = os.path.join(_ROOT, ".venv-chatterbox")
_VPY = os.path.join(_VENV, "bin", "python")
_proc = None


def _clean_env():
    # Drop VIRTUAL_ENV/UV_* so uv doesn't reuse the parent project venv.
    return {k: v for k, v in os.environ.items()
            if k != "VIRTUAL_ENV" and not k.startswith("UV_")}


def _ensure_venv():
    """Create the dedicated chatterbox venv once.

    Isolated from the main project (torch 2.10 / transformers 5.12) which is
    incompatible with chatterbox. Pins are explicit so the resolver can't drift.
    """
    if os.path.exists(_VPY):
        return
    env = _clean_env()
    subprocess.run(["uv", "venv", _VENV, "--python", "3.12"],
                   check=True, cwd=_ROOT, env=env)
    subprocess.run(["uv", "pip", "install", "--python", _VPY,
                    "chatterbox-tts", "torch==2.6.0", "torchaudio==2.6.0",
                    "transformers==5.2.0",
                    "setuptools<81"],  # perth imports pkg_resources, removed in setuptools>=81
                   check=True, cwd=_ROOT, env=env)


def _alive():
    try:
        with urllib.request.urlopen(f"{_URL}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _stop_worker():
    global _proc
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()


def _ensure_worker():
    """Start the isolated worker if not already running. Blocks until healthy.

    First launch is slow: uv resolves the chatterbox-tts env and models download.
    """
    global _proc
    if _alive():
        return
    _ensure_venv()
    if _proc is None or _proc.poll() is not None:
        # Launch the worker with the dedicated venv's python directly — no nested
        # `uv run`, so nothing from the parent project env can leak in.
        _proc = subprocess.Popen(
            [_VPY, _SCRIPT, str(_PORT)],
            cwd=_ROOT,
            env=_clean_env(),
        )
        atexit.register(_stop_worker)
    for _ in range(600):  # up to 10 min for first-run env build + model download
        if _alive():
            return
        if _proc.poll() is not None:
            raise RuntimeError("chatterbox worker exited during startup")
        time.sleep(1)
    raise RuntimeError("chatterbox worker failed to become healthy")


def _post(path, payload, timeout):
    req = urllib.request.Request(
        f"{_URL}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


class ChatterboxBackend:
    """Adapts the isolated chatterbox worker to the VieNeuTTS interface."""

    def __init__(self, backend, language_id=None, device="cpu", sample_rate=24000):
        self.backend = backend
        self.language_id = language_id
        self.device = device
        self.sample_rate = sample_rate

    def encode_reference(self, audio_path: str):
        # Return bytes, not a path: flask deletes the temp ref file before infer().
        with open(audio_path, "rb") as f:
            return f.read()

    def infer(self, text, ref_codes=None, ref_text=None, temperature=0.8):
        _ensure_worker()
        payload = {"backend": self.backend, "device": self.device, "text": text}
        if self.language_id:
            payload["language_id"] = self.language_id
        if isinstance(ref_codes, (bytes, bytearray)):
            payload["audio_b64"] = base64.b64encode(ref_codes).decode()
        with _post("/infer", payload, timeout=600) as r:
            self.sample_rate = int(r.headers.get("X-Sample-Rate", self.sample_rate))
            data = r.read()
        return np.frombuffer(data, dtype=np.float32).copy()

    def list_preset_voices(self):
        return []

    def get_preset_voice(self, voice_id):
        return {"codes": None, "text": ""}


def make_chatterbox(backbone_cfg: dict, device: str) -> ChatterboxBackend:
    backend = backbone_cfg.get("backend")
    language_id = backbone_cfg.get("language_id")
    _ensure_worker()
    # Load the model in the worker now (preload) and learn its sample rate.
    with _post("/load", {"backend": backend, "device": device}, timeout=600) as r:
        sr = json.loads(r.read()).get("sr", 24000)
    return ChatterboxBackend(backend, language_id=language_id, device=device, sample_rate=sr)
