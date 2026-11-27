"""ChatGPT / Codex subscription accounts: device-code sign-in, token refresh, chat/completions <-> Responses translation, failover."""
import asyncio
import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, delete

from app import db, proxy
from app.accounts import tokens
from app.main import app
from app.models import Endpoint, OAuthAccount, RequestLog
from app.router import health

AUTH, BACKEND = "auth.openai.com", "chatgpt.com"


def jwt(claims: dict) -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{b({'alg': 'none'})}.{b(claims)}.sig"


def tokens_for(n: int, email="me@example.com", acct="acct-1", expires_in=3600) -> dict:
    claim = {"https://api.openai.com/auth": {"chatgpt_account_id": acct, "chatgpt_plan_type": "plus"}}
    return {"access_token": jwt({**claim, "n": n}), "refresh_token": f"refresh-{n}", "id_token": jwt({**claim, "email": email}), "expires_in": expires_in}


def sse(*events) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


TEXT = sse({"type": "response.created", "response": {}},
           {"type": "response.output_text.delta", "delta": "Hello "}, {"type": "response.output_text.delta", "delta": "world"},
           {"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15,
                                                                                           "output_tokens_details": {"reasoning_tokens": 1}}}})
TOOL = sse({"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "get_weather"}},
           {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"city":'},
           {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '"Paris"}'},
           {"type": "response.completed", "response": {"usage": {"input_tokens": 20, "output_tokens": 5}}})


class Vendor:
    """Mock OpenAI: the auth server and the Codex backend."""

    def __init__(self):
        self.n = 0                       # token generation counter
        self.polls_before_ok = 1         # device polls answered "pending" first
        self.next_login = dict(email="me@example.com", acct="acct-1")
        self.refresh_fail = None         # e.g. "invalid_grant"
        self.refreshes = 0
        self.backend: list = []          # replies for /responses, popped in order; last one repeats: (status, body)
        self.calls: list = []            # (path, headers, json body)

    def issue(self, **kw):
        self.n += 1
        return tokens_for(self.n, **kw)

    async def handler(self, request: httpx.Request):
        path = request.url.path
        body = request.content.decode() if request.content else ""
        if request.url.host == AUTH:
            if path.endswith("/deviceauth/usercode"):
                return httpx.Response(200, json={"device_auth_id": "dev-1", "user_code": "ABCD-EFGH", "interval": "2"})
            if path.endswith("/deviceauth/token"):
                if self.polls_before_ok > 0:
                    self.polls_before_ok -= 1
                    return httpx.Response(403, json={"error": "authorization_pending"})
                return httpx.Response(200, json={"authorization_code": "code-1", "code_verifier": "ver", "code_challenge": "chal"})
            if path.endswith("/oauth/token"):
                form = dict(x.split("=", 1) for x in body.split("&"))
                if form["grant_type"] == "refresh_token":
                    self.refreshes += 1
                    await asyncio.sleep(0.05)   # a real network round trip: concurrent callers overlap here
                    if self.refresh_fail:
                        return httpx.Response(400, json={"error": self.refresh_fail})
                    return httpx.Response(200, json=self.issue(**self.next_login))
                return httpx.Response(200, json=self.issue(**self.next_login))
        if request.url.host == BACKEND:
            if path.endswith("/models"):
                self.calls.append((path, dict(request.headers), None))
                return httpx.Response(404, json={"detail": "no"})
            if path.endswith("/responses"):
                self.calls.append((path, dict(request.headers), json.loads(body)))
                status, payload = self.backend[0] if len(self.backend) == 1 else self.backend.pop(0)
                if isinstance(payload, str):
                    return httpx.Response(status, text=payload, headers={"content-type": "text/event-stream"})
                return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"detail": "unexpected " + str(request.url)})


@pytest.fixture
def v(monkeypatch):
    vendor = Vendor()
    vendor.backend = [(200, TEXT)]
    monkeypatch.setattr(proxy, "client", httpx.AsyncClient(transport=httpx.MockTransport(vendor.handler)))
    tokens._locks.clear()
    return vendor


@pytest.fixture(scope="module")
def client():
    db.init_db()
    yield TestClient(app)
    with Session(db.engine) as s:   # test_compression_api assumes an empty request log
        s.exec(delete(RequestLog))
        s.commit()


def sign_in(client, name=None, endpoint_id=None):
    s = client.post("/api/accounts/codex/start", json={"name": name, "endpoint_id": endpoint_id})
    assert s.status_code == 200, s.text
    sid = s.json()["session_id"]
    for _ in range(5):
        r = client.post("/api/accounts/codex/poll", json={"session_id": sid})
        assert r.status_code == 200, r.text
        if r.json()["status"] == "done":
            return s.json(), r.json()
    raise AssertionError("never finished")


def enable(client, ep, upstream_id):
    mid = next(m["id"] for m in ep["models"] if m["upstream_id"] == upstream_id)
    assert client.patch(f"/api/models/{mid}", json={"enabled": True}).status_code == 200
    return mid


def make_gw(client, ep):
    mid = enable(client, ep, "gpt-5.5")
    g = client.post("/api/gateways", json={"name": f"cx-{time.time_ns()}", "models": [{"public_name": "codex", "members": [{"model_id": mid}]}]})
    assert g.status_code == 200, g.text
    return g.json()


def chat(client, g, route="chat/completions", admin=True, **body):
    base = f"/api/admin-gw/{g['slug']}/v1" if admin else f"/gw/{g['slug']}/v1"
    return client.post(f"{base}/{route}", json={"model": "codex", **body})


def cleanup(client, *eps):
    for ep in eps:
        client.delete(f"/api/endpoints/{ep['id']}")


def test_sign_in_creates_a_usable_account(client, v):
    start, done = sign_in(client)
    assert start["user_code"] == "ABCD-EFGH" and start["verification_url"].endswith("/codex/device") and start["interval"] == 2
    ep = done["endpoint"]
    assert done["created"] and ep["provider"] == "codex-oauth" and ep["account"]["email"] == "me@example.com" and ep["account"]["plan"] == "plus"
    assert ep["account"]["needs_login"] is False and ep["api_key"] is False
    assert {m["upstream_id"] for m in ep["models"]} >= {"gpt-5.5", "gpt-5.4"} and not any(m["enabled"] for m in ep["models"])   # new models stay off until enabled, as on any endpoint
    # no token anywhere an admin (or a log reader) can see
    with db.session() as s:
        acc = s.get(OAuthAccount, ep["id"])
        access, refresh = acc.access_token, acc.refresh_token
        raw = s.exec(text("select access_token, refresh_token from oauthaccount where endpoint_id = :i").bindparams(i=ep["id"])).one()
    assert raw[0] != access and raw[1] != refresh and "refresh-" not in raw[1]             # encrypted at rest
    for url in ("/api/endpoints", "/api/accounts"):
        body = client.get(url).text
        assert access not in body and refresh not in body and "refresh-" not in body
    assert client.get("/api/accounts").json()[0]["email"] == "me@example.com"
    cleanup(client, ep)


def test_models_stay_off_until_enabled_and_the_choice_survives_rescan_and_sign_in(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    mid = next(m["id"] for m in ep["models"] if m["upstream_id"] == "gpt-5.5")
    g = client.post("/api/gateways", json={"name": f"off-{time.time_ns()}", "models": [{"public_name": "codex", "members": [{"model_id": mid}]}]}).json()
    assert chat(client, g, messages=[{"role": "user", "content": "hi"}]).status_code in (400, 404, 502)   # a disabled model is not served
    enable(client, ep, "gpt-5.5")
    assert chat(client, g, messages=[{"role": "user", "content": "hi"}]).status_code == 200
    re = client.post(f"/api/endpoints/{ep['id']}/rescan").json()
    on = {m["upstream_id"]: m["enabled"] for m in re["models"]}
    assert on["gpt-5.5"] is True and on["gpt-5.4"] is False
    v.polls_before_ok = 0
    _, again = sign_in(client, endpoint_id=ep["id"])
    on = {m["upstream_id"]: m["enabled"] for m in again["endpoint"]["models"]}
    assert on["gpt-5.5"] is True and on["gpt-5.4"] is False
    cleanup(client, ep)


def test_chat_completion_non_stream_and_stream(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    g = make_gw(client, ep)
    r = chat(client, g, messages=[{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}], max_tokens=50, temperature=0.2)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["choices"][0]["message"]["content"] == "Hello world" and d["choices"][0]["finish_reason"] == "stop"
    assert d["usage"]["prompt_tokens"] == 11 and d["usage"]["completion_tokens"] == 4 and d["usage"]["completion_tokens_details"]["reasoning_tokens"] == 1
    path, headers, body = v.calls[-1]
    assert path.endswith("/codex/responses") and headers["authorization"].startswith("Bearer ey") and headers["chatgpt-account-id"] == "acct-1"
    assert headers["originator"] == "codex_cli_rs"
    assert body["model"] == "gpt-5.5" and body["store"] is False and body["stream"] is True and body["instructions"] == "Be brief."
    assert body["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert "temperature" not in body and "max_tokens" not in body and "max_output_tokens" not in body
    # streaming
    r = chat(client, g, messages=[{"role": "user", "content": "hi"}], stream=True)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    lines = [ln for ln in r.text.splitlines() if ln.startswith("data:")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln[5:]) for ln in lines[:-1]]
    assert "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks if c["choices"]) == "Hello world"
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "stop" for c in chunks) and chunks[-1]["usage"]["completion_tokens"] == 4
    # the Anthropic and Responses front doors work on top of it
    a = chat(client, g, route="messages", max_tokens=20, messages=[{"role": "user", "content": "hi"}])
    assert a.status_code == 200 and a.json()["content"][0]["text"] == "Hello world"
    o = chat(client, g, route="responses", input="hi")
    assert o.status_code == 200 and o.json()["output"][-1]["content"][0]["text"] == "Hello world"
    cleanup(client, ep)


def test_tool_calls_round_trip(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    g = make_gw(client, ep)
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "d", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    v.backend = [(200, TOOL)]
    r = chat(client, g, messages=[{"role": "user", "content": "weather in Paris?"}], tools=tools, tool_choice="auto")
    msg = r.json()["choices"][0]
    assert msg["finish_reason"] == "tool_calls" and msg["message"]["tool_calls"][0]["id"] == "call_1"
    assert msg["message"]["tool_calls"][0]["function"] == {"name": "get_weather", "arguments": '{"city":"Paris"}'}
    sent = v.calls[-1][2]
    assert sent["tools"][0]["name"] == "get_weather" and sent["tools"][0]["parameters"]["properties"]["city"] == {"type": "string"} and sent["tool_choice"] == "auto"
    v.backend = [(200, TEXT)]
    history = [{"role": "user", "content": "weather in Paris?"},
               {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]},
               {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}]
    chat(client, g, messages=history, tools=tools)
    items = v.calls[-1][2]["input"]
    assert items[1] == {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": '{"city":"Paris"}'}
    assert items[2] == {"type": "function_call_output", "call_id": "call_1", "output": "sunny"}
    cleanup(client, ep)


def test_expired_token_is_refreshed_once_and_the_refresh_token_rotates(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    with db.session() as s:
        acc = s.get(OAuthAccount, ep["id"])
        old_refresh, acc.expires_at = acc.refresh_token, time.time() - 10
        s.add(acc); s.commit()

    async def three():
        return await asyncio.gather(*(tokens.fresh_access(ep["id"]) for _ in range(3)))
    got = asyncio.run(three())
    assert v.refreshes == 1 and len({t for t, _ in got}) == 1                  # one refresh serves all three callers
    with db.session() as s:
        acc = s.get(OAuthAccount, ep["id"])
        assert acc.refresh_token != old_refresh and acc.refresh_token.startswith("refresh-") and acc.expires_at > time.time() + 3000
    cleanup(client, ep)


def test_401_refreshes_and_retries(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    g = make_gw(client, ep)
    v.backend = [(401, {"detail": "token revoked"}), (200, TEXT)]
    r = chat(client, g, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Hello world" and v.refreshes == 1
    auths = [h["authorization"] for p, h, b in v.calls if p.endswith("/responses")]
    assert auths[-1] != auths[-2]                                              # the retry carried the new token
    cleanup(client, ep)


def test_dead_refresh_token_needs_sign_in_and_signing_in_again_keeps_the_endpoint(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    g = make_gw(client, ep)
    with db.session() as s:
        acc = s.get(OAuthAccount, ep["id"]); acc.expires_at = 0; s.add(acc); s.commit()
    v.refresh_fail = "refresh_token_reused"
    r = chat(client, g, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502
    info = client.get("/api/accounts").json()[0]
    assert info["needs_login"] is True and "refresh" in (info["last_error"] or "").lower()
    asyncio.run(health.check_endpoint(ep["id"]))
    assert next(e for e in client.get("/api/endpoints").json() if e["id"] == ep["id"])["status"] == "offline"
    # sign in again for that account: same endpoint, models and gateway
    v.refresh_fail, v.polls_before_ok = None, 0
    _, again = sign_in(client, endpoint_id=ep["id"])
    assert again["created"] is False and again["endpoint"]["id"] == ep["id"] and again["endpoint"]["account"]["needs_login"] is False
    assert chat(client, g, messages=[{"role": "user", "content": "hi"}]).status_code == 200
    assert len([a for a in client.get("/api/accounts").json()]) == 1
    cleanup(client, ep)


def test_the_same_account_signing_in_twice_does_not_add_a_copy(client, v):
    _, first = sign_in(client)
    v.polls_before_ok = 0
    _, second = sign_in(client)
    assert second["created"] is False and second["endpoint"]["id"] == first["endpoint"]["id"]
    v.next_login, v.polls_before_ok = dict(email="other@example.com", acct="acct-2"), 0
    _, other = sign_in(client)
    assert other["created"] is True and other["endpoint"]["id"] != first["endpoint"]["id"]
    cleanup(client, first["endpoint"], other["endpoint"])


def test_a_rate_limited_account_fails_over_to_the_next(client, v):
    _, a = sign_in(client)
    v.next_login, v.polls_before_ok = dict(email="b@example.com", acct="acct-b"), 0
    _, b = sign_in(client)
    ma, mb = enable(client, a["endpoint"], "gpt-5.5"), enable(client, b["endpoint"], "gpt-5.5")
    g = client.post("/api/gateways", json={"name": f"pool-{time.time_ns()}", "strategy": "priority_failover",
                                           "models": [{"public_name": "codex", "members": [{"model_id": ma, "priority": 0}, {"model_id": mb, "priority": 1}]}]}).json()
    v.backend = [(429, {"detail": "usage limit"}), (200, TEXT)]
    r = chat(client, g, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Hello world"
    accts = [h["chatgpt-account-id"] for p, h, bd in v.calls if p.endswith("/responses")][-2:]
    assert accts == ["acct-1", "acct-b"]
    cleanup(client, a["endpoint"], b["endpoint"])


def test_vendor_errors_do_not_leak_on_the_public_url_and_failed_runs_are_502(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    g = make_gw(client, ep)
    v.backend = [(500, {"detail": "internal stack trace /srv/secret.py"})]
    r = chat(client, g, admin=False, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502 and "secret.py" not in r.text
    v.backend = [(200, sse({"type": "response.output_text.delta", "delta": "x"}, {"type": "response.failed", "response": {"error": {"message": "boom /internal"}}}))]
    r = chat(client, g, admin=False, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502 and "/internal" not in r.text
    cleanup(client, ep)


def test_account_endpoints_are_protected_and_clean_up(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    assert client.patch(f"/api/endpoints/{ep['id']}", json={"base_url": "http://evil.example/v1"}).status_code == 400
    assert client.patch(f"/api/endpoints/{ep['id']}", json={"api_key": "sk-x"}).status_code == 400
    assert client.patch(f"/api/endpoints/{ep['id']}", json={"name": "renamed"}).status_code == 200
    assert client.post("/api/endpoints", json={"name": "x", "provider": "codex-oauth", "base_url": "http://evil.example"}).status_code == 400
    assert client.post("/api/accounts/codex/poll", json={"session_id": "nope"}).status_code == 404
    assert client.delete(f"/api/endpoints/{ep['id']}").status_code == 200
    with db.session() as s:
        assert s.get(OAuthAccount, ep["id"]) is None and s.get(Endpoint, ep["id"]) is None
