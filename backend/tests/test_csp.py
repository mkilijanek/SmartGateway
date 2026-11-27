"""The admin UI's Content-Security-Policy: strict, but the Playground plays generated audio from blob: URLs, which `default-src 'self'` alone blocks."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def directives():
    csp = client.get("/healthz").headers["content-security-policy"]
    return {d.split()[0]: d.split()[1:] for d in (x.strip() for x in csp.split(";")) if d}


def test_blob_media_is_allowed_so_generated_audio_can_play():
    d = directives()
    assert d["media-src"] == ["'self'", "blob:"]            # without it the <audio> of the Playground gets "src not supported" and cannot play
    assert "blob:" in d["img-src"]


def test_the_rest_of_the_policy_stays_strict():
    d = directives()
    assert d["default-src"] == ["'self'"] and d["script-src"] == ["'self'"] and d["connect-src"] == ["'self'"]
    assert d["frame-ancestors"] == ["'none'"] and d["object-src"] == ["'none'"] and d["base-uri"] == ["'self'"]
    assert "'unsafe-eval'" not in " ".join(sum(d.values(), [])) and "*" not in sum(d.values(), [])
