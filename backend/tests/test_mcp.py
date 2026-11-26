import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import db, retention
from app.main import app
from app.mcp import client as mcp_client, registry, server as mcp_server
from app.mcp.client import McpError, McpSession
from app.models import McpCallLog


class FakeServer:
    """A tiny MCP server behind httpx.MockTransport. `sse` answers with text/event-stream like many real servers do."""

    def __init__(self, tools, sse=False, session=True, page=0):
        self.tools, self.sse, self.session, self.page = tools, sse, session, page
        self.calls: list = []
        self.requests: list = []
        self.expire_next = False

    def reply(self, id_, result=None, error=None):
        body = {"jsonrpc": "2.0", "id": id_, **({"error": error} if error else {"result": result})}
        if self.sse:
            text = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {}})}\n\nevent: message\ndata: {json.dumps(body)}\n\n"
            return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream", **self._sid()})
        return httpx.Response(200, json=body, headers=self._sid())

    def _sid(self):
        return {"mcp-session-id": "sess-1"} if self.session else {}

    def __call__(self, request: httpx.Request):
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        self.requests.append((msg.get("method"), dict(request.headers)))
        if self.expire_next and msg.get("method") != "initialize":
            self.expire_next = False
            return httpx.Response(404, text="session gone")
        m, id_, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if id_ is None:
            return httpx.Response(202)
        if m == "initialize":
            return self.reply(id_, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "resources": {}, "prompts": {}}, "serverInfo": {"name": "fake", "version": "1"}})
        if m == "ping":
            return self.reply(id_, {})
        if m == "tools/list":
            names = list(self.tools)
            if self.page:
                start = int(p.get("cursor") or 0)
                chunk = names[start:start + self.page]
                nxt = str(start + self.page) if start + self.page < len(names) else None
                return self.reply(id_, {"tools": [self.tools[n] for n in chunk], **({"nextCursor": nxt} if nxt else {})})
            return self.reply(id_, {"tools": list(self.tools.values())})
        if m == "tools/call":
            self.calls.append((p["name"], p.get("arguments")))
            if p["name"] == "boom":
                return self.reply(id_, {"content": [{"type": "text", "text": "it broke"}], "isError": True})
            return self.reply(id_, {"content": [{"type": "text", "text": f"{p['name']}:{json.dumps(p.get('arguments'), sort_keys=True)}"}]})
        if m == "resources/list":
            return self.reply(id_, {"resources": [{"uri": "file:///notes.txt", "name": "notes"}]})
        if m == "resources/templates/list":
            return self.reply(id_, {"resourceTemplates": [{"uriTemplate": "db://rows/{id}", "name": "row"}]})
        if m == "resources/read":
            return self.reply(id_, {"contents": [{"uri": p["uri"], "text": f"read {p['uri']}"}]})
        if m == "prompts/list":
            return self.reply(id_, {"prompts": [{"name": "greet", "description": "say hi"}]})
        if m == "prompts/get":
            return self.reply(id_, {"messages": [{"role": "user", "content": {"type": "text", "text": f"hi {p.get('arguments')}"}}]})
        return self.reply(id_, error={"code": -32601, "message": "nope"})


def tool(name, desc="d"):
    return {"name": name, "description": desc, "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}


@pytest.fixture
def servers(monkeypatch):
    fakes = {"alpha.test": FakeServer({"search": tool("search"), "boom": tool("boom")}),
             "beta.test": FakeServer({"search": tool("search"), "fetch": tool("fetch")}, sse=True, session=False, page=1)}

    def handler(request: httpx.Request):
        return fakes[request.url.host](request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(mcp_client, "get_http", lambda: http)
    registry._sessions.clear()
    return fakes


@pytest.fixture(scope="module")
def client():
    db.init_db()
    yield TestClient(app)


def uniq(p):
    return f"{p}-{time.time_ns()}"


def add_ep(client, host, name=None, **kw):
    r = client.post("/api/mcp-endpoints", json={"name": name or uniq(host), "url": f"http://{host}/mcp", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def rpc(client, path, method, params=None, id_=1, headers=None):
    return client.post(path, json={"jsonrpc": "2.0", "id": id_, "method": method, **({"params": params} if params is not None else {})}, headers=headers or {})


# ------------------------------------------------------------------ client
async def test_client_json_session_pagination_and_reinit(servers):
    s = McpSession("http://alpha.test/mcp", {"Authorization": "Bearer x"})
    assert [t["name"] for t in await s.list_all("tools/list", "tools")] == ["search", "boom"]
    assert s.server_info["name"] == "fake" and s.session_id == "sess-1" and s.mode == "streamable"
    methods = [m for m, _ in servers["alpha.test"].requests]
    assert methods[:3] == ["initialize", "notifications/initialized", "tools/list"]
    assert servers["alpha.test"].requests[2][1]["mcp-session-id"] == "sess-1" and servers["alpha.test"].requests[0][1]["authorization"] == "Bearer x"
    servers["alpha.test"].expire_next = True            # the server forgot the session: initialise again and retry once
    assert (await s.call_tool("search", {"q": "a"}))["content"][0]["text"].startswith("search:")
    assert [m for m, _ in servers["alpha.test"].requests].count("initialize") == 2


async def test_client_sse_replies_and_cursor(servers):
    s = McpSession("http://beta.test/mcp", {})
    assert [t["name"] for t in await s.list_all("tools/list", "tools")] == ["search", "fetch"]    # two pages, SSE bodies with a notification first
    assert s.session_id is None
    with pytest.raises(McpError) as e:
        await s.request("nonsense")
    assert e.value.code == -32601


async def test_client_http_error_is_reported(monkeypatch):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="kaput")))
    monkeypatch.setattr(mcp_client, "get_http", lambda: http)
    with pytest.raises(McpError) as e:
        await McpSession("http://x.test/mcp", {}, "streamable_http").ensure()
    assert "500" in e.value.message and e.value.data == {"status": 500}


# ------------------------------------------------------------------ endpoints API
def test_endpoint_crud_headers_hidden_and_discovery(client, servers):
    ep = add_ep(client, "alpha.test", headers={"Authorization": "Bearer secret-token"})
    assert ep["status"] == "online" and ep["counts"] == {"tool": 2, "resource": 1, "template": 1, "prompt": 1}
    assert ep["header_names"] == ["Authorization"] and "secret-token" not in json.dumps(ep) and "headers_enc" not in ep
    assert "secret-token" not in json.dumps(client.get("/api/mcp-endpoints").json())
    items = client.get(f"/api/mcp-endpoints/{ep['id']}/items").json()
    assert {(i["kind"], i["name"]) for i in items} >= {("tool", "search"), ("resource", "file:///notes.txt"), ("template", "db://rows/{id}"), ("prompt", "greet")}
    # editing: an empty header value keeps the saved secret
    up = client.patch(f"/api/mcp-endpoints/{ep['id']}", json={"name": "renamed", "headers": {"Authorization": "", "X-Extra": "1"}}).json()
    assert up["name"] == "renamed" and up["header_names"] == ["Authorization", "X-Extra"]
    assert servers["alpha.test"].requests[-1][1]["authorization"] == "Bearer secret-token"
    assert client.post("/api/mcp-endpoints", json={"name": "bad", "url": "ftp://x"}).status_code == 400
    assert client.post("/api/mcp-endpoints/test", json={"name": "t", "url": "http://alpha.test/mcp"}).json()["counts"]["tool"] == 2
    assert client.delete(f"/api/mcp-endpoints/{ep['id']}").status_code == 200


def test_unreachable_endpoint_is_not_saved(client, monkeypatch):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down")))
    monkeypatch.setattr(mcp_client, "get_http", lambda: http)
    n = len(client.get("/api/mcp-endpoints").json())
    r = client.post("/api/mcp-endpoints", json={"name": "dead", "url": "http://dead.test/mcp"})
    assert r.status_code == 400 and "Cannot connect" in r.json()["detail"]
    assert len(client.get("/api/mcp-endpoints").json()) == n


# ------------------------------------------------------------------ gateway: namespacing, routing, selection
def make_gateway(client, servers_eps, items=None, **kw):
    body = {"name": uniq("mcpgw"), "members": [{"endpoint_id": e["id"], "prefix": p, "enabled_items": items.get(p, []) if items else []} for p, e in servers_eps], **kw}
    r = client.post("/api/mcp-gateways", json=body)
    assert r.status_code == 200, r.text
    return r.json()


ALL_A = ["tool:search", "tool:boom", "resource:file:///notes.txt", "template:db://rows/{id}", "prompt:greet"]
ALL_B = ["tool:search", "tool:fetch"]


def test_gateway_namespacing_routing_and_visibility(client, servers):
    a, b = add_ep(client, "alpha.test"), add_ep(client, "beta.test")
    gw = make_gateway(client, [("a", a), ("b", b)], {"a": ALL_A, "b": ["tool:search"]})
    path = f"/mcp/{gw['slug']}"
    init = rpc(client, path, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"] and init["serverInfo"]["name"] == gw["name"]
    names = [t["name"] for t in rpc(client, path, "tools/list").json()["result"]["tools"]]
    assert names == ["a__search", "a__boom", "b__search"]                       # beta's fetch was not ticked: hidden
    assert rpc(client, path, "tools/call", {"name": "b__fetch", "arguments": {}}).json()["error"]["code"] == -32602   # and not callable
    r = rpc(client, path, "tools/call", {"name": "b__search", "arguments": {"q": "x"}}).json()["result"]
    assert r["content"][0]["text"] == 'search:{"q": "x"}' and servers["beta.test"].calls == [("search", {"q": "x"})] and servers["alpha.test"].calls == []
    boom = rpc(client, path, "tools/call", {"name": "a__boom"}).json()["result"]
    assert boom["isError"] is True and boom["content"][0]["text"] == "it broke"     # upstream isError passes through
    res = rpc(client, path, "resources/list").json()["result"]["resources"]
    assert res[0]["uri"] == "mcp://a/file:///notes.txt"
    assert rpc(client, path, "resources/templates/list").json()["result"]["resourceTemplates"][0]["uriTemplate"] == "mcp://a/db://rows/{id}"
    read = rpc(client, path, "resources/read", {"uri": "mcp://a/file:///notes.txt"}).json()["result"]["contents"][0]
    assert read["uri"] == "mcp://a/file:///notes.txt" and read["text"] == "read file:///notes.txt"
    tpl = rpc(client, path, "resources/read", {"uri": "mcp://a/db://rows/42"}).json()["result"]["contents"][0]       # matches the template
    assert tpl["text"] == "read db://rows/42"
    assert rpc(client, path, "resources/read", {"uri": "mcp://a/secret://x"}).json()["error"]["code"] == -32002
    assert rpc(client, path, "prompts/list").json()["result"]["prompts"][0]["name"] == "a__greet"
    assert "hi" in json.dumps(rpc(client, path, "prompts/get", {"name": "a__greet", "arguments": {"n": "1"}}).json()["result"])
    # notifications, batches, unknown methods, GET
    assert client.post(path, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    batch = client.post(path, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]).json()
    assert [x["id"] for x in batch] == [1, 2]
    assert rpc(client, path, "nope").json()["error"]["code"] == -32601
    assert client.get(path).status_code == 405 and client.post("/mcp/does-not-exist", json={}).status_code == 404
    # a tool that appears upstream later is not exposed until ticked
    servers["alpha.test"].tools["extra"] = tool("extra")
    assert client.post(f"/api/mcp-endpoints/{a['id']}/rescan").json()["counts"]["tool"] == 3
    assert "a__extra" not in [t["name"] for t in rpc(client, path, "tools/list").json()["result"]["tools"]]
    cat = client.get(f"/api/mcp-gateways/{gw['id']}/catalog").json()
    assert [t["name"] for t in cat["tools"]] == ["a__search", "a__boom", "b__search"]
    del servers["alpha.test"].tools["extra"]


def test_upstream_down_gives_error_result(client, servers, monkeypatch):
    a = add_ep(client, "alpha.test")
    gw = make_gateway(client, [("a", a)], {"a": ["tool:search"]})
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway")))
    monkeypatch.setattr(mcp_client, "get_http", lambda: http)
    registry._sessions.clear()
    out = rpc(client, f"/mcp/{gw['slug']}", "tools/call", {"name": "a__search"}).json()["result"]
    assert out["isError"] is True and "502" in out["content"][0]["text"]


def test_gateway_validation_and_endpoint_in_use(client, servers):
    a = add_ep(client, "alpha.test")
    bad = {"name": "x", "members": [{"endpoint_id": a["id"], "prefix": "p"}, {"endpoint_id": add_ep(client, "beta.test")["id"], "prefix": "P"}]}
    assert client.post("/api/mcp-gateways", json=bad).status_code == 400                           # prefixes clash (case-insensitive after cleaning)
    assert client.post("/api/mcp-gateways", json={"name": "x", "members": [{"endpoint_id": a["id"]}, {"endpoint_id": a["id"]}]}).status_code == 400
    assert client.post("/api/mcp-gateways", json={"name": "x", "members": [{"endpoint_id": 999999}]}).status_code == 400
    assert client.post("/api/mcp-gateways", json={"name": "x", "members": [{"endpoint_id": a["id"], "enabled_items": ["bogus"]}]}).status_code == 400
    gw = make_gateway(client, [("a", a)], {"a": ["tool:search"]})
    r = client.delete(f"/api/mcp-endpoints/{a['id']}")
    assert r.status_code == 409 and gw["name"] in r.json()["detail"]["message"]
    assert client.delete(f"/api/mcp-endpoints/{a['id']}?force=true").status_code == 200
    assert client.get(f"/api/mcp-gateways/{gw['id']}").json()["members"] == []


# ------------------------------------------------------------------ auth, limits, logging
def test_keys_allowed_tools_rpm_and_logs(client, servers):
    a = add_ep(client, "alpha.test")
    gw = make_gateway(client, [("a", a)], {"a": ["tool:search", "tool:boom"]})
    path = f"/mcp/{gw['slug']}"
    assert rpc(client, path, "tools/list").status_code == 200                                      # no keys: open
    key = client.post(f"/api/mcp-gateways/{gw['id']}/keys", json={"name": "k", "allowed_tools": ["a__search"], "rpm_limit": 5}).json()
    assert key["key"].startswith("sgm-") and "key_hash" not in key
    assert "key" not in client.get(f"/api/mcp-gateways/{gw['id']}/keys").json()[0]
    bad = rpc(client, path, "tools/list")
    assert bad.status_code == 401 and "WWW-Authenticate" in bad.headers
    assert rpc(client, path, "tools/list", headers={"Authorization": "Bearer nope"}).status_code == 401
    bearer, xkey = {"Authorization": f"Bearer {key['key']}"}, {"x-api-key": key["key"]}
    assert [t["name"] for t in rpc(client, path, "tools/list", headers=bearer).json()["result"]["tools"]] == ["a__search"]
    assert rpc(client, path, "tools/call", {"name": "a__boom"}, headers=xkey).json()["error"]["code"] == -32602
    ok = rpc(client, path, "tools/call", {"name": "a__search", "arguments": {"secret": "SHOULD-NOT-BE-LOGGED"}}, headers=xkey)
    assert ok.json()["result"]["content"][0]["text"].startswith("search:")
    limited = [rpc(client, path, "ping", headers=bearer).status_code for _ in range(2)]            # ping is exempt from the limit; calls are not
    assert limited == [200, 200]
    for _ in range(5):
        r = rpc(client, path, "tools/list", headers=bearer).json()
    assert r["error"]["message"].startswith("Rate limit")
    client.patch(f"/api/mcp-keys/{key['id']}", json={"enabled": False})
    assert rpc(client, path, "tools/list", headers=bearer).status_code == 401                      # SG-010: every key disabled must not open the gateway
    assert rpc(client, path, "tools/list").status_code == 401
    assert next(g for g in client.get("/api/mcp-gateways").json() if g["id"] == gw["id"])["has_keys"] is True   # the UI must not call a closed gateway open
    client.patch(f"/api/mcp-keys/{key['id']}", json={"enabled": True})
    assert rpc(client, path, "tools/list", headers=bearer).status_code == 200
    time.sleep(0.3)
    calls = client.get(f"/api/mcp-gateways/{gw['id']}/calls").json()
    assert any(c["name"] == "a__search" and c["ok"] for c in calls["recent"]) and "SHOULD-NOT-BE-LOGGED" not in json.dumps(calls)
    assert calls["stats"] and calls["stats"][0]["calls"] >= 1
    assert client.delete(f"/api/mcp-keys/{key['id']}").status_code == 200
    assert rpc(client, path, "tools/list").status_code == 200                                      # no keys at all: open (explicit, UI says so)


def test_expired_key_and_retention(client, servers):
    a = add_ep(client, "alpha.test")
    gw = make_gateway(client, [("a", a)], {"a": ["tool:search"]})
    key = client.post(f"/api/mcp-gateways/{gw['id']}/keys", json={"expires_at": time.time() - 10}).json()
    assert rpc(client, f"/mcp/{gw['slug']}", "tools/list", headers={"Authorization": f"Bearer {key['key']}"}).status_code == 401
    with Session(db.engine) as s:
        s.add(McpCallLog(gateway_id=gw["id"], method="tools/call", name="old", ts=time.time() - 40 * 86400))
        s.commit()
    client.put("/api/settings", json={"mcp_retention_days": 30})
    retention.prune_once()
    with Session(db.engine) as s:
        assert s.exec(select(McpCallLog).where(McpCallLog.name == "old")).first() is None
    client.put("/api/settings", json={"mcp_retention_days": 0})


def test_admin_route_needs_no_key(client, servers):
    a = add_ep(client, "alpha.test")
    gw = make_gateway(client, [("a", a)], {"a": ["tool:search"]})
    client.post(f"/api/mcp-gateways/{gw['id']}/keys", json={})
    assert rpc(client, f"/mcp/{gw['slug']}", "tools/list").status_code == 401
    assert rpc(client, f"/api/admin-mcp/{gw['slug']}", "tools/list").json()["result"]["tools"][0]["name"] == "a__search"


def test_dashboard_mcp_stats(client, servers):
    a = add_ep(client, "alpha.test")
    gw = make_gateway(client, [("a", a)], {"a": ["tool:search", "tool:boom"]})
    path = f"/mcp/{gw['slug']}"
    for _ in range(3):
        rpc(client, path, "tools/call", {"name": "a__search", "arguments": {}})
    rpc(client, path, "tools/call", {"name": "a__boom"})
    time.sleep(0.4)
    d = client.get("/api/insights/mcp?minutes=60").json()
    ep = next(e for e in d["endpoints"] if e["id"] == a["id"])
    assert ep["calls"] == 4 and ep["errors"] == 1 and ep["tools"] == 2 and ep["status"] == "online" and ep["avg_ms"] is not None
    assert d["totals"]["calls"] >= 4 and d["totals"]["endpoints"] >= 1 and len(d["timeline"]) == 24
    assert d["tools"][0]["name"] == "a__search" and d["tools"][0]["calls"] == 3
    assert any(g["id"] == gw["id"] and g["calls"] == 4 for g in d["gateways"])
    assert sum(b["calls"] for b in d["timeline"]) == d["totals"]["calls"]


# ------------------------------------------------------------------ SG-013 / SG-014: batch limits and JSON-RPC validation
def post_raw(client, path, body):
    return client.post(path, json=body)


def test_batch_is_bounded_and_one_bad_message_does_not_sink_it(client, servers):
    a = add_ep(client, "alpha.test")
    gw = make_gateway(client, [("a", a)], {"a": ["tool:search"]})
    path = f"/mcp/{gw['slug']}"
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert post_raw(client, path, []).status_code == 400                                       # empty batch = invalid request
    too_many = post_raw(client, path, [{**ping, "id": i} for i in range(mcp_server.MAX_BATCH + 1)])
    assert too_many.status_code == 400 and too_many.json()["error"]["code"] == -32600
    mixed = post_raw(client, path, [ping, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": [1, 2]}, "junk", {**ping, "id": 4}]).json()
    by_id = {r["id"]: r for r in mixed}
    assert by_id[1]["result"] == {} and by_id[4]["result"] == {}
    assert by_id[2]["error"]["code"] == -32602                                                  # params is not an object
    assert any(r["error"]["code"] == -32600 for r in mixed if r["id"] is None)                  # the string is not a request


@pytest.mark.parametrize("msg,code", [
    ({"jsonrpc": "2.0", "id": 1, "method": 5}, -32600),
    ({"jsonrpc": "2.0", "id": [1], "method": "ping"}, -32600),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "a", "arguments": "x"}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []}, -32602),
])
def test_malformed_jsonrpc_gets_an_error_not_a_500(client, servers, msg, code):
    a = add_ep(client, "alpha.test")
    gw = make_gateway(client, [("a", a)], {"a": ["tool:search"]})
    r = post_raw(client, f"/mcp/{gw['slug']}", msg)
    assert r.status_code == 200 and r.json()["error"]["code"] == code


def test_batch_concurrency_is_capped(client, servers, monkeypatch):
    import asyncio
    state = {"now": 0, "peak": 0}

    async def slow(gw, key, msg):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    monkeypatch.setattr(mcp_server, "handle", slow)
    monkeypatch.setattr(mcp_server, "BATCH_CONCURRENCY", 3)
    a = add_ep(client, "alpha.test")
    gw = make_gateway(client, [("a", a)], {"a": ["tool:search"]})
    r = post_raw(client, f"/mcp/{gw['slug']}", [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(mcp_server.MAX_BATCH)])
    assert r.status_code == 200 and len(r.json()) == mcp_server.MAX_BATCH and state["peak"] == 3


def test_unexpected_handler_error_is_contained(client, servers, monkeypatch):
    async def boom(gw, key, msg):
        if msg["id"] == 2:
            raise RuntimeError("kaboom with /internal/path")
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    monkeypatch.setattr(mcp_server, "handle", boom)
    a = add_ep(client, "alpha.test")
    gw = make_gateway(client, [("a", a)], {"a": ["tool:search"]})
    out = {r["id"]: r for r in post_raw(client, f"/mcp/{gw['slug']}", [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in (1, 2, 3)]).json()}
    assert out[1]["result"] == {} and out[3]["result"] == {}
    assert out[2]["error"]["code"] == -32603 and "kaboom" not in json.dumps(out[2])
