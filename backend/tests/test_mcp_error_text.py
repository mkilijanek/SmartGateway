from app.mcp.client import McpError
from app.mcp.server import public_error


def test_http_errors_hide_the_upstream_body_and_host():
    e = McpError("HTTP 500: Traceback ... /srv/app/secret.py password=hunter2 at https://internal.corp/mcp", -32000, {"status": 500})
    code, msg = public_error(e)
    assert code == -32000 and msg == "The upstream MCP server answered HTTP 500"
    assert "internal.corp" not in msg and "hunter2" not in msg


def test_unreachable_upstream_has_a_stable_message():
    e = McpError("All connection attempts failed: http://10.0.0.5:9000", -32000, {"status": 0})
    assert public_error(e) == (-32000, "The upstream MCP server could not be reached")


def test_jsonrpc_errors_keep_code_and_message_but_lose_secrets_data_and_length():
    e = McpError("bad call, Authorization: Bearer abc.def-123 api_key=sk-LIVE12345678 " + "x" * 500, -32602, {"trace": "internal"})
    code, msg = public_error(e)
    assert code == -32602 and len(msg) <= 200
    assert "abc.def-123" not in msg and "sk-LIVE" not in msg and "[redacted]" in msg
    assert public_error(McpError("Unknown thing", -32601)) == (-32601, "Unknown thing")
