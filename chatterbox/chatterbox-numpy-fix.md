# Fix: Chatterbox worker `ModuleNotFoundError: No module named 'numpy'`

## Root cause

`vieneu/chatterbox_backend.py` builds an isolated venv (`.venv-chatterbox`) for
the chatterbox worker on first run. `_ensure_venv()` skips reinstall if the
venv's python binary already exists:

```python
if os.path.exists(_VPY):
    return
```

If the pip install step failed or was interrupted previously, the venv is left
half-built (python binary present, deps missing/incomplete) and never gets
repaired — every later boot short-circuits on the existence check and reuses
the broken venv.

`numpy` was also not pinned explicitly in the install command
(`chatterbox-tts torch==2.6.0 torchaudio==2.6.0 transformers==5.2.0
setuptools<81`), relying on transitive resolution instead.

## Fix applied

`vieneu/chatterbox_backend.py`: added `numpy` explicitly to the `uv pip
install` command so it's guaranteed present regardless of transitive
resolution.

## Steps to apply on an affected host

1. Pull this fix (or apply the same one-line diff to
   `vieneu/chatterbox_backend.py`, adding `"numpy"` to the pip install list).
2. Delete the stale broken venv so it rebuilds clean:
   ```bash
   rm -rf /path/to/VieTTS/.venv-chatterbox
   ```
3. Restart the server (`make` / `./run_with_restart.sh` / whatever launcher is
   used). First boot after deletion rebuilds the venv and reinstalls deps —
   expect it to take longer than normal.
4. Confirm in the log: no more
   `ModuleNotFoundError: No module named 'numpy'` under the Chatterbox
   preload lines.

## Weights "re-downloading" every start

What the log actually shows (`HEAD .../resolve/main/...` → `200`/`302`/`307`)
is HF Hub's normal etag revalidation against already-cached files, not a full
re-download of model weight bytes. This adds network latency at every boot
but does not re-fetch gigabytes each time.

To skip revalidation entirely and boot fully offline (once all needed repos
are already cached at least once):

```bash
export HF_HUB_OFFLINE=1
```

Caveat: any new model repo referenced later needs one online run first to
populate the cache before `HF_HUB_OFFLINE=1` will work for it.

If full weight bytes are genuinely re-downloading every boot (not just HEAD
checks), the cache directory itself isn't persistent between runs. Check:

- `HF_HOME` / `HUGGINGFACE_HUB_CACHE` env vars — unset defaults to
  `~/.cache/huggingface`.
- If running in Docker: confirm the cache dir is a mounted volume, not
  container-local storage that gets wiped on restart.

Also set `HF_TOKEN` to silence the "unauthenticated requests" warning and get
higher rate limits / faster downloads.
