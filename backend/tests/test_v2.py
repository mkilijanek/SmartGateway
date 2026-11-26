import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from app import crypto
from app.compat import anthropic as anth
from app.compat import responses as resp_api
from app.proxy import detect_requirements, estimate_tokens, required_tier
from app.router import affinity, cache
from app.router.limits import LimitError, Limiter
from app.router.strategies import Candidate, Stats, pick


def cand(i, limit=2, **kw):
    return Candidate(member_id=i, model_id=i, endpoint_id=i, endpoint_name=f"e{i}", base_url="http://x",
                     api_key=None, upstream_id=f"m{i}", caps={"chat"}, limit=limit, **kw)


# ---- routing
def test_external_busy_blocks_and_biases():
    st = Stats()
    a, b = cand(1, limit=2), cand(2, limit=2)
    st.external_busy[1] = 2
    assert pick("least_busy", [a, b], st) is b
    st.external_busy[1] = 1
    st.acquire(b)
    assert pick("least_busy", [a, b], st) is a or pick("least_busy", [a, b], st) is b
    st.external_busy[2] = 2
    st.external_busy[1] = 2
    assert pick("least_busy", [a, b], st) is None


def test_sticky_prefers_remembered_member():
    st = Stats()
    a, b = cand(1), cand(2)
    for _ in range(3):
        assert pick("least_busy", [a, b], st, {"prefer_member": 2}) is b
    st.acquire(b); st.acquire(b)  # b full -> falls through
    assert pick("least_busy", [a, b], st, {"prefer_member": 2}) is a


def test_tiered_never_downgrades_and_escalates():
    st = Stats()
    small, mid, big = cand(1, tier=1), cand(2, tier=2), cand(3, tier=3)
    allc = [small, mid, big]
    assert pick("tiered", allc, st, {"tier": 1}) is small
    assert pick("tiered", allc, st, {"tier": 2}) is mid
    assert pick("tiered", allc, st, {"tier": 3}) is big
    st.acquire(big); st.acquire(big)
    assert pick("tiered", allc, st, {"tier": 3}) is None  # wait, don't downgrade
    st.acquire(small); st.acquire(small)
    assert pick("tiered", allc, st, {"tier": 1}) is mid  # escalate when small is busy
    assert pick("tiered", [small, mid], st, {"tier": 3}) is mid  # no tier-3 exists -> best available


def test_estimates_and_tiers():
    body = {"messages": [{"role": "user", "content": "x" * 3500}], "max_tokens": 100}
    pt, out = estimate_tokens("chat/completions", body)
    assert 900 <= pt <= 1100 and out == 100
    img = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}]}
    assert estimate_tokens("chat/completions", img)[0] > 800
    assert required_tier(100, {}, set()) == 1
    assert required_tier(2000, {}, set()) == 2
    assert required_tier(9000, {"tools": [1]}, {"reasoning"}) == 3


def test_tools_is_soft_requirement():
    hard, soft = detect_requirements("chat/completions", {"messages": [], "tools": [{"type": "function"}]})
    assert hard == {"chat"} and soft == {"tools"}
    assert detect_requirements("audio/speech", {})[0] == {"tts"}
    assert detect_requirements("images/generations", {})[0] == {"image_gen"}


def test_affinity_keys():
    k1 = affinity.session_key("", {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "hello"}]})
    k2 = affinity.session_key("", {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "hello"}, {"role": "assistant", "content": "x"}]})
    assert k1 == k2 and affinity.session_key("abc", {}) == "h:abc"
    affinity.put(7, "k", 3)
    assert affinity.get(7, "k") == 3 and affinity.get(7, "zz") is None


# ---- limits
def test_limiter_rpm_tpm_concurrency():
    lim = Limiter()
    key = SimpleNamespace(id=1, rpm_limit=2, tpm_limit=None, max_concurrent=None, daily_token_quota=None, monthly_token_quota=None)
    t1 = lim.admit(key, 10); t2 = lim.admit(key, 10)
    with pytest.raises(LimitError) as e:
        lim.admit(key, 10)
    assert e.value.status == 429 and e.value.retry_after >= 1
    key2 = SimpleNamespace(id=2, rpm_limit=None, tpm_limit=100, max_concurrent=1, daily_token_quota=None, monthly_token_quota=None)
    t = lim.admit(key2, 60)
    with pytest.raises(LimitError):
        lim.admit(key2, 10)  # concurrency
    t.release(tokens=95)
    with pytest.raises(LimitError):
        lim.admit(key2, 10)  # 95 + 10 > 100 tpm
    assert lim.admit(None).key_id is None


# ---- crypto + migration
def test_crypto_roundtrip_and_legacy_plaintext():
    enc = crypto.encrypt("sk-secret")
    assert enc.startswith("enc:") and "sk-secret" not in enc
    assert crypto.decrypt(enc) == "sk-secret" and crypto.decrypt("plain") == "plain"
    t = crypto.EncryptedString()
    assert t.process_bind_param("abc", None).startswith("enc:") and t.process_bind_param(None, None) is None
    assert t.process_result_value(t.process_bind_param("abc", None), None) == "abc"
    assert crypto.is_unreadable(t.process_result_value("enc:not-a-valid-token", None))  # unreadable secret must not crash the app, nor read as "no secret"


def test_schema_migration_and_legacy_key(tmp_path, monkeypatch):
    # build a v1-shaped database, then run init_db against it
    path = tmp_path / "v1.db"
    c = sqlite3.connect(path)
    c.executescript("""
    create table endpoint (id integer primary key, name text, base_url text, api_key text, status text, last_error text, last_checked real, created_at real);
    create table gateway (id integer primary key, slug text, name text, api_key text, strategy text, queue_timeout real, max_retries integer, created_at real);
    create table gatewaymodel (id integer primary key, gateway_id integer, public_name text, strategy text);
    create table member (id integer primary key, gateway_model_id integer, model_id integer, weight integer, priority integer, slots_override integer, enabled integer);
    insert into endpoint values (1,'olares','http://x/v1','sk-plain','online',null,null,0);
    insert into gateway values (1,'main','Main','gw-secret','least_busy',60,2,0);
    insert into gatewaymodel values (1,1,'smart',null);
    insert into member values (1,1,1,1,0,null,1);
    """)
    c.commit(); c.close()
    from sqlalchemy import create_engine
    from app import db
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    db.init_db()
    c = sqlite3.connect(path)
    assert [r[0] for r in c.execute("select api_key from endpoint")][0].startswith("enc:")
    assert [r[1] for r in c.execute("pragma table_info(member)")].count("is_fallback") == 1
    assert c.execute("select api_key from gateway").fetchone()[0] is None
    h = c.execute("select key_hash from apikey").fetchone()[0]
    assert h == crypto.hash_key("gw-secret")
    db.init_db()  # idempotent
    assert c.execute("select count(*) from apikey").fetchone()[0] == 1


# ---- compat layers
def test_anthropic_request_translation():
    body = {"model": "m", "max_tokens": 50, "system": "be brief", "stop_sequences": ["X"],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}}]},
                         {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}],
            "tools": [{"name": "f", "description": "d", "input_schema": {"type": "object"}}]}
    c = anth.to_chat(body)
    assert c["messages"][0] == {"role": "system", "content": "be brief"}
    assert c["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,AAA")
    assert c["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    assert c["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "ok"}
    assert c["tools"][0]["function"]["name"] == "f" and c["stop"] == ["X"] and c["max_tokens"] == 50


def test_anthropic_response_translation():
    r = anth.from_chat({"choices": [{"message": {"content": "hello", "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": '{"x":2}'}}]}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 4}}, "pub")
    assert r["model"] == "pub" and r["stop_reason"] == "tool_use" and r["usage"] == {"input_tokens": 3, "output_tokens": 4}
    assert r["content"][0] == {"type": "text", "text": "hello"} and r["content"][1]["input"] == {"x": 2}


async def _lines(items):
    for i in items:
        yield i


async def test_anthropic_stream_translation():
    chunks = [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}',
        "data: [DONE]",
    ]
    out = [e async for e in anth.stream_from_chat(_lines(chunks), "pub")]
    names = [e.split("\n")[0] for e in out]
    assert names == ["event: message_start", "event: content_block_start", "event: content_block_delta", "event: content_block_delta",
                     "event: content_block_stop", "event: message_delta", "event: message_stop"]
    md = json.loads(out[5].split("data: ")[1])
    assert md["delta"]["stop_reason"] == "end_turn" and md["usage"]["output_tokens"] == 2


def test_responses_translation():
    c = resp_api.to_chat({"model": "m", "instructions": "sys", "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                          "max_output_tokens": 20, "reasoning": {"effort": "high"}})
    assert c["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert c["max_tokens"] == 20 and c["reasoning_effort"] == "high"
    assert resp_api.to_chat({"model": "m", "input": "yo"})["messages"] == [{"role": "user", "content": "yo"}]
    r = resp_api.from_chat({"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}, "pub", {})
    assert r["output"][0]["content"][0]["text"] == "ok" and r["usage"]["total_tokens"] == 3


async def test_responses_stream():
    chunks = ['data: {"choices":[{"delta":{"content":"A"}}]}', 'data: {"choices":[{"delta":{"content":"B"}}]}',
              'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}', "data: [DONE]"]
    out = [e async for e in resp_api.stream_from_chat(_lines(chunks), "pub")]
    types = [json.loads(e.split("data: ")[1])["type"] for e in out]
    assert types[0] == "response.created" and types[-1] == "response.completed"
    assert types.count("response.output_text.delta") == 2
    assert json.loads(out[-1].split("data: ")[1])["response"]["output"][0]["content"][0]["text"] == "AB"


def test_cache_key_ignores_stream_and_user():
    a = cache.make_key(1, "chat/completions", {"model": "m", "messages": [1], "temperature": 0, "user": "u1"})
    b = cache.make_key(1, "chat/completions", {"temperature": 0, "messages": [1], "model": "m", "stream": False})
    assert a == b and a != cache.make_key(2, "chat/completions", {"model": "m", "messages": [1], "temperature": 0})
