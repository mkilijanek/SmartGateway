"""The published address is configurable with SG_BIND; unset it stays 0.0.0.0, as before."""
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("path", ["docker-compose.yml", "deploy/docker-compose.yml"])
def test_port_is_published_on_sg_bind_defaulting_to_all_interfaces(path):
    ports = yaml.safe_load((ROOT / path).read_text())["services"]["smartgateway"]["ports"]
    assert ports == ["${SG_BIND:-0.0.0.0}:${SG_PORT:-8080}:8080"]


@pytest.mark.parametrize("path", [".env.example", "deploy/.env.example"])
def test_env_examples_document_it_commented_out(path):
    text = (ROOT / path).read_text()
    assert "# SG_BIND=127.0.0.1" in text and "\nSG_BIND=" not in text      # an example must not change the default when copied
