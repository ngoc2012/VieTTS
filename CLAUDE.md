# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

VieNeu-TTS is a Vietnamese Text-to-Speech system with instant voice cloning (3-5s reference audio). It supports multiple inference backends (PyTorch, GGUF quantized, ONNX codec) and runs fully offline at 24kHz.

Two model variants:
- **VieNeu-TTS (0.5B):** Fine-tuned from NeuTTS Air, highest quality (Apache 2.0)
- **VieNeu-TTS-0.3B:** Trained from scratch, 2x faster inference (CC BY-NC 4.0)

## Commands

```bash
# Setup (requires Python 3.10+, eSpeak NG, uv package manager)
uv sync                      # Install deps (GPU default)
uv sync --no-default-groups  # CPU-only install

# Run
uv run gradio_app.py         # Web UI at http://127.0.0.1:7860
uv run web_stream_gguf.py    # Real-time streaming at http://localhost:8001
uv run main.py               # Local SDK inference
uv run main_remote.py        # Remote SDK inference (LMDeploy)

# Docker
docker compose --profile gpu up  # GPU mode

# Makefile shortcuts
make check    # Verify toolchain (python, uv, espeak, docker, gpu)
make setup    # Install prereqs + uv sync
make demo     # Launch Gradio UI
make clean    # Remove .venv, __pycache__, .pytest_cache
```

There is no formal test suite or linter configured in this project.

## Architecture

### Core Packages

- **`vieneu/`** — Main SDK package
  - `core.py` — All TTS classes (~1800 lines): `VieNeuTTS`, `FastVieNeuTTS`, `RemoteVieNeuTTS`, `Vieneu`
  - `serve.py` — LMDeploy server orchestration
  - `assets/voices.json` — Preset voice definitions (18 voices) with quantized codec codes
- **`vieneu_utils/`** — Shared text/audio utilities
  - `phonemize_text.py` — Vietnamese text-to-phoneme conversion (uses `phoneme_dict.json`)
  - `normalize_text.py` — Vietnamese text normalization
  - `core_utils.py` — Audio chunk joining, text splitting

### Text Processing Pipeline

```
Raw Text → normalize_text → phonemize_with_dict → split_text_into_chunks (256 chars max)
→ Backbone inference → Codec decoding → overlap-add → Final 24kHz waveform
```

### TTS Class Hierarchy

- **`VieNeuTTS`** — Standard implementation: PyTorch transformers + GGUF via llama-cpp-python. Dual-model: backbone (LLM) + codec (NeuCodec). Supports voice cloning and LoRA adapters.
- **`FastVieNeuTTS`** — Optimized for GGUF CPU inference with streaming (frame-based buffering, <300ms latency).
- **`RemoteVieNeuTTS`** — Client for LMDeploy server via OpenAI-compatible API.
- **`Vieneu`** — Auto-detecting wrapper that selects optimal backend (PyTorch → GGUF → ONNX).

### Key Constants

- Sample rate: 24kHz, hop length: 480, context window: 2048 tokens
- Streaming: 100 frames lookback, 10 lookahead

### Configuration

- `config.yaml` — Defines backbone models (6 variants), codec options (3 types), and text settings
- `pyproject.toml` — CPU version; `pyproject.toml.gpu` — GPU version with CUDA 12.8+
- Platform-specific dependency sources for PyTorch, llama-cpp-python, and LMDeploy

### Fine-tuning (`finetune/`)

Uses LoRA (PEFT) for fine-tuning. Key files: `train.py`, `merge_lora.py`, `create_voices_json.py`, data preprocessing scripts in `data_scripts/`.

### Web UI

`gradio_app.py` (~1280 lines) — Gradio-based interface. Reads model/codec configs from `config.yaml`. Models are lazy-loaded from HuggingFace Hub with local caching.

### System Dependencies

eSpeak NG is required at runtime for phonemization — the `phonemizer` library calls it. Install: `sudo apt install espeak-ng` (Linux), `brew install espeak` (macOS).

claude --resume bf2aab53-30e0-4b6e-b7c2-b74921b1a93e

