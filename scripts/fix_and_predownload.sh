#!/bin/bash
# Fix chatterbox numpy error + predownload all model weights into the shared
# HF cache so startup never touches the network again. See
# docs/FIX_STARTUP_ERRORS.md for the why.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/3: rebuild .venv-chatterbox (fixes numpy / partial installs) =="
rm -rf .venv-chatterbox
uv venv .venv-chatterbox --python 3.12
uv pip install --python .venv-chatterbox/bin/python \
  chatterbox-tts torch==2.6.0 torchaudio==2.6.0 transformers==5.2.0 numpy \
  "setuptools<81"

echo "== 2/3: predownload VieNeu-TTS backbones + codecs (main .venv) =="
uv run --frozen python - <<'PY'
from huggingface_hub import snapshot_download

repos = [
    "pnnbao-ump/VieNeu-TTS",
    "pnnbao-ump/VieNeu-TTS-0.3B",
    "pnnbao-ump/VieNeu-TTS-q8-gguf",
    "pnnbao-ump/VieNeu-TTS-q4-gguf",
    "pnnbao-ump/VieNeu-TTS-0.3B-q4-gguf",
    "pnnbao-ump/VieNeu-TTS-0.3B-q8-gguf",
    "neuphonic/neucodec",
    "neuphonic/distill-neucodec",
    "neuphonic/neucodec-onnx-decoder-int8",
]
for repo in repos:
    print(f"-- {repo}")
    try:
        snapshot_download(repo_id=repo)
    except Exception as e:
        print(f"   skip ({e})")
PY

echo "== 3/3: predownload chatterbox weights (isolated venv) =="
.venv-chatterbox/bin/python - <<'PY'
from huggingface_hub import snapshot_download

for repo in ["resemble-ai/chatterbox", "resemble-ai/chatterbox-multilingual"]:
    print(f"-- {repo}")
    try:
        snapshot_download(repo_id=repo)
    except Exception as e:
        print(f"   skip ({e})")
PY

echo
echo "Done. Everything now cached under \${HF_HOME:-~/.cache/huggingface}."
echo "Add this to ~/.bashrc (or wherever you export env for 'make') so"
echo "startup never hits the network again:"
echo
echo "    export HF_HUB_OFFLINE=1"
