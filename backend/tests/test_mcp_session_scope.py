"""Upstream MCP session scope: one per endpoint shared by all clients (default) or one per gateway key (session_scope="key")."""
from test_mcp import ALL_A, add_ep, client, make_gateway, rpc, servers  # noqa: F401  (fixtures)

from app.mcp import registry


def inits(fake):
    return sum(1 for m, _ in fake.requests if m == "initialize")


def make(client, scope):
    gw = make_gateway(client, [("a", add_ep(client, "alpha.test"))], {"a": ALL_A}, session_scope=scope)
    keys = [client.post(f"/api/mcp-gateways/{gw['id']}/keys", json={"name": n}).json()["key"] for n in ("k1", "k2")]
    return gw, keys


def call(client, gw, tok):
    r = rpc(client, f"/mcp/{gw['slug']}", "tools/call", {"name": "a__search", "arguments": {}}, headers={"Authorization": f"Bearer {tok}"}).json()
    assert "result" in r, r


def test_default_is_shared_and_unchanged(client, servers):
    gw, (k1, k2) = make(client, "shared")
    assert gw["session_scope"] == "shared"
    before = inits(servers["alpha.test"])
    call(client, gw, k1); call(client, gw, k2); call(client, gw, k1)
    assert inits(servers["alpha.test"]) - before == 0                         # every client rode the session that already existed


def test_per_key_scope_gives_each_key_its_own_session(client, servers):
    gw, (k1, k2) = make(client, "key")
    assert gw["session_scope"] == "key"
    before = inits(servers["alpha.test"])                                     # adding the endpoint already pinged it over the shared session
    call(client, gw, k1); call(client, gw, k2); call(client, gw, k1); call(client, gw, k2)
    assert inits(servers["alpha.test"]) - before == 2                         # one new session per key, each reused
    assert len({k for k in registry._sessions if k[1].startswith("key:")}) == 2


def test_scope_is_validated_editable_and_sessions_are_dropped_with_the_endpoint(client, servers):
    gw, _ = make(client, "shared")
    body = {"name": gw["name"], "slug": gw["slug"], "members": [{"endpoint_id": m["endpoint_id"], "prefix": m["prefix"], "enabled": True, "enabled_items": m["enabled_items"]} for m in gw["members"]]}
    assert client.put(f"/api/mcp-gateways/{gw['id']}", json={**body, "session_scope": "bogus"}).status_code == 422
    assert client.put(f"/api/mcp-gateways/{gw['id']}", json={**body, "session_scope": "key"}).json()["session_scope"] == "key"


def test_scoped_sessions_are_capped_per_endpoint(client, servers, monkeypatch):
    import asyncio
    monkeypatch.setattr(registry, "MAX_SCOPED_SESSIONS", 3)
    ep = add_ep(client, "alpha.test")

    async def go():
        from app.db import session
        from app.models import McpEndpoint
        with session() as s:
            e = s.get(McpEndpoint, ep["id"])
        for i in range(6):
            registry.session_for(e, f"key:{i}")
        await asyncio.sleep(0)
        await registry.drop_session(e.id)
        return [k for k in registry._sessions if k[0] == e.id]

    assert asyncio.run(go()) == []                                           # dropping the endpoint drops every scope


def test_cap_keeps_the_newest(client, servers, monkeypatch):
    import asyncio
    monkeypatch.setattr(registry, "MAX_SCOPED_SESSIONS", 3)
    ep = add_ep(client, "alpha.test")

    async def go():
        from app.db import session
        from app.models import McpEndpoint
        with session() as s:
            e = s.get(McpEndpoint, ep["id"])
        for i in range(6):
            registry.session_for(e, f"key:{i}")
        await asyncio.sleep(0)
        return sorted(k[1] for k in registry._sessions if k[0] == e.id and k[1])   # the shared "" session is not counted

    assert asyncio.run(go()) == ["key:3", "key:4", "key:5"]
