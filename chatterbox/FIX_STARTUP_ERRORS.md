# Fix: chatterbox numpy error + repeat model "redownload" on every startup

## Root causes

1. **`ModuleNotFoundError: No module named 'numpy'` (chatterbox worker)**
   `vieneu/chatterbox_backend.py::_ensure_venv()` only checks whether
   `.venv-chatterbox/bin/python` *exists* — it never verifies the packages
   inside actually installed successfully. If that `uv pip install` run was
   interrupted (disk full, network drop, Ctrl-C), the venv is left half-built
   and every future startup fails the same way forever, since the existence
   check short-circuits a reinstall.

2. **Models "redownloading" every startup**
   They aren't actually redownloading — `huggingface_hub` still makes a live
   HEAD/GET request to `huggingface.co` for every cached file on every launch
   to check the etag, even when the ~39GB already sitting in
   `~/.cache/huggingface` is complete. This is what you see as the `HTTP
   Request: HEAD ...` lines and the `401 Unauthorized` noise for
   `neuphonic/neucodec-onnx-decoder-int8` (a gated repo) — it costs a few
   seconds and a scary-looking warning on every boot, on top of unauthenticated
   rate limits.

## Fix

Run `./scripts/fix_and_predownload.sh` (see below). It:
- rebuilds `.venv-chatterbox` cleanly (fixes numpy / any partial install)
- predownloads every model this app touches into the shared HF cache
- prints the exact env vars to set so startup goes fully offline afterward

Then add to your shell profile (`~/.bashrc`) or `.env` used by `make`:

```bash
export HF_HUB_OFFLINE=1
```

With `HF_HUB_OFFLINE=1`, `hf_hub_download`/`from_pretrained` skip the network
entirely and read straight from cache — no HEAD requests, no 401 noise, no
delay. Only unset it (`HF_HUB_OFFLINE=0` or unset) temporarily if you add a
new model repo that isn't cached yet.

Optional: set `HF_TOKEN` (from https://huggingface.co/settings/tokens) to
raise the unauthenticated rate limit and clear the `neucodec-onnx-decoder-int8`
401 warning on the *first* download (harmless once cached + offline).

Note: the repo's `.env` (`HF_HOME=/root/.cache/huggingface`) is a Docker-only
setting — `flask_app.py` never loads `.env` (no `python-dotenv` call), and on
your host `/root` isn't even readable by your user, so it has zero effect on
`make` / `uv run` startup. Your real cache lives at
`~/.cache/huggingface` (already 39GB, already has everything). No action
needed there unless you also run the Docker path.
