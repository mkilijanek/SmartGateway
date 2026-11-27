"""Per-model audio defaults: a default voice / format / speed for speech and a default language / format for transcription, applied only when the
client left that setting out, and only on the audio routes."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from app import db, proxy
from app.main import app
from app.models import Endpoint, Model, RequestLog
from app.router import health


class Up:
    def __init__(self):
        self.calls: list = []   # (path, content-type, body bytes)

    def handler(self, request: httpx.Request):
        p = request.url.path
        self.calls.append((p, request.headers.get("content-type", ""), request.content))
        if p.endswith("/audio/speech"):
            return httpx.Response(200, content=b"AUDIO", headers={"content-type": "audio/mpeg"})
        if "/audio/" in p:
            return httpx.Response(200, json={"text": "hello"})
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


@pytest.fixture(scope="module")
def client():
    db.init_db()
    yield TestClient(app)
    with Session(db.engine) as s:
        s.exec(delete(RequestLog))
        s.commit()


@pytest.fixture
def up(monkeypatch):
    u = Up()
    monkeypatch.setattr(proxy, "client", httpx.AsyncClient(transport=httpx.MockTransport(u.handler)))
    return u


def mk(caps):
    with Session(db.engine) as s:
        ep = Endpoint(name=f"ad-{time.time_ns()}", base_url="http://up/v1", status="online"); s.add(ep); s.commit(); s.refresh(ep)
        m = Model(endpoint_id=ep.id, upstream_id="u", capabilities=caps, enabled=True, max_slots=2); s.add(m); s.commit(); s.refresh(m)
        health.status[ep.id] = "online"
        return m.id


def gateway(client, **model_fields):
    mid = mk(model_fields.pop("caps"))
    r = client.post("/api/gateways", json={"name": f"ad-{time.time_ns()}", "models": [{"public_name": "m", "members": [{"model_id": mid}], **model_fields}]})
    return r


def ok_gateway(client, **kw):
    r = gateway(client, **kw)
    assert r.status_code == 200, r.text
    return r.json()


def url(g, route):
    return f"/api/admin-gw/{g['slug']}/v1/{route}"


SPEECH = {"speech": {"voice": "af", "response_format": "wav", "speed": 1.25}}


def test_speech_defaults_fill_what_the_client_left_out(client, up):
    g = ok_gateway(client, caps=["tts"], voices=["af", "am"], audio_defaults=SPEECH)
    assert g["models"][0]["audio_defaults"] == SPEECH
    assert client.post(url(g, "audio/speech"), json={"model": "m", "input": "hi"}).status_code == 200
    assert json.loads(up.calls[-1][2]) == {"model": "u", "input": "hi", "voice": "af", "response_format": "wav", "speed": 1.25}
    client.post(url(g, "audio/speech"), json={"model": "m", "input": "hi", "voice": "am", "speed": 2.0})          # the client's own values win
    sent = json.loads(up.calls[-1][2])
    assert sent["voice"] == "am" and sent["speed"] == 2.0 and sent["response_format"] == "wav"


def test_transcription_defaults_apply_to_multipart_requests_and_translations_get_no_language(client, up):
    g = ok_gateway(client, caps=["audio"], audio_defaults={"transcription": {"language": "pl", "response_format": "verbose_json", "temperature": 0.2}})
    files = {"file": ("a.wav", b"RIFFxxxx", "audio/wav")}
    client.post(url(g, "audio/transcriptions"), data={"model": "m"}, files=files)
    body = up.calls[-1][2].decode("latin-1")
    assert 'name="language"\r\n\r\npl' in body and 'name="response_format"\r\n\r\nverbose_json' in body and 'name="temperature"\r\n\r\n0.2' in body
    client.post(url(g, "audio/transcriptions"), data={"model": "m", "language": "en"}, files=files)
    body = up.calls[-1][2].decode("latin-1")
    assert body.count('name="language"') == 1 and 'name="language"\r\n\r\nen' in body
    client.post(url(g, "audio/translations"), data={"model": "m"}, files=files)
    body = up.calls[-1][2].decode("latin-1")
    assert 'name="language"' not in body and 'name="response_format"\r\n\r\nverbose_json' in body


def test_chat_and_other_models_are_untouched(client, up):
    g = ok_gateway(client, caps=["chat"], audio_defaults=SPEECH, default_params={"temperature": 0.3})
    client.post(url(g, "chat/completions"), json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
    sent = json.loads(up.calls[-1][2])
    assert "voice" not in sent and "speed" not in sent and sent["temperature"] == 0.3
    plain = ok_gateway(client, caps=["tts"])
    assert plain["models"][0]["audio_defaults"] is None
    client.post(url(plain, "audio/speech"), json={"model": "m", "input": "hi"})
    assert json.loads(up.calls[-1][2]) == {"model": "u", "input": "hi"}


@pytest.mark.parametrize("defaults,msg", [
    ({"speech": {"response_format": "ogg"}}, "Speech format"),
    ({"speech": {"speed": 9}}, "between 0.25 and 4"),
    ({"speech": {"speed": "fast"}}, "must be a number"),
    ({"speech": {"voice": "zz"}}, "not in this model's voice list"),
    ({"transcription": {"language": "p1"}}, "language code"),
    ({"transcription": {"response_format": "mp3"}}, "Transcription format"),
    ({"transcription": {"temperature": 2}}, "between 0 and 1"),
])
def test_invalid_defaults_are_refused_with_a_clear_message(client, defaults, msg):
    r = gateway(client, caps=["tts"], voices=["af"], audio_defaults=defaults)
    assert r.status_code == 400 and msg in r.json()["detail"] and "'m'" in r.json()["detail"]


def test_defaults_are_normalised_updated_and_cleared(client, up):
    g = ok_gateway(client, caps=["tts"], audio_defaults={"speech": {"voice": " af ", "speed": "1.5", "bogus": 1}, "other": {"x": 1}, "transcription": {}})
    assert g["models"][0]["audio_defaults"] == {"speech": {"voice": "af", "speed": 1.5}}          # trimmed, typed, unknown keys dropped
    body = {"name": g["name"], "slug": g["slug"], "models": [{**{k: v for k, v in g["models"][0].items() if k in ("public_name", "enabled")},
                                                              "members": [{"model_id": g["models"][0]["members"][0]["model_id"]}], "audio_defaults": {}}]}
    r = client.put(f"/api/gateways/{g['id']}", json=body)
    assert r.status_code == 200 and r.json()["models"][0]["audio_defaults"] is None


# ------------------------------------------------------------------ models exposed directly on the unified endpoint
def direct(client, caps, **sel):
    mid = mk(caps)
    name = f"direct-{time.time_ns()}"
    r = client.put("/api/unified", json={"enabled": True, "gateways": [], "models": [{"model_id": mid, "name": name, **sel}]})
    return mid, name, r


def test_unified_direct_models_get_audio_defaults_too(client, up):
    try:
        mid, name, r = direct(client, ["tts"], voices=["af", "am"], audio_defaults=SPEECH)
        assert r.status_code == 200, r.text
        view = {m["name"]: m for m in r.json()["models"]}
        assert view[name]["audio_defaults"] == SPEECH                                                     # shown back to the editor
        assert client.post("/unified/v1/audio/speech", json={"model": name, "input": "hi"}).status_code == 200
        assert json.loads(up.calls[-1][2]) == {"model": "u", "input": "hi", "voice": "af", "response_format": "wav", "speed": 1.25}
        client.post("/unified/v1/audio/speech", json={"model": name, "input": "hi", "voice": "am"})        # the client's own value wins
        assert json.loads(up.calls[-1][2])["voice"] == "am"
        # editing: new defaults replace the old ones, an empty object clears them
        mid2, name2, r2 = direct(client, ["audio"], audio_defaults={"transcription": {"language": "de"}})
        assert r2.status_code == 200
        client.post("/unified/v1/audio/transcriptions", data={"model": name2}, files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")})
        assert 'name="language"\r\n\r\nde' in up.calls[-1][2].decode("latin-1")
        r3 = client.put("/api/unified", json={"enabled": True, "gateways": [], "models": [{"model_id": mid2, "name": name2, "audio_defaults": {}}]})
        assert r3.json()["models"][0]["audio_defaults"] is None
    finally:
        client.put("/api/unified", json={"enabled": False, "gateways": [], "models": []})


def test_unified_direct_audio_defaults_are_validated_and_only_kept_for_audio_models(client, up):
    try:
        _, name, r = direct(client, ["tts"], voices=["af"], audio_defaults={"speech": {"voice": "zz"}})
        assert r.status_code == 400 and "not in this model's voice list" in r.json()["detail"] and name in r.json()["detail"]
        _, _, r = direct(client, ["tts"], audio_defaults={"speech": {"speed": 9}})
        assert r.status_code == 400 and "between 0.25 and 4" in r.json()["detail"]
        _, name, r = direct(client, ["chat"], audio_defaults=SPEECH)                                       # a chat model has no audio settings: dropped
        assert r.status_code == 200 and r.json()["models"][0]["audio_defaults"] is None
    finally:
        client.put("/api/unified", json={"enabled": False, "gateways": [], "models": []})


# ------------------------------------------------------------------ what a public model can do (the Playground offers chat / speak / transcribe from this)
from app.voices import abilities


def test_abilities_are_decided_per_upstream_model():
    assert abilities([["chat", "tools"]]) == {"chat": True, "tts": False, "stt": False, "capabilities": ["chat", "tools"]}
    assert abilities([["audio", "tts"]])["tts"] and not abilities([["audio", "tts"]])["stt"]       # a TTS model is tagged audio too: not speech-to-text
    assert abilities([["audio", "chat"]]) == {"chat": True, "tts": False, "stt": True, "capabilities": ["audio", "chat"]}
    both = abilities([["audio", "tts"], ["audio", "chat"]])                                          # a TTS upstream plus a transcription upstream
    assert both["tts"] and both["stt"] and both["chat"]
    assert abilities([]) == {"chat": False, "tts": False, "stt": False, "capabilities": []}


def test_gateway_and_unified_output_tell_what_each_name_can_do(client, up):
    tts, stt, chat = mk(["audio", "tts"]), mk(["audio", "chat"]), mk(["chat"])
    g = client.post("/api/gateways", json={"name": f"ab-{time.time_ns()}", "models": [
        {"public_name": "speak", "members": [{"model_id": tts}]}, {"public_name": "listen", "members": [{"model_id": stt}]},
        {"public_name": "talk", "members": [{"model_id": chat}]}, {"public_name": "all", "members": [{"model_id": tts}, {"model_id": stt}]}]}).json()
    ab = {m["public_name"]: m["abilities"] for m in g["models"]}
    assert (ab["speak"]["tts"], ab["speak"]["stt"], ab["speak"]["chat"]) == (True, False, False)
    assert (ab["listen"]["tts"], ab["listen"]["stt"], ab["listen"]["chat"]) == (False, True, True)
    assert (ab["talk"]["tts"], ab["talk"]["stt"], ab["talk"]["chat"]) == (False, False, True)
    assert (ab["all"]["tts"], ab["all"]["stt"]) == (True, True)
    try:
        direct_tts = mk(["tts"])
        r = client.put("/api/unified", json={"enabled": True, "gateways": [{"id": g["id"], "model": "listen", "aliases": ["whisper-1"]}],
                                             "models": [{"model_id": direct_tts, "name": "kokoro-d", "aliases": ["tts-1"]}]}).json()
        a = r["model_abilities"]
        assert a["kokoro-d"]["tts"] and not a["kokoro-d"]["stt"] and a["tts-1"]["tts"]                  # a directly exposed model and its alias
        gname = g["name"]
        assert a[gname]["stt"] and a["whisper-1"]["stt"]                                                  # a gateway name resolves to its default model
        assert a[f"{gname}/speak"]["tts"] and a[f"{gname}/talk"]["chat"] and not a[f"{gname}/talk"]["tts"]
    finally:
        client.put("/api/unified", json={"enabled": False, "gateways": [], "models": []})
