# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

VieTTS is a fork of VieNeu-TTS (Vietnamese Text-to-Speech with instant voice cloning, 3-5s reference audio) extended into a full Flask web application. The upstream Gradio UI (`gradio_app.py`) and the `main.py`/`main_remote.py`/`web_stream_gguf.py` entry scripts were **removed**; `flask_app.py` is the app now. Output is 24kHz, fully offline. See README.md for full feature documentation.

Major components:
- **TTS studio** (`flask_app.py`, ~100KB, single process) — multi-model synthesis, voice cloning, Opus/WebM streaming via ffmpeg, per-user output history
- **Billing** (`billing.py`) — SQLite credit accounts (`billing.db`), per-operation charges, PayPal Orders-v2 top-ups (see `PAYPAL_SETUP.md`)
- **PDF reader** — upload → page PNGs → OCR (opendataloader-pdf → EasyOCR → PyMuPDF fallback) → background EN→VI translation (Tencent HY-MT1.5-1.8B from `models/HY-MT1.5-1.8B`)
- **YouTube tool** — yt-dlp download + SeamlessM4T subtitle generation (`seamless-m4t-medium.py`)
- **Browser extension** (Manifest V3, lives in `static/`) — scrapes page text into the studio
- **Chatterbox backends** — run out-of-process in `.venv-chatterbox` (`chatterbox_worker.py`, port 5099) because of torch/transformers version conflicts
- **`distributed/`** — Celery + Redis + Flower skeleton; placeholder workload, TTS not wired in

Model variants: VieNeu-TTS 0.5B (Apache 2.0, fine-tuned from NeuTTS Air) and 0.3B (CC BY-NC 4.0, trained from scratch), each in PyTorch and GGUF q4/q8.

## Commands

```bash
# Setup (Python 3.12, eSpeak NG, uv)
make setup                   # prereq check + uv sync
uv sync --no-default-groups  # CPU-only install

# Run the web app → http://localhost:5000  (PORT env to change)
make                         # run_with_restart.sh supervisor around: uv run --frozen flask_app.py
uv run flask_app.py          # direct

uv run download_translation_model.py  # fetch HY-MT1.5-1.8B (needed for PDF translation)
python billing.py seed|list|topup|check  # billing CLI

# Tunnels
make cloud        # cloudflared one-shot
make cloud-auto   # monitored cloudflared + email alerts
./tunnel_restart.sh  # ngrok with cloudflared fallback

# Docker
docker compose -f docker-compose.light.yml up   # CPU stack (make re / detach / down)
docker compose --profile gpu up                 # GPU dev (NOTE: still runs deleted gradio_app.py — broken)

# LMDeploy API server (GPU)
python -m vieneu.serve --model pnnbao-ump/VieNeu-TTS --port 23333

make check    # toolchain report
make clean    # remove .venv, __pycache__, .pytest_cache
```

There is no formal test suite or linter. Root `test_*.py` files are scratch experiments (Chatterbox, VoxCPM2, TinyTTS, translation), not tests.

## Architecture

### Core Packages

- **`vieneu/`** — SDK package (published on PyPI as `vieneu`)
  - `core.py` (~1840 lines) — `VieNeuTTS` (PyTorch + GGUF), `FastVieNeuTTS` (LMDeploy, CUDA-only, no `save()`), `RemoteVieNeuTTS` (OpenAI-compatible client, async batch), `Vieneu(mode=...)` factory (only `"remote"`/`"api"` vs default — there is no `"fast"` mode)
  - `serve.py` — LMDeploy server wrapper (port 23333, `--tunnel` uses bore)
  - `chatterbox_backend.py` — builds `.venv-chatterbox` and spawns the isolated worker
  - `assets/voices.json` — 6 preset voices (Binh default, Tuyen, Vinh, Doan, Ly, Ngoc)
- **`vieneu_utils/`** — `normalize_text.py` (Vietnamese number/date/unit normalization; `<en>…</en>` spans protected), `phonemize_text.py` (17MB `phoneme_dict.json` + eSpeak fallback), `core_utils.py` (chunk splitting/joining)

### Text Processing Pipeline

```
Raw Text → normalize_text → phonemize_with_dict → split_text_into_chunks (256 chars max)
→ Backbone inference → Codec decoding → overlap-add → Final 24kHz waveform
```

Callers pass raw text; phonemization happens inside prompt formatting.

### Flask app specifics

- One TTS job at a time (`active_job_id` lock; concurrent requests get 503). All job/progress state is in-process memory — **never run multi-worker gunicorn**.
- Session auth (cookie key auto-generated in `.flask_secret`); only credit-charging endpoints require login. CORS is `*`.
- Charges happen before work via `billing.charge()` (atomic, no refunds on failure). Rates in `billing.RATES` (euro cents).
- `config.yaml` is consumed by `flask_app.py` only (11 backbones: 6 VieNeu + 5 Chatterbox; 3 codecs). The SDK never reads it.
- ONNX codec (`neucodec-onnx-decoder-int8`) is decode-only — cannot encode reference audio.
- External binaries: ffmpeg (streaming), `/usr/local/bin/yt-dlp` (hardcoded), opendataloader-pdf (needs Java 17), eSpeak NG.

### Configuration

- `pyproject.toml` — active (CPU torch); `pyproject.toml.gpu` — CUDA 12.8 + LMDeploy swap-in; `pyproject.toml.cpu` — minimal. `requirements.txt` is a stale cu118 freeze; uv.lock is the source of truth.
- Key env vars: `PORT` (5000), `PAYPAL_CLIENT_ID`/`PAYPAL_CLIENT_SECRET`/`PAYPAL_ENV`, `CHATTERBOX_PORT`, `DIRECT_HOST`, `PHONEME_DICT_PATH`.

### Fine-tuning (`finetune/`)

LoRA (PEFT): `data_scripts/filter_data.py` → `data_scripts/encode_data.py` → `train.py` (config in `configs/lora_config.py`, run from repo root) → `merge_lora.py` → `create_voices_json.py`.

### System Dependencies

eSpeak NG is required at runtime for phonemization — the `phonemizer` library calls it. Install: `sudo apt install espeak-ng` (Linux), `brew install espeak` (macOS).

### Known stale references (do not trust)

`make demo` and `docker-compose.yml` still invoke the deleted `gradio_app.py`; `docker-compose.light.yml` maps 7860 but the app listens on 5000; `make nocache` has a broken line continuation; `README_PYPI.md` links deleted `main.py`/`main_remote.py`.
