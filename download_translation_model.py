#!/usr/bin/env python3
"""Download HY-MT1.5-1.8B translation model from Hugging Face."""

import os
from pathlib import Path
from huggingface_hub import snapshot_download

model_id = "tencent/HY-MT1.5-1.8B"
models_dir = Path(__file__).parent / "models"
model_dir = models_dir / "HY-MT1.5-1.8B"

print(f"Downloading {model_id}...")
print(f"Target directory: {model_dir}")

snapshot_download(
    repo_id=model_id,
    local_dir=str(model_dir),
    local_dir_use_symlinks=False,
)

print(f"✓ Model downloaded to {model_dir}")
print(f"\nDirectory contents:")
for item in model_dir.iterdir():
    print(f"  - {item.name}")
