#!/usr/bin/env bash
set -euo pipefail

# musl runtime lib needed by llama-cpp-python's prebuilt libggml/libllama binaries
# (built against musl for portability; missing on glibc distros like Ubuntu)
sudo apt-get install -y musl
sudo ln -sf /lib/x86_64-linux-musl/libc.so /lib/x86_64-linux-gnu/libc.musl-x86_64.so.1
sudo ldconfig
sudo apt install ffmpeg
