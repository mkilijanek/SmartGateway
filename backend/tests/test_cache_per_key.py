"""Response cache scope: shared by every key of the gateway (default) or separate per key (cache_per_key)."""
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


@pytest.fixture(scope="module")
def client():
    db.init_db()
    return TestClient(app)


@pytest.fixture
def upstream(monkeypatch):
    state = {"n": 0}

    def handler(request: httpx.Request):
        state["n"] += 1
        body = json.loads(request.content)
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "model": body["model"], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                                         "choices": [{"index": 0, "message": {"role": "assistant", "content": f"answer {state['n']}"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(proxy, "client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return state


@pytest.fixture(autouse=True)
def clean_logs():
    """Other tests (compression insights) assume an empty request log."""
    yield
    with Session(db.engine) as s:
        s.exec(delete(RequestLog))
        s.commit()


def gateway_with_two_keys(client, per_key):
    with Session(db.engine) as s:
        ep = Endpoint(name="cpk-ep", base_url="http://fake/v1", status="online"); s.add(ep); s.commit(); s.refresh(ep)
        m = Model(endpoint_id=ep.id, upstream_id="up", capabilities=["chat"], max_slots=4, context_length=32000); s.add(m); s.commit(); s.refresh(m)
        ep_id, m_id = ep.id, m.id
    health.status[ep_id] = "online"
    slug = f"cpk-{time.time_ns()}"
    mdl = {"public_name": "mm", "cache_enabled": True, "cache_per_key": per_key, "members": [{"model_id": m_id}]}
    gw = client.post("/api/gateways", json={"name": "g", "slug": slug, "models": [mdl]}).json()
    assert gw["models"][0]["cache_per_key"] is per_key
    keys = [client.post(f"/api/gateways/{gw['id']}/keys", json={"name": n}).json()["key"] for n in ("k1", "k2")]
    return slug, keys


def ask(client, slug, key):
    r = client.post(f"/gw/{slug}/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                    json={"model": "mm", "temperature": 0, "messages": [{"role": "user", "content": "same question"}]})
    assert r.status_code == 200, r.text
    return r.json()["choices"][0]["message"]["content"]


def test_default_shares_the_cache_between_keys(client, upstream):
    slug, (k1, k2) = gateway_with_two_keys(client, per_key=False)
    first = ask(client, slug, k1)
    assert ask(client, slug, k2) == first and upstream["n"] == 1                   # unchanged behaviour: k2 is served k1's cached answer


def test_per_key_cache_isolates_keys_but_still_caches_each(client, upstream):
    slug, (k1, k2) = gateway_with_two_keys(client, per_key=True)
    a1 = ask(client, slug, k1)
    a2 = ask(client, slug, k2)
    assert a1 != a2 and upstream["n"] == 2                                          # k2 did not get k1's answer
    assert ask(client, slug, k1) == a1 and ask(client, slug, k2) == a2 and upstream["n"] == 2   # each key hits its own entry
