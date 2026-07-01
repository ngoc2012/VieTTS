"""Isolated Chatterbox TTS worker.

Runs in its OWN uv environment so chatterbox-tts (torch 2.6 / transformers 5.2)
does not clobber the main VieNeu-TTS pins (torch 2.10 / transformers 5.12).

Launched automatically by vieneu/chatterbox_backend.py via:
    uv run --no-project --with chatterbox-tts python chatterbox_worker.py [PORT]

Endpoints (127.0.0.1 only):
    GET  /health          -> {"ok": true}
    POST /load  {backend, device}                 -> {"ok": true, "sr": <int>}
    POST /infer {backend, device, text,
                 language_id?, audio_b64?}         -> raw float32 PCM, header X-Sample-Rate

Models are cached by backend key, so the 3 multilingual variants (EN/ZH/FR)
share ONE ChatterboxMultilingualTTS instance.
"""
import base64
import json
import sys
import tempfile
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import torch

_MODELS = {}  # backend -> model


def get_model(backend, device):
    if backend not in _MODELS:
        if backend == "chatterbox_mtl":
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS
            _MODELS[backend] = ChatterboxMultilingualTTS.from_pretrained(device=device)
        else:
            from chatterbox.tts import ChatterboxTTS
            _MODELS[backend] = ChatterboxTTS.from_pretrained(device=device)
    return _MODELS[backend]


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            req = self._read_json()
            if self.path == "/load":
                m = get_model(req["backend"], req.get("device", "cpu"))
                self._json(200, {"ok": True, "sr": int(m.sr)})
                return
            if self.path != "/infer":
                self._json(404, {"error": "not found"})
                return

            model = get_model(req["backend"], req.get("device", "cpu"))
            kwargs = {}
            if req.get("language_id"):
                kwargs["language_id"] = req["language_id"]

            tmp_path = None
            if req.get("audio_b64"):
                tmp_path = tempfile.mktemp(suffix=".wav")
                with open(tmp_path, "wb") as f:
                    f.write(base64.b64decode(req["audio_b64"]))
                kwargs["audio_prompt_path"] = tmp_path
            try:
                result = model.generate(req["text"], **kwargs)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

            if isinstance(result, torch.Tensor):
                wav = result.squeeze().cpu().numpy()
            else:
                wav = np.asarray(result)
            data = wav.astype(np.float32).tobytes()

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Sample-Rate", str(int(model.sr)))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # keep worker alive; report error to caller
            import traceback
            traceback.print_exc()
            self._json(500, {"error": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5099
    print(f"[chatterbox_worker] listening on 127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
