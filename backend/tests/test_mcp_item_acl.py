"""Per-key allow-lists for resources and prompts (tools had one already). None = everything, as before."""
from test_mcp import ALL_A, add_ep, client, make_gateway, rpc, servers  # noqa: F401  (fixtures)


def key(client, gw, **kw):
    r = client.post(f"/api/mcp-gateways/{gw['id']}/keys", json={"name": "k", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def call(client, path, tok, method, params=None):
    return rpc(client, path, method, params, headers={"Authorization": f"Bearer {tok}"}).json()


def test_key_limited_to_listed_resources_and_prompts(client, servers):
    gw = make_gateway(client, [("a", add_ep(client, "alpha.test"))], {"a": ALL_A})
    path = f"/mcp/{gw['slug']}"
    open_key = key(client, gw)                                                    # no lists: sees everything, as before
    narrow = key(client, gw, allowed_resources=[], allowed_prompts=[])
    one = key(client, gw, allowed_resources=["mcp://a/file:///notes.txt"], allowed_prompts=["a__greet"])

    assert len(call(client, path, open_key["key"], "resources/list")["result"]["resources"]) == 1
    assert len(call(client, path, open_key["key"], "prompts/list")["result"]["prompts"]) == 1

    for method, k in (("resources/list", "resources"), ("resources/templates/list", "resourceTemplates"), ("prompts/list", "prompts")):
        assert call(client, path, narrow["key"], method)["result"][k] == []        # empty list = nothing
    r = call(client, path, narrow["key"], "resources/read", {"uri": "mcp://a/file:///notes.txt"})
    assert r["error"]["code"] == -32002
    r = call(client, path, narrow["key"], "resources/read", {"uri": "mcp://a/db://rows/42"})      # a template match is covered too
    assert r["error"]["code"] == -32002
    assert call(client, path, narrow["key"], "prompts/get", {"name": "a__greet"})["error"]["code"] == -32602
    assert servers["alpha.test"].calls == []

    assert [x["uri"] for x in call(client, path, one["key"], "resources/list")["result"]["resources"]] == ["mcp://a/file:///notes.txt"]
    assert call(client, path, one["key"], "resources/read", {"uri": "mcp://a/file:///notes.txt"})["result"]["contents"][0]["text"] == "read file:///notes.txt"
    assert call(client, path, one["key"], "resources/read", {"uri": "mcp://a/db://rows/42"})["error"]["code"] == -32002      # the template is not on its list
    assert "result" in call(client, path, one["key"], "prompts/get", {"name": "a__greet"})


def test_key_lists_survive_update_and_are_reported(client, servers):
    gw = make_gateway(client, [("a", add_ep(client, "alpha.test"))], {"a": ALL_A})
    k = key(client, gw, allowed_prompts=["a__greet"])
    assert k["allowed_prompts"] == ["a__greet"] and k["allowed_resources"] is None
    patched = client.patch(f"/api/mcp-keys/{k['id']}", json={"name": "k", "allowed_resources": ["mcp://a/file:///notes.txt"]}).json()
    assert patched["allowed_resources"] == ["mcp://a/file:///notes.txt"] and patched["allowed_prompts"] is None   # PATCH replaces the whole body, like for tools
