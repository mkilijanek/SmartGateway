#!/usr/bin/env bash
# Regenerate backend/requirements.lock (with hashes) from backend/requirements.txt. Run after changing a dependency.
set -euo pipefail
cd "$(dirname "$0")/../backend"
command -v pip-compile >/dev/null || { echo 'install pip-tools first (pip install pip-tools, ideally in a venv)'; exit 1; }
pip-compile -q --generate-hashes --allow-unsafe --strip-extras -o requirements.lock requirements.txt
