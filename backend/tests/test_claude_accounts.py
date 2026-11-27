"""Claude subscription accounts: PKCE sign-in with a pasted code, token refresh, chat/completions <-> Anthropic Messages translation."""
import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, delete

from app import db, proxy
from app.accounts import claude_adapter, tokens
from app.main import app
from app.models import Endpoint, OAuthAccount, RequestLog

TOKEN_HOST, API_HOST = "platform.claude.com", "api.anthropic.com"


def sse(*events) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def msg_stream(*blocks, stop="end_turn", usage=None):
    ev = [{"type": "message_start", "message": {"usage": usage or {"input_tokens": 10, "cache_read_input_tokens": 3, "output_tokens": 1}}}, {"type": "ping"}]
    for i, b in enumerate(blocks):
        ev.append({"type": "content_block_start", "index": i, "content_block": b["start"]})
        ev += [{"type": "content_block_delta", "index": i, "delta": d} for d in b["deltas"]]
        ev.append({"type": "content_block_stop", "index": i})
    ev += [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 7}}, {"type": "message_stop"}]
    return sse(*ev)


TEXT = msg_stream({"start": {"type": "text", "text": ""}, "deltas": [{"type": "text_delta", "text": "Hello "}, {"type": "text_delta", "text": "Claude"}]})
TOOL = msg_stream({"start": {"type": "thinking", "thinking": ""}, "deltas": [{"type": "thinking_delta", "thinking": "hmm"}]},
                  {"start": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}},
                   "deltas": [{"type": "input_json_delta", "partial_json": '{"city":'}, {"type": "input_json_delta", "partial_json": '"Paris"}'}]}, stop="tool_use")


class Vendor:
    def __init__(self):
        self.n = 0
        self.exchanges: list = []     # token requests (json bodies)
        self.refreshes = 0
        self.refresh_fail = None
        self.email = "claude.user@example.com"
        self.backend: list = [(200, TEXT)]
        self.calls: list = []         # (headers, json body) of /v1/messages
        self.models = None

    def issue(self):
        self.n += 1
        return {"access_token": f"sk-ant-oat01-access-{self.n}", "refresh_token": f"sk-ant-ort01-refresh-{self.n}", "expires_in": 3600,
                "account": {"uuid": "acct-uuid-1", "email_address": self.email}, "organization": {"uuid": "org-1", "name": "Max"}}

    async def handler(self, request: httpx.Request):
        path, body = request.url.path, request.content.decode() if request.content else ""
        if request.url.host == TOKEN_HOST and path.endswith("/oauth/token"):
            d = json.loads(body)
            await asyncio.sleep(0.02)
            if d["grant_type"] == "refresh_token":
                self.refreshes += 1
                if self.refresh_fail:
                    return httpx.Response(400, json={"error": self.refresh_fail})
                return httpx.Response(200, json=self.issue())
            self.exchanges.append(d)
            if d["code"] == "bad-code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self.issue())
        if request.url.host == API_HOST and path == "/v1/models":
            if self.models is None:
                return httpx.Response(403, json={"error": {"message": "nope"}})
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if request.url.host == API_HOST and path == "/v1/messages":
            self.calls.append((dict(request.headers), json.loads(body)))
            status, payload = self.backend[0] if len(self.backend) == 1 else self.backend.pop(0)
            if isinstance(payload, str):
                return httpx.Response(status, text=payload, headers={"content-type": "text/event-stream"})
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"detail": "unexpected " + str(request.url)})


@pytest.fixture
def v(monkeypatch):
    vendor = Vendor()
    monkeypatch.setattr(proxy, "client", httpx.AsyncClient(transport=httpx.MockTransport(vendor.handler)))
    monkeypatch.delenv("SG_CLAUDE_CODE_IDENTITY", raising=False)
    tokens._locks.clear()
    return vendor


@pytest.fixture(scope="module")
def client():
    db.init_db()
    yield TestClient(app)
    with Session(db.engine) as s:
        s.exec(delete(RequestLog))
        s.commit()


def sign_in(client, endpoint_id=None, code="the-code"):
    s = client.post("/api/accounts/claude/start", json={"endpoint_id": endpoint_id})
    assert s.status_code == 200, s.text
    state = parse_qs(urlparse(s.json()["authorize_url"]).query)["state"][0]
    r = client.post("/api/accounts/claude/finish", json={"session_id": s.json()["session_id"], "code": f"{code}#{state}"})
    assert r.status_code == 200, r.text
    return s.json(), r.json()


def make_gw(client, ep, model="claude-sonnet-5-5"):
    mid = next(m["id"] for m in ep["models"] if m["upstream_id"] == model)
    assert client.patch(f"/api/models/{mid}", json={"enabled": True}).status_code == 200
    g = client.post("/api/gateways", json={"name": f"cl-{time.time_ns()}", "models": [{"public_name": "claude", "members": [{"model_id": mid}]}]})
    assert g.status_code == 200, g.text
    return g.json()


def chat(client, g, route="chat/completions", admin=True, **body):
    base = f"/api/admin-gw/{g['slug']}/v1" if admin else f"/gw/{g['slug']}/v1"
    return client.post(f"{base}/{route}", json={"model": "claude", **body})


def cleanup(client, *eps):
    for ep in eps:
        client.delete(f"/api/endpoints/{ep['id']}")


def test_pkce_sign_in_creates_an_account(client, v):
    start, done = sign_in(client)
    q = parse_qs(urlparse(start["authorize_url"]).query)
    assert urlparse(start["authorize_url"]).netloc == "claude.ai"
    assert q["response_type"] == ["code"] and q["code_challenge_method"] == ["S256"] and q["client_id"][0] and q["redirect_uri"][0].endswith("/oauth/code/callback")
    ex = v.exchanges[-1]                       # the verifier sent at exchange is the one behind the challenge in the link
    assert base64.urlsafe_b64encode(hashlib.sha256(ex["code_verifier"].encode()).digest()).rstrip(b"=").decode() == q["code_challenge"][0]
    assert ex["grant_type"] == "authorization_code" and ex["code"] == "the-code" and ex["state"] == q["state"][0]
    ep = done["endpoint"]
    assert done["created"] and ep["provider"] == "claude-oauth" and ep["account"]["email"] == "claude.user@example.com" and ep["account"]["kind"] == "claude"
    assert {m["upstream_id"] for m in ep["models"]} >= {"claude-opus-5-5", "claude-sonnet-5-5"} and not any(m["enabled"] for m in ep["models"])   # new models stay off until enabled, as on any endpoint
    with db.session() as s:
        acc = s.get(OAuthAccount, ep["id"])
        access, refresh = acc.access_token, acc.refresh_token
        raw = s.exec(text("select access_token, refresh_token from oauthaccount where endpoint_id = :i").bindparams(i=ep["id"])).one()
    assert raw[0] != access and "ort01" not in raw[1]                        # encrypted at rest
    for url in ("/api/endpoints", "/api/accounts"):
        body = client.get(url).text
        assert "sk-ant-" not in body and access not in body and refresh not in body
    assert client.get("/api/accounts").json()[0]["provider"] == "claude-oauth"
    cleanup(client, ep)


def test_models_stay_off_until_enabled_and_the_choice_survives_rescan_and_sign_in(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    mid = next(m["id"] for m in ep["models"] if m["upstream_id"] == "claude-sonnet-5-5")
    g = client.post("/api/gateways", json={"name": f"off-{time.time_ns()}", "models": [{"public_name": "claude", "members": [{"model_id": mid}]}]}).json()
    assert chat(client, g, messages=[{"role": "user", "content": "hi"}]).status_code in (400, 404, 502)   # a disabled model is not served
    assert client.patch(f"/api/models/{mid}", json={"enabled": True}).status_code == 200
    assert chat(client, g, messages=[{"role": "user", "content": "hi"}]).status_code == 200
    on = {m["upstream_id"]: m["enabled"] for m in client.post(f"/api/endpoints/{ep['id']}/rescan").json()["models"]}
    assert on["claude-sonnet-5-5"] is True and on["claude-opus-5-5"] is False
    _, again = sign_in(client, endpoint_id=ep["id"])
    on = {m["upstream_id"]: m["enabled"] for m in again["endpoint"]["models"]}
    assert on["claude-sonnet-5-5"] is True and on["claude-opus-5-5"] is False
    cleanup(client, ep)


def test_wrong_state_or_code_is_refused_and_the_sign_in_stays_open(client, v):
    s = client.post("/api/accounts/claude/start", json={}).json()
    state = parse_qs(urlparse(s["authorize_url"]).query)["state"][0]
    fin = lambda code: client.post("/api/accounts/claude/finish", json={"session_id": s["session_id"], "code": code})  # noqa: E731
    assert fin("x#another-state").status_code == 400 and not v.exchanges        # a code from some other sign-in never reaches Anthropic
    assert fin("").status_code == 400
    assert fin(f"bad-code#{state}").status_code == 400
    ok = fin(f"good-code#{state}")                                              # the same session can still be completed
    assert ok.status_code == 200
    assert client.post("/api/accounts/claude/finish", json={"session_id": s["session_id"], "code": "x"}).status_code == 404   # one use only
    assert client.post("/api/accounts/claude/finish", json={"session_id": "nope", "code": "x"}).status_code == 404
    cleanup(client, ok.json()["endpoint"])


def test_chat_stream_non_stream_and_other_front_doors(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    g = make_gw(client, ep)
    r = chat(client, g, messages=[{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}], temperature=0.3, stop="END")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["choices"][0]["message"]["content"] == "Hello Claude" and d["choices"][0]["finish_reason"] == "stop"
    assert d["usage"] == {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20, "prompt_tokens_details": {"cached_tokens": 3}}
    headers, body = v.calls[-1]
    assert headers["authorization"].startswith("Bearer sk-ant-oat01-access-") and headers["anthropic-version"] == "2023-06-01" and "oauth-2025-04-20" in headers["anthropic-beta"]
    assert body["model"] == "claude-sonnet-5-5" and body["stream"] is True and body["max_tokens"] == claude_adapter.DEFAULT_MAX_TOKENS
    assert body["system"] == [{"type": "text", "text": "Be brief."}] and body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert body["temperature"] == 0.3 and body["stop_sequences"] == ["END"]
    r = chat(client, g, messages=[{"role": "user", "content": "hi"}], stream=True, max_tokens=33)
    lines = [ln for ln in r.text.splitlines() if ln.startswith("data:")]
    assert lines[-1] == "data: [DONE]" and v.calls[-1][1]["max_tokens"] == 33
    chunks = [json.loads(ln[5:]) for ln in lines[:-1]]
    assert "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks if c["choices"]) == "Hello Claude" and chunks[-1]["usage"]["completion_tokens"] == 7
    a = chat(client, g, route="messages", max_tokens=20, messages=[{"role": "user", "content": "hi"}])
    assert a.status_code == 200 and a.json()["content"][0]["text"] == "Hello Claude"
    o = chat(client, g, route="responses", input="hi")
    assert o.status_code == 200 and o.json()["output"][-1]["content"][0]["text"] == "Hello Claude"
    cleanup(client, ep)


def test_claude_code_identity_is_off_unless_switched_on(client, v, monkeypatch):
    _, done = sign_in(client)
    ep = done["endpoint"]
    g = make_gw(client, ep)
    chat(client, g, messages=[{"role": "user", "content": "hi"}])
    assert "system" not in v.calls[-1][1]                                      # nothing is presented as Claude Code by default
    monkeypatch.setenv("SG_CLAUDE_CODE_IDENTITY", "1")
    chat(client, g, messages=[{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}])
    assert v.calls[-1][1]["system"][0]["text"] == claude_adapter.IDENTITY_TEXT and v.calls[-1][1]["system"][1]["text"] == "Be brief."
    cleanup(client, ep)


def test_tools_images_and_thinking(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    g = make_gw(client, ep)
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "d", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    v.backend = [(200, TOOL)]
    r = chat(client, g, messages=[{"role": "user", "content": "weather in Paris?"}], tools=tools, tool_choice="required")
    ch = r.json()["choices"][0]
    assert ch["finish_reason"] == "tool_calls" and ch["message"]["reasoning_content"] == "hmm"
    assert ch["message"]["tool_calls"][0] == {"id": "toolu_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}
    sent = v.calls[-1][1]
    assert sent["tools"][0]["name"] == "get_weather" and sent["tools"][0]["input_schema"]["properties"]["city"] == {"type": "string"} and sent["tool_choice"] == {"type": "any"}
    v.backend = [(200, TEXT)]
    history = [{"role": "user", "content": "weather in Paris and Rome?"},
               {"role": "assistant", "content": "checking", "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}},
                                                                         {"id": "toolu_2", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Rome"}'}}]},
               {"role": "tool", "tool_call_id": "toolu_1", "content": "sunny"}, {"role": "tool", "tool_call_id": "toolu_2", "content": "rainy"},
               {"role": "user", "content": [{"type": "text", "text": "and this?"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    chat(client, g, messages=history, tools=tools, reasoning_effort="low", temperature=0.9)
    sent = v.calls[-1][1]
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["user", "assistant", "user"]                                # roles alternate: both results and the next question share one user turn
    assert [b["type"] for b in sent["messages"][1]["content"]] == ["text", "tool_use", "tool_use"] and sent["messages"][1]["content"][1]["input"] == {"city": "Paris"}
    last = sent["messages"][2]["content"]
    assert [b["type"] for b in last] == ["tool_result", "tool_result", "text", "image"] and last[0]["tool_use_id"] == "toolu_1" and last[1]["content"] == "rainy"
    assert last[3]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": 2048} and sent["max_tokens"] > 2048 and "temperature" not in sent
    cleanup(client, ep)


def test_refresh_rotation_retry_and_failure(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    g = make_gw(client, ep)
    with db.session() as s:
        acc = s.get(OAuthAccount, ep["id"]); old = acc.refresh_token; acc.expires_at = time.time() - 5; s.add(acc); s.commit()

    async def three():
        return await asyncio.gather(*(tokens.fresh_access(ep["id"]) for _ in range(3)))
    asyncio.run(three())
    assert v.refreshes == 1
    with db.session() as s:
        assert s.get(OAuthAccount, ep["id"]).refresh_token != old
    v.backend = [(401, {"error": {"message": "revoked"}}), (200, TEXT)]
    assert chat(client, g, messages=[{"role": "user", "content": "hi"}]).status_code == 200 and v.refreshes == 2
    with db.session() as s:
        acc = s.get(OAuthAccount, ep["id"]); acc.expires_at = 0; s.add(acc); s.commit()
    v.refresh_fail, v.backend = "invalid_grant", [(200, TEXT)]
    assert chat(client, g, messages=[{"role": "user", "content": "hi"}]).status_code == 502
    assert client.get("/api/accounts").json()[0]["needs_login"] is True
    v.refresh_fail = None
    _, again = sign_in(client, endpoint_id=ep["id"])                             # sign in again: same endpoint, models and gateway
    assert again["created"] is False and again["endpoint"]["id"] == ep["id"] and chat(client, g, messages=[{"role": "user", "content": "hi"}]).status_code == 200
    cleanup(client, ep)


def test_vendor_errors_are_clean(client, v):
    _, done = sign_in(client)
    ep = done["endpoint"]
    g = make_gw(client, ep)
    v.backend = [(500, {"error": {"message": "stack trace /srv/secret.py"}})]
    r = chat(client, g, admin=False, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502 and "secret.py" not in r.text
    v.backend = [(200, sse({"type": "message_start", "message": {}}, {"type": "error", "error": {"type": "overloaded_error", "message": "busy /internal"}}))]
    r = chat(client, g, admin=False, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502 and "/internal" not in r.text
    v.backend = [(400, {"error": {"type": "invalid_request_error", "message": "max_tokens too large"}})]
    r = chat(client, g, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 400 and "max_tokens too large" in r.text
    cleanup(client, ep)


def test_both_kinds_of_account_live_side_by_side(client, v):
    _, c = sign_in(client)
    cleanup(client, c["endpoint"])
    assert client.patch(f"/api/endpoints/{c['endpoint']['id']}", json={"base_url": "http://x"}).status_code == 404   # gone
    _, c2 = sign_in(client)
    assert client.patch(f"/api/endpoints/{c2['endpoint']['id']}", json={"base_url": "http://evil.example"}).status_code == 400
    assert client.post("/api/endpoints", json={"name": "x", "provider": "claude-oauth", "base_url": "http://evil.example"}).status_code == 400
    assert client.post("/api/accounts/codex/poll", json={"session_id": "nope"}).status_code == 404
    client.delete(f"/api/endpoints/{c2['endpoint']['id']}")
    with db.session() as s:
        assert s.get(OAuthAccount, c2["endpoint"]["id"]) is None and s.get(Endpoint, c2["endpoint"]["id"]) is None


def test_models_come_from_the_account_when_the_api_lists_them(client, v):
    v.models = ["claude-sonnet-5-5", "claude-fable-5-2"]
    _, done = sign_in(client)
    assert {m["upstream_id"] for m in done["endpoint"]["models"]} == {"claude-sonnet-5-5", "claude-fable-5-2"}
    cleanup(client, done["endpoint"])
