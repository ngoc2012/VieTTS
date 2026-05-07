#!/usr/bin/env python3
from huggingface_hub import snapshot_download
import os

model_id = "tencent/HY-MT1.5-1.8B"
save_dir = "downloads/models/HY-MT1.5-1.8B"

os.makedirs(save_dir, exist_ok=True)
print(f"Downloading {model_id}...")
path = snapshot_download(repo_id=model_id, local_dir=save_dir, local_dir_use_symlinks=False)
print(f"Done! Model at: {path}")

files = os.listdir(path)
print(f"\nDownloaded files:")
for f in sorted(files)[:10]:
    full_path = os.path.join(path, f)
    if os.path.isfile(full_path):
        size_mb = os.path.getsize(full_path) / (1024**2)
        print(f"  {f} ({size_mb:.1f} MB)")
    else:
        print(f"  {f}/ (directory)")
