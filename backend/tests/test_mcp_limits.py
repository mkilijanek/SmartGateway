import json
import time

import httpx
import pytest

from app.mcp import client as mcp_client
from app.mcp.client import McpError, McpSession


# ------------------------------------------------------------------ response size and deadlines
def _client_with(monkeypatch, handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(mcp_client, "get_http", lambda: http)


def _ready_session(url="http://big.test/mcp"):
    s = McpSession(url, {}, "streamable_http")
    s._ready, s.mode, s.protocol = True, "streamable", "2025-06-18"
    return s


async def test_oversized_json_response_is_aborted(monkeypatch):
    monkeypatch.setattr(mcp_client, "MAX_RESPONSE_BYTES", 1000)
    _client_with(monkeypatch, lambda req: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"blob": "x" * 5000}}))
    with pytest.raises(McpError, match="more than"):
        await _ready_session().request("tools/list")


async def test_oversized_sse_event_and_unterminated_line_are_aborted(monkeypatch):
    monkeypatch.setattr(mcp_client, "MAX_RESPONSE_BYTES", 1000)
    big = ("event: message\ndata: " + "x" * 5000 + "\n\n").encode()
    _client_with(monkeypatch, lambda req: httpx.Response(200, content=big, headers={"content-type": "text/event-stream"}))
    with pytest.raises(McpError, match="more than"):
        await _ready_session().request("tools/list")
    _client_with(monkeypatch, lambda req: httpx.Response(200, content=b"data: " + b"y" * 5000, headers={"content-type": "text/event-stream"}))
    with pytest.raises(McpError, match="more than"):
        await _ready_session().request("tools/list")


async def test_response_within_the_limit_still_parses(monkeypatch):
    body = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "t"}]}}
    sse = f"event: message\r\ndata: {json.dumps(body)}\r\n\r\n".encode()          # CRLF framing, last event not followed by more data
    _client_with(monkeypatch, lambda req: httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"}))
    assert (await _ready_session().request("tools/list")) == {"tools": [{"name": "t"}]}


async def test_total_deadline_covers_a_server_that_never_finishes(monkeypatch):
    import asyncio

    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    _client_with(monkeypatch, slow)
    t0 = time.time()
    with pytest.raises(McpError, match="Timed out"):
        await _ready_session().request("tools/list", timeout=0.2)
    assert time.time() - t0 < 2


async def test_large_error_page_keeps_its_status_and_the_sse_fallback(monkeypatch):
    _client_with(monkeypatch, lambda req: httpx.Response(404, content=b"<html>" + b"x" * 10_000 + b"</html>"))   # bigger than the error-body peek
    reached = []

    async def fake_open_sse(self):
        reached.append(True)
        raise McpError("sse fallback reached")

    monkeypatch.setattr(McpSession, "_open_sse", fake_open_sse)
    with pytest.raises(McpError, match="sse fallback reached"):
        await McpSession("http://big.test/mcp", {}, "auto")._initialize()
    assert reached


async def test_error_status_is_reported_for_a_large_body(monkeypatch):
    _client_with(monkeypatch, lambda req: httpx.Response(500, content=b"e" * 10_000))
    with pytest.raises(McpError) as e:
        await _ready_session().request("tools/list")
    assert e.value.data == {"status": 500} and len(e.value.message) < 400


async def test_many_short_lines_are_parsed_in_linear_time(monkeypatch):
    body = (b":\n" * 300_000) + b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
    _client_with(monkeypatch, lambda req: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}))
    t0 = time.time()
    assert await _ready_session().request("ping") == {}
    assert time.time() - t0 < 3


async def test_call_timeout_default_is_configurable_and_reports_status_zero(monkeypatch):
    import asyncio

    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    monkeypatch.setattr(mcp_client, "CALL_TIMEOUT", 0.2)
    _client_with(monkeypatch, slow)
    with pytest.raises(McpError) as e:
        await _ready_session().request("tools/list")
    assert e.value.data == {"status": 0}


async def test_sse_pending_future_is_removed_when_the_deadline_hits(monkeypatch):
    _client_with(monkeypatch, lambda req: httpx.Response(202))
    s = McpSession("http://big.test/mcp", {}, "legacy_sse")
    s._ready, s.mode, s._post_url = True, "sse", "http://big.test/messages"
    with pytest.raises(McpError, match="Timed out"):
        await s.request("tools/list", timeout=0.2)
    assert s._pending == {}
