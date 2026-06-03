#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
docker build -t syncit-apt-test -f "$SCRIPT_DIR/Dockerfile.apt-test" "$SCRIPT_DIR"
docker run -it --rm -v "$SCRIPT_DIR:/app" syncit-apt-test bash