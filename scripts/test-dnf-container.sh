#!/usr/bin/env bash
# Usage:
#   ./scripts/test-dnf-container.sh            # build + interactive shell
#   ./scripts/test-dnf-container.sh <command>   # build + run command
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
docker build -t syncit-dnf-test -f "$SCRIPT_DIR/Dockerfile.dnf-test" "$SCRIPT_DIR"

if [ $# -ge 1 ]; then
  docker run --rm -v "$SCRIPT_DIR:/app" -v /tmp/bundles:/tmp/bundles syncit-dnf-test bash -c "$*"
else
  docker run -it --rm -v "$SCRIPT_DIR:/app" syncit-dnf-test bash
fi