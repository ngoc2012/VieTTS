#!/usr/bin/env bash
set -euo pipefail

# musl runtime lib needed by llama-cpp-python's prebuilt libggml/libllama binaries
# (built against musl for portability; missing on glibc distros like Ubuntu)
sudo apt-get install -y musl
