#!/usr/bin/env bash
# Opt-in reproducible build: base images pinned by digest, Python deps installed from the hash-locked requirements.lock,
# npm from package-lock.json (always). Also writes an SBOM next to the image when `syft` is installed.
#   ./tools/strict-build.sh [image-tag]        (default tag: smartgateway:strict)
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${1:-smartgateway:strict}"
NODE_IMAGE="${NODE_IMAGE:-node:22-alpine}"; PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.12-slim}"
digest() { docker buildx imagetools inspect "$1" --format '{{.Manifest.Digest}}'; }
NODE_PIN="${NODE_IMAGE%%@*}@$(digest "$NODE_IMAGE")"; PY_PIN="${PYTHON_IMAGE%%@*}@$(digest "$PYTHON_IMAGE")"
echo "node:   $NODE_PIN"; echo "python: $PY_PIN"
docker build --build-arg NODE_IMAGE="$NODE_PIN" --build-arg PYTHON_IMAGE="$PY_PIN" --build-arg REQUIREMENTS=requirements.lock -t "$TAG" .
if command -v syft >/dev/null; then syft "$TAG" -o spdx-json > "${TAG//[:\/]/_}.sbom.spdx.json" && echo "SBOM written"; fi
