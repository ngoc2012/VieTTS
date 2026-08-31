# VieTTS

A self-hosted **Vietnamese Text-to-Speech studio** built on top of [VieNeu-TTS](https://huggingface.co/pnnbao-ump/VieNeu-TTS) (Vietnamese TTS with instant voice cloning), extended into a full web application with:

- 🎙️ **TTS studio** — multi-row text editor, 6 preset Vietnamese voices, instant voice cloning from 3–5 s of reference audio, low-latency streaming playback
- 🌍 **Extra TTS engines** — Chatterbox (English) and Chatterbox Multilingual (EN/ZH/FR) running in an isolated worker
- 📖 **PDF reader** — upload a PDF, per-page OCR (opendataloader-pdf / EasyOCR / PyMuPDF), automatic **English → Vietnamese translation** (Tencent HY-MT1.5-1.8B, fully local)
- ▶️ **YouTube tool** — download a video, extract audio, generate subtitles (SeamlessM4T), watch inline with subtitle track
- 💳 **Credit-based billing** — SQLite accounts, per-operation pricing, PayPal Checkout top-ups (sandbox & live)
- 🧩 **Browser extension** (Manifest V3) — scrape text from any web page and send it to the TTS studio
- 📦 **Python SDK** (`vieneu` on PyPI) — PyTorch / GGUF / ONNX backends, LMDeploy GPU serving, remote client mode
- ⚙️ **Ops tooling** — Docker images, auto-restart supervisor, ngrok/cloudflared tunnels with email alerts, and a Celery + Redis distributed-compute skeleton

Everything runs **fully offline** (except PayPal and the optional tunnels). Output is 24 kHz WAV.

> This repo is a fork of [pnnbao97/VieNeu-TTS](https://github.com/pnnbao97/VieNeu-TTS). The upstream Gradio UI (`gradio_app.py`) has been replaced by the Flask application described below. Upstream docs: [README.vi.md](README.vi.md) (Vietnamese), [README_PYPI.md](README_PYPI.md) (SDK/PyPI).

---

## Table of contents

1. [Quick start](#quick-start)
2. [The web application](#web-app)
   - [TTS studio](#tts-studio)
   - [Accounts & billing](#billing)
   - [PDF reader & translation](#pdf-reader)
   - [YouTube tool](#youtube)
   - [HTTP API](#http-api)
3. [Browser extension](#extension)
4. [Python SDK (`vieneu`)](#sdk)
5. [TTS models & voices](#models)
6. [Translation engine](#translation)
7. [Fine-tuning](#finetuning)
8. [Deployment](#deployment)
9. [Distributed compute cluster](#distributed)
10. [Configuration reference](#configuration)
11. [Project layout](#layout)
12. [Known issues](#known-issues)
13. [License & credits](#license)

---

## 1. Quick start <a name="quick-start"></a>

**Requirements:** Python 3.12, [uv](https://docs.astral.sh/uv/), eSpeak NG (phonemization), ffmpeg (streaming). Optional: `yt-dlp` at `/usr/local/bin/yt-dlp` (YouTube tool), NVIDIA GPU + CUDA 12.8 (LMDeploy acceleration).

```bash
# Install system deps (Debian/Ubuntu)
sudo apt install espeak-ng ffmpeg

# Check toolchain and install Python deps
make check
make setup            # = prereq check + uv sync   (make setup-cpu for CPU-only)

# Download the translation model (optional, ~4 GB — powers the PDF reader)
uv run download_translation_model.py

# Run the web app (auto-restarting supervisor)
make                  # = ./run_with_restart.sh → uv run --frozen flask_app.py
```

Open **http://localhost:5000**. On first launch the app:

1. Creates `billing.db` (SQLite) and `.flask_secret` (session key),
2. Preloads the default model `VieNeu-TTS-0.3B-q4-gguf` + `NeuCodec ONNX (Fast CPU)` and the Chatterbox variants (Chatterbox builds its own `.venv-chatterbox` on first use — this can take several minutes),
3. Loads the HY-MT1.5 translation model if present in `models/HY-MT1.5-1.8B`.

Register an account on `/login` — new accounts get a **10.00 € welcome credit**.

To expose the app publicly without port-forwarding:

```bash
make cloud            # one-shot cloudflared tunnel
make cloud-auto       # monitored tunnel with auto-restart + email alerts
./tunnel_restart.sh   # ngrok-first, falls back to cloudflared on bandwidth limits
```

---

## 2. The web application <a name="web-app"></a>

`flask_app.py` (single process, port `5000` / `$PORT`). Pages:

| Page | URL | What it does |
|---|---|---|
| TTS studio | `/` | Main synthesis UI |
| Login / register | `/login` | Username + password, cookie sessions |
| Account | `/account` | Balance, top-up (PayPal or test mode), pricing table, recent transactions |
| Profile | `/profile` | Spend summary, full transaction history, change password, delete account |
| PDF reader | `/read` | Upload PDFs, browse recent uploads |
| PDF viewer | `/viewer/<pdf_id>` | Page-by-page reading with OCR + Vietnamese translation |
| YouTube | `/yt` | Download / subtitle / watch YouTube videos |

### TTS studio <a name="tts-studio"></a>

- **Model selector** — choose any backbone from `config.yaml` (6 VieNeu variants + 5 Chatterbox entries) and a codec; models are lazy-loaded and pooled so switching back is instant.
- **Multi-row editor** — add unlimited text rows, *Generate All*, *Download All*, *Stop All*, autoplay-next; state persists in `localStorage`.
- **Preset voices** — 6 bundled Vietnamese voices (see [Models & voices](#models)) with temperature control.
- **Voice cloning** — upload 3–5 s of reference audio + its exact transcript; works on all backends including GGUF.
- **Streaming playback** — audio starts before synthesis finishes: the server pipes live 24 kHz PCM through ffmpeg into Opus/WebM (`/api/stream/<job_id>`) for MediaSource playback.
- **History & trash** — per-username output folders (`outputs/<username>/`) with rename, reorder, merge-to-single-WAV, and a text trash bin.
- **Whitelist / Viet Abbr tabs** — abbreviation passthrough list and custom `KEY: "expansion"` replacements applied before synthesis.
- One synthesis job runs at a time; concurrent requests get HTTP 503 and the UI queues them.

### Accounts & billing <a name="billing"></a>

Credit-based billing in `billing.py` (SQLite, `billing.db`, WAL mode). New accounts get **1000 cents (10 €)**. Charges are atomic (`UPDATE … WHERE balance_cents >= cost`) and taken **before** the operation runs.

| Operation | Price |
|---|---|
| TTS synthesis request | 0.01 € |
| PDF upload | 0.05 € |
| Zone OCR (rectangle select) | 0.01 € |
| Page translation | 0.02 € / page |
| YouTube download | 0.10 € |

**Top-ups via PayPal** (see [PAYPAL_SETUP.md](PAYPAL_SETUP.md)): set `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENV=sandbox|live`. The server creates Orders-v2 orders (1–500 €, EUR only), captures on approval, credits the **amount reported by PayPal** (never the client), and is idempotent per capture ID. Without PayPal credentials the app runs in **test mode**: `/account` shows an instant fake top-up form instead.

CLI utilities:

```bash
python billing.py seed                 # create alice/bob/carol test accounts
python billing.py list                 # list accounts + balances
python billing.py topup <user> <eur>   # manual credit
python billing.py check                # self-test against a temp DB
```

### PDF reader & translation <a name="pdf-reader"></a>

1. Drag-and-drop a PDF on `/read` → pages are rendered at 2× to PNGs under `static/uploads/images/<pdf_id>/`.
2. Open `/viewer/<pdf_id>` — left: page image, right: Vietnamese translation.
3. OCR runs per page with a fallback chain: **opendataloader-pdf** (structured, needs Java 17) → **EasyOCR** → **PyMuPDF text extraction**; results are cached as `page_N_ocr.json`.
4. Translation to Vietnamese runs in a background queue per element; the viewer polls status and prefetches the previous page.
5. Extras: rectangle **zone OCR** (select any region → OCR + translate), full-page overlay, copy OCR/translation, force-retranslate a page, **Translate All** with progress badge and stop button.

### YouTube tool <a name="youtube"></a>

Paste a URL on `/yt` → a 3-stage background pipeline runs: `yt-dlp` download → audio extraction → subtitle generation via SeamlessM4T (`seamless-m4t-medium.py`, produces `.srt`). The page then plays the video inline (`/api/yt/stream/…`, HTTP range requests) with the subtitles served as WebVTT (`/api/yt/vtt/…`). Files land in `downloads/` and can be listed, downloaded, or deleted from the UI.

### HTTP API <a name="http-api"></a>

All endpoints are JSON unless noted; CORS is wide open (`Access-Control-Allow-Origin: *`). 💰 = charges credits (session login required, 401 without a session, 402 on insufficient funds).

**TTS:** `GET /api/models` · `GET /api/codecs` · `POST /api/load_model` `{backbone, codec}` · `GET /api/voices` · 💰 `POST /api/synthesize` (JSON or multipart: `text, voice_id, ref_text, temperature, username, audio_name, ref_audio`) → `{job_id}` · `GET /api/busy` · `GET /api/status/<job_id>` · `GET /api/audio/<job_id>` (WAV) · `GET /api/stream/<job_id>` (chunked WebM/Opus) · `POST /api/cancel/<job_id>`

**History:** `GET /api/history` · `GET/DELETE /api/history/file/<user>/<file>` · `GET /api/history/text/<user>/<stem>` · `POST /api/history/rename|move/…` · `POST /api/history/merge` — plus `GET/POST/PATCH/DELETE /api/trash…`

**PDF/OCR:** 💰 `POST /api/upload_pdf` · `GET /api/pdf_images/<id>` · `GET /api/pdf_text/<id>` · `DELETE /api/pdf/<id>` · `GET /api/ocr/<id>/<page>` (+`/cached`, `/status`, `/dimensions`) · 💰 `POST /api/ocr/<id>/<page>/zone` · 💰 `POST /api/ocr/<id>/<page>/force_translate` · 💰 `POST /api/ocr/<id>/translate_all` (+`/status`, `/stop`) · `POST /api/translate` `{text}` (free)

**YouTube:** 💰 `POST /api/yt/download` `{url}` · `GET /api/yt/status/<job>` · `GET /api/yt/files` · `GET /api/yt/file|stream|vtt/<file>` · `DELETE /api/yt/file/<file>`

**Billing:** `POST /api/paypal/create_order` `{amount}` · `POST /api/paypal/capture_order` `{orderID}` · `POST /account/topup` (test mode only)

Progress is polling-based (no WebSockets). Job state lives in process memory — **run exactly one worker process** (no multi-worker gunicorn).

---

## 3. Browser extension <a name="extension"></a>

A Manifest V3 extension lives in `static/` (`manifest.json`, `background.js`, `content.js`, `popup.html`). Load it unpacked via `chrome://extensions` → *Load unpacked* → select the `static/` directory.

- Clicking the toolbar icon opens the full TTS studio in a standalone 540×700 popup window (survives focus loss).
- The popup has a **Server URL** field (default `http://localhost:5000`) — point it at any running VieTTS instance.
- The **Inspect** button injects `content.js` into the active tab: hover highlights groups of same-tag sibling elements, click captures their text and inserts it as a new row in the studio, ready to synthesize.
- Note: the extension calls the API cross-origin without cookies, so billed endpoints require an anonymous-accessible server or a shared session context.

---

## 4. Python SDK (`vieneu`) <a name="sdk"></a>

```bash
pip install vieneu          # Linux/macOS
# Windows (prebuilt llama-cpp wheel):
pip install vieneu --extra-index-url https://pnnbao97.github.io/llama-cpp-python-v0.3.16/cpu/
```

Exports: `VieNeuTTS`, `FastVieNeuTTS`, `RemoteVieNeuTTS`, and the factory `Vieneu(mode=…)`.

### Basic synthesis

```python
from vieneu import Vieneu

tts = Vieneu()                                  # 0.3B q4 GGUF on CPU by default
audio = tts.infer(text="Xin chào, tôi là VieNeu.")   # default voice "Binh"
tts.save(audio, "output.wav")                   # 24 kHz WAV
```

### Preset voices & cloning

```python
for description, voice_id in tts.list_preset_voices():
    print(voice_id, "-", description)

voice = tts.get_preset_voice("Ngoc")
tts.save(tts.infer(text="Chào bạn.", voice=voice), "ngoc.wav")

# Clone from 3–5 s of reference audio + its EXACT transcript
audio = tts.infer(text="Đây là giọng nói được clone.",
                  ref_audio="examples/audio_ref/example_4.wav",
                  ref_text="Tết là dịp mọi người háo hức đón chào một năm mới với nhiều hy vọng và mong ước.")

# Or encode once and reuse (faster for many calls)
codes = tts.encode_reference("ref.wav")
audio = tts.infer(text="Câu thứ hai.", ref_codes=codes, ref_text="<transcript>")
```

### Streaming (GGUF = true token-level streaming, <300 ms first chunk on CPU)

```python
from vieneu import VieNeuTTS

tts = VieNeuTTS(backbone_repo="pnnbao-ump/VieNeu-TTS-q4-gguf", backbone_device="cpu")
for chunk in tts.infer_stream(text="Câu một. Câu hai.", voice=tts.get_preset_voice("Binh")):
    play(chunk)  # np.float32 @ 24 kHz
```

### GPU serving (LMDeploy) & remote clients

```bash
python -m vieneu.serve --model pnnbao-ump/VieNeu-TTS --port 23333          # requires vieneu[gpu]
python -m vieneu.serve --model pnnbao-ump/VieNeu-TTS --tunnel              # public URL via bore.pub
# or Docker:
docker run --gpus all -p 23333:23333 pnnbao97/vieneu-tts:serve --tunnel
```

```python
from vieneu import Vieneu

tts = Vieneu(mode="remote", api_base="http://server:23333/v1",
             model_name="pnnbao-ump/VieNeu-TTS")     # only the small codec loads locally
tts.save(tts.infer(text="Chế độ remote."), "remote.wav")
# Async batch: await tts.infer_batch_async([...], concurrency_limit=50)  (needs aiohttp)
```

`FastVieNeuTTS` (direct LMDeploy in-process, CUDA only) adds `infer_batch()`, `clone_voice()`, and speaker caching — note it has no `save()`; use `soundfile.write(path, wav, tts.sample_rate)`.

### Chatterbox backend (English / multilingual)

```python
from vieneu.chatterbox_backend import make_chatterbox

box = make_chatterbox({"backend": "chatterbox_mtl", "language_id": "fr"}, "cpu")
wav = box.infer("Bonjour, ceci est un test.")
```

Chatterbox needs torch 2.6 / transformers 5.2 (incompatible with the main env), so it runs **out-of-process**: a first call builds `.venv-chatterbox` and spawns `chatterbox_worker.py` on `127.0.0.1:5099` (`CHATTERBOX_PORT`). The multilingual model supports 23 languages; `config.yaml` wires up EN, ZH, FR.

### Text pipeline

`Raw text → VietnameseTTSNormalizer (numbers, dates, units, currency → Vietnamese words) → phonemize_with_dict (17 MB phoneme dictionary + eSpeak NG fallback) → 256-char chunking → backbone → NeuCodec decode → overlap-add`. Wrap English spans in `<en>…</en>` to protect them from Vietnamese normalization and route them through `en-us` phonemization. Callers always pass raw text — never pre-phonemize.

---

## 5. TTS models & voices <a name="models"></a>

| Backbone (`config.yaml`) | Format | Device | Streaming |
|---|---|---|---|
| VieNeu-TTS (GPU) | PyTorch | GPU | — |
| VieNeu-TTS-0.3B (GPU) | PyTorch | GPU | — |
| VieNeu-TTS-q8-gguf / q4-gguf | GGUF | CPU/GPU | ✅ |
| VieNeu-TTS-0.3B-q8-gguf / q4-gguf | GGUF | CPU/GPU | ✅ |
| Chatterbox (EN) | PyTorch (isolated worker) | CPU/GPU | — |
| Chatterbox Multilingual (EN/ZH/FR) | PyTorch (isolated worker) | CPU/GPU | — |

Codecs: `NeuCodec (Standard)`, `NeuCodec (Distill)`, `NeuCodec ONNX (Fast CPU)` (decode-only int8 — cannot encode reference audio, so cloning with it requires pre-encoded codes).

**Preset voices** (`vieneu/assets/voices.json`, spec `vieneu.voice.presets` v1.0): **Binh** (default, male North), **Tuyen** (male North), **Vinh** (male South), **Doan** (female South), **Ly** (female North), **Ngoc** (female North). Reference audio samples live in `examples/audio_ref/` with matching `.txt` transcripts.

Model details: trained on [VieNeu-TTS-1000h](https://huggingface.co/datasets/pnnbao-ump/VieNeu-TTS-1000h) (443,641 samples), 2048-token context, Perth watermark on by default. Custom models (LoRA / full fine-tunes / GGUF) can be loaded by repo ID — see [docs/CUSTOM_MODEL_USAGE.md](docs/CUSTOM_MODEL_USAGE.md).

---

## 6. Translation engine <a name="translation"></a>

Two independent pipelines:

- **Text EN→VI (integrated):** Tencent **HY-MT1.5-1.8B**, lazy-loaded from `models/HY-MT1.5-1.8B` (`uv run download_translation_model.py` to fetch). Powers `/api/translate` and the entire PDF-viewer translation queue. Runs bf16 on CUDA, fp32 on CPU.
- **Speech→subtitles (YouTube tool):** **SeamlessM4T-medium** (`seamless-m4t-medium.py`) — VAD-segments an audio track and speech-translates each segment into an SRT file (currently `eng→fra`).

---

## 7. Fine-tuning <a name="finetuning"></a>

LoRA fine-tuning on your own voice — full guide in [finetune/README.md](finetune/README.md) (≥12 GB VRAM recommended):

```bash
# 1. Data: finetune/dataset/raw_audio/*.wav (3–15 s clips) + metadata.csv ("file|text")
#    (or bootstrap from HF: uv run finetune/data_scripts/get_hf_sample.py)
uv run finetune/data_scripts/filter_data.py    # → metadata_cleaned.csv (drops bad audio/text)
uv run finetune/data_scripts/encode_data.py    # → metadata_encoded.csv (NeuCodec tokens)

# 2. Edit finetune/configs/lora_config.py (base model, r=16, lr=2e-4, max_steps=5000, …)
uv run finetune/train.py                       # run from repo root; saves to finetune/output/

# 3. Ship it
uv run finetune/merge_lora.py --base_model pnnbao-ump/VieNeu-TTS-0.3B \
    --adapter finetune/output/<run> --output merged_model
uv run finetune/create_voices_json.py --audio my_voice.wav --text "<exact transcript>" --name MyVoice
# → copy voices.json into the merged model dir and upload to HF; users then just:
#   Vieneu(backbone_repo="you/your-model").infer(text="...")
```

A Colab notebook (`finetune/finetune_VieNeu-TTS.ipynb`) covers the same flow end-to-end. LoRA adapters can also be loaded at runtime: `tts.load_lora_adapter("repo/or/path")` (PyTorch backends only).

---

## 8. Deployment <a name="deployment"></a>

### Local, supervised

`make` runs `run_with_restart.sh`: an infinite supervisor around `uv run --frozen flask_app.py` that restarts on any crash (with segfault detection). Tunnel supervisors (`cloud_restart.sh`, `ngrok_restart.sh`, `tunnel_restart.sh`) add health-checked public URLs with automatic restarts and optional Gmail alerts (`SMTP_USER`/`SMTP_PASS`/`EMAIL_TO`). `tunnel_restart.sh` is the most robust: ngrok first, automatic fallback to cloudflared when ngrok hits bandwidth limits.

### Docker

| Compose file | What it runs |
|---|---|
| `docker-compose.yml` (`--profile gpu`) | Dev: CUDA 12.8 image, repo hot-mounted at `/workspace`, port 7860 |
| `docker-compose.build.yml` | Builds/pushes the prod image (`IMAGE_NAME`/`IMAGE_TAG` from `.env`) |
| `docker-compose.prod.yml` (`--profile gpu`) | Prod: pulls the baked image, no volume mounts |
| `docker-compose.light.yml` | CPU stack: Flask app (`Dockerfile.app-light`) + cloudflared monitor sidecar |
| `distributed/docker-compose.yml` | Redis + Flower + Celery worker (see below) |

Production path (see [docs/Deploy.md](docs/Deploy.md)): build → push → on the server `docker compose -f docker-compose.prod.yml --profile gpu pull && … up -d`. The API-server image is published as `pnnbao97/vieneu-tts:serve` (LMDeploy + built-in bore tunnel, port 23333); rebuild with `make docker-build-serve`.

Makefile shortcuts: `make check` (toolchain report), `make setup` / `setup-cpu`, `make` (run app), `make cloud` / `cloud-auto` (tunnels), `make re` / `detach` / `down` (light stack), `make docker-gpu`, `make clean`.

---

## 9. Distributed compute cluster <a name="distributed"></a>

`distributed/` contains a runnable **Celery 5.4 + Redis 7 + Flower** skeleton for fanning heavy jobs out to LAN workers (design docs: [distributed/ARCHITECTURE.md](distributed/ARCHITECTURE.md), [distributed-skeleton.md](distributed-skeleton.md)).

```bash
# Main node: Redis (6379) + Flower dashboard (5555) + one local worker
cd distributed && cp .env.example .env && docker compose up -d

# Any other machine on the LAN:
pip install -r distributed/requirements.txt
MAIN_IP=<main-node-ip> REDIS_PASSWORD=<pw> celery -A celery_app worker -l info --concurrency=4 -n worker1@%h

# Submit test jobs:
MAIN_IP=<ip> REDIS_PASSWORD=<pw> python distributed/submit.py 10
```

Reliability is preconfigured (acks-late, prefetch 1, retries with backoff, 10-min task timeout); results go to a Redis `jobs` hash. The single task `heavy_compute` currently runs a placeholder CPU workload — **TTS inference is not wired in yet** (replace `do_work()` in `distributed/tasks.py`). Security stance is trusted-LAN only: change `REDIS_PASSWORD` and note Flower has no auth.

---

## 10. Configuration reference <a name="configuration"></a>

Environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5000` | Flask listen port |
| `DIRECT_HOST` | auto-detected public IP | Host used for direct heavy-audio URLs (bypasses tunnel bandwidth) |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | — | Enable real PayPal top-ups (unset = test mode) |
| `PAYPAL_ENV` | `sandbox` | `live` for production PayPal |
| `CHATTERBOX_PORT` | `5099` | Isolated Chatterbox worker port |
| `PRELOAD_BACKBONES` | (default set) | Backbones to load at startup |
| `PHONEME_DICT_PATH` | bundled | Override the phoneme dictionary |
| `PHONEMIZER_ESPEAK_LIBRARY` | auto-probed | Path to `libespeak-ng` |
| `SMTP_USER` / `SMTP_PASS` / `EMAIL_TO` | — | Gmail alerts from the tunnel supervisors |
| `MAIN_IP` / `REDIS_PASSWORD` | `127.0.0.1` / `changeme` | Distributed cluster connection |

State on disk: `billing.db` (accounts/transactions), `.flask_secret` (delete to invalidate all sessions), `outputs/<username>/` (synthesized WAVs + trash), `static/uploads/` (PDFs, page images, OCR caches), `downloads/` (YouTube media), `models/HY-MT1.5-1.8B` (translation model), `.venv-chatterbox/` (isolated Chatterbox env), `merged_models_cache/` (merged LoRA models).

Dependency variants: `pyproject.toml` (active, CPU torch), `pyproject.toml.gpu` (CUDA 12.8 + LMDeploy — swap in for GPU installs), `pyproject.toml.cpu` (minimal SDK-only).

---

## 11. Project layout <a name="layout"></a>

```
flask_app.py            Web application (all routes, job queues, streaming)
billing.py              Credit accounts, transactions, PayPal-independent core + CLI
chatterbox_worker.py    Isolated Chatterbox HTTP worker (spawned automatically)
vieneu/                 SDK: core.py (TTS classes), serve.py (LMDeploy), chatterbox_backend.py,
                        assets/voices.json (6 presets)
vieneu_utils/           Vietnamese normalization, phonemization (+17 MB dict), chunking
templates/ static/      Web UI (index/read/viewer/yt/account/profile) + browser extension
client/client.html      Standalone React demo for the SDK streaming server (not the Flask app)
finetune/               LoRA training pipeline (data_scripts → train → merge → voices.json)
distributed/            Celery + Redis + Flower cluster skeleton
docker/  docs/          Dockerfiles; deploy, Makefile, custom-model guides
examples/audio_ref/     Sample reference audio + transcripts for cloning
seamless-m4t-medium.py  Speech→SRT subtitle generator (used by the YouTube tool)
download_translation_model.py   Fetch HY-MT1.5-1.8B into models/
test_*.py               Scratch experiments (Chatterbox, VoxCPM2, TinyTTS, translation, …)
```

---

## 12. Known issues <a name="known-issues"></a>

- `make demo` and `docker-compose.yml`'s command still reference `gradio_app.py`, which was removed when the Flask app replaced the Gradio UI. Use `make` instead.
- `docker-compose.light.yml` maps port 7860, but the Flask app inside listens on 5000.
- `make nocache` has a broken line continuation and will fail.
- `README_PYPI.md` links `main.py` / `main_remote.py`, which no longer exist (the code samples themselves are still API-accurate).
- `requirements.txt` is a cu118 freeze snapshot that conflicts with the cu128 Docker images; `pyproject.toml`/`uv.lock` are the source of truth.
- The wheel's `package-data` includes `assets/samples/*` but not `assets/voices.json`.
- If Chatterbox fails with `ModuleNotFoundError` after an interrupted install, `rm -rf .venv-chatterbox` and restart (see [docs/chatterbox-numpy-fix.md](docs/chatterbox-numpy-fix.md)).
- History files and most read endpoints are unauthenticated by design (single-user/LAN assumption); only credit-charging endpoints require login. Review before exposing publicly.

---

## 13. License & credits <a name="license"></a>

Built on **VieNeu-TTS** by Phạm Nguyễn Ngọc Bảo ([pnnbao-ump](https://huggingface.co/pnnbao-ump)) — itself built on [NeuTTS Air](https://huggingface.co/neuphonic/neutts-air) and [NeuCodec](https://huggingface.co/neuphonic/neucodec) by Neuphonic.

- **VieNeu-TTS (0.5B):** Apache 2.0
- **VieNeu-TTS-0.3B:** CC BY-NC 4.0 (non-commercial; contact the author for commercial licensing)
- Chatterbox by Resemble AI; HY-MT1.5 by Tencent; SeamlessM4T by Meta — each under its own license.

```bibtex
@misc{vieneutts2026,
  title        = {VieNeu-TTS: Vietnamese Text-to-Speech with Instant Voice Cloning},
  author       = {Pham Nguyen Ngoc Bao},
  year         = {2026},
  publisher    = {Hugging Face},
  howpublished = {\url{https://huggingface.co/pnnbao-ump/VieNeu-TTS}}
}
```
