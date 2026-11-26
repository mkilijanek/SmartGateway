import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_lock_covers_requirements_and_has_hashes():
    lock = (ROOT / "backend" / "requirements.lock").read_text().lower()
    for line in (ROOT / "backend" / "requirements.txt").read_text().splitlines():
        name, _, ver = line.partition("==")
        name = re.sub(r"\[.*\]", "", name).strip().lower().replace("_", "-")
        if name:
            assert f"{name}=={ver.strip()}" in lock, name
    entries = re.findall(r"^[a-z0-9_.-]+==", lock, re.M)
    assert entries and lock.count("--hash=sha256:") >= len(entries)


def test_dockerfile_default_is_unchanged_and_strict_is_opt_in():
    d = (ROOT / "Dockerfile").read_text()
    assert "ARG NODE_IMAGE=node:22-alpine" in d and "ARG PYTHON_IMAGE=python:3.12-slim" in d
    assert "ARG REQUIREMENTS=requirements.txt" in d
    assert "npm ci" in d and "npm install" not in d
