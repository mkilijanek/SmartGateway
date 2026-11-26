"""The MCP server a gateway exposes (Streamable HTTP, stateless): POST /mcp/{slug}.

It aggregates the tools / resources / prompts of the gateway's MCP endpoints under per-endpoint prefixes, forwards calls to the
owning endpoint and logs each call (never its arguments or result)."""
import asyncio
import json
import os
import re
import time
from collections import defaultdict, deque
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlmodel import select

from .. import events
from ..crypto import hash_key
from ..db import session
from ..models import McpApiKey, McpCallLog, McpEndpoint, McpGateway
from . import registry
from .client import McpError

router = APIRouter()
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BATCH = int(os.environ.get("SG_MCP_MAX_BATCH", "20"))          # JSON-RPC messages per POST
BATCH_CONCURRENCY = int(os.environ.get("SG_MCP_BATCH_CONCURRENCY", "5"))   # of those, how many run at once
_rpm: dict[int, deque] = defaultdict(deque)


def _rpc_err(id_, code: int, message: str, data=None) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message, **({"data": data} if data is not None else {})}}


def _http_err(status: int, message: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(_rpc_err(None, -32001 if status == 401 else -32600, message), status_code=status, headers=headers)


def _token(request: Request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()


def authenticate(slug: str, request: Request):
    """(gateway, key, error). A gateway with no keys at all is open; once it has any key, a disabled or expired key never reopens it."""
    with session() as s:
        gw = s.exec(select(McpGateway).where(McpGateway.slug == slug)).first()
        if gw is None:
            return None, None, _http_err(404, f"MCP gateway '{slug}' not found")
        keys = s.exec(select(McpApiKey).where(McpApiKey.gateway_id == gw.id)).all()   # disabled keys count: disabling the last one must not open the gateway
        if not keys:
            return gw, None, None
        tok = _token(request)
        key = next((k for k in keys if tok and k.key_hash == hash_key(tok)), None)
        if key is None or not key.enabled or (key.expires_at and key.expires_at < time.time()):
            return None, None, _http_err(401, "A valid API key is required", {"WWW-Authenticate": 'Bearer realm="smartgateway"'})
        key.last_used = time.time()
        s.add(key)
        s.commit()
        s.refresh(key)
        s.refresh(gw)
        return gw, key, None


def _allowed(key, field: str, name: str) -> bool:
    """A key's list for one kind of item; None (the default) allows all."""
    lst = getattr(key, field, None) if key else None
    return lst is None or name in lst


def _over_rpm(key) -> bool:
    if not key or not key.rpm_limit:
        return False
    q, now = _rpm[key.id], time.time()
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= key.rpm_limit:
        return True
    q.append(now)
    return False


def _log(gw_id: int, key, ep_id, method: str, name: str, ok: bool, ms: int, error: Optional[str]):
    def w():
        with session() as s:
            s.add(McpCallLog(gateway_id=gw_id, key_id=key.id if key else None, endpoint_id=ep_id, method=method, name=name[:200], ok=ok, total_ms=ms, error=(error or None) and error[:300]))
            s.commit()
    events.publish({"type": "mcp_call", "gateway_id": gw_id, "endpoint_id": ep_id, "method": method, "name": name, "ok": ok, "total_ms": ms})
    asyncio.get_running_loop().run_in_executor(None, w)


def _text_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


_SECRET = re.compile(r"(?i)(bearer\s+[\w.~+/=-]+|(?:api[-_ ]?key|token|secret|password|authorization)\s*[=:]\s*(?:(?:bearer|basic)\s+)?\S+|sk-[\w-]{8,})")


def public_error(err: McpError) -> tuple[int, str]:
    """What a gateway client may learn about an upstream failure: no response bodies, no hosts, no `data`; details stay in the call log."""
    if isinstance(err.data, dict) and "status" in err.data:
        st = err.data["status"]
        return -32000, "The upstream MCP server could not be reached" if not st else f"The upstream MCP server answered HTTP {st}"
    return err.code, _SECRET.sub("[redacted]", err.message)[:200]


async def _forward(gw, key, method: str, name: str, ep_entry: dict, fn):
    """Run one upstream call with timing + logging. Returns (result, error_message)."""
    t0 = time.time()
    err = None
    result = None
    try:
        with session() as s:
            ep = s.get(McpEndpoint, ep_entry["endpoint_id"])
        if ep is None:
            raise McpError("The MCP endpoint no longer exists")
        scope = (f"key:{key.id}" if key is not None else "open") if getattr(gw, "session_scope", "shared") == "key" else ""
        result = await fn(registry.session_for(ep, scope))
    except McpError as e:
        err = e
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        err = McpError(str(e) or e.__class__.__name__, -32000, {"status": 0})
    ms = int((time.time() - t0) * 1000)
    ok = err is None and not (isinstance(result, dict) and result.get("isError"))
    _log(gw.id, key, ep_entry["endpoint_id"], method, name, ok, ms, None if ok else (str(err) if err else "tool returned isError"))
    return result, err


async def handle(gw: McpGateway, key, msg: dict) -> Optional[dict]:
    """One JSON-RPC message -> response (None for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params")
    if id_ is not None and (isinstance(id_, bool) or not isinstance(id_, (str, int, float))):
        return _rpc_err(None, -32600, "Invalid request: id must be a string or a number")
    if not isinstance(method, str):
        return _rpc_err(id_, -32600, "Invalid request: method must be a string")
    if id_ is None and "id" not in msg:
        return None                              # notification (initialized, cancelled, ...)
    if params is None:
        params = {}
    if not isinstance(params, dict) or not isinstance(params.get("arguments"), (dict, type(None))):
        return _rpc_err(id_, -32602, "Invalid params")
    if method == "initialize":
        want = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": want if want in SUPPORTED else SUPPORTED[0],
            "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}, "prompts": {"listChanged": False}},
            "serverInfo": {"name": gw.name, "version": "1.0"}, **({"instructions": gw.description} if gw.description else {})}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if _over_rpm(key):
        return _rpc_err(id_, -32000, "Rate limit exceeded for this API key")
    cat = None
    if method in ("tools/list", "resources/list", "resources/templates/list", "prompts/list"):
        cat = registry.catalog(gw.id)
        if method == "tools/list":
            allowed = set(key.allowed_tools) if key and key.allowed_tools is not None else None
            return {"jsonrpc": "2.0", "id": id_, "result": {"tools": [t["definition"] for t in cat["tools"] if allowed is None or t["name"] in allowed]}}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": id_, "result": {"resources": [r["definition"] for r in cat["resources"] if _allowed(key, "allowed_resources", r["name"])]}}
        if method == "resources/templates/list":
            return {"jsonrpc": "2.0", "id": id_, "result": {"resourceTemplates": [r["definition"] for r in cat["templates"] if _allowed(key, "allowed_resources", r["name"])]}}
        return {"jsonrpc": "2.0", "id": id_, "result": {"prompts": [r["definition"] for r in cat["prompts"] if _allowed(key, "allowed_prompts", r["name"])]}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        entry = registry.resolve(gw.id, "tool", name)
        if entry is None or (key and key.allowed_tools is not None and name not in key.allowed_tools):
            return _rpc_err(id_, -32602, f"Unknown tool: {name}")
        res, err = await _forward(gw, key, method, name, entry, lambda s: s.call_tool(entry["upstream"], params.get("arguments")))
        if err is not None:
            code, msg = public_error(err)
            if isinstance(err.data, dict) and "status" in err.data:    # the server could not be reached / answered with an HTTP error
                return {"jsonrpc": "2.0", "id": id_, "result": _text_result(f"Upstream MCP server error: {msg}")}
            return _rpc_err(id_, code, msg)
        return {"jsonrpc": "2.0", "id": id_, "result": res}
    if method == "resources/read":
        uri = str(params.get("uri") or "")
        entry = registry.resolve(gw.id, "resource", uri)
        if entry is None or not _allowed(key, "allowed_resources", entry["name"]):
            return _rpc_err(id_, -32002, f"Resource not found: {uri}")
        res, err = await _forward(gw, key, method, uri, entry, lambda s: s.read_resource(entry["upstream"]))
        if err is not None:
            return _rpc_err(id_, *public_error(err))
        for c in res.get("contents") or []:      # report the gateway's uri, not the upstream one
            if isinstance(c, dict):
                c["uri"] = uri
        return {"jsonrpc": "2.0", "id": id_, "result": res}
    if method == "prompts/get":
        name = str(params.get("name") or "")
        entry = registry.resolve(gw.id, "prompt", name)
        if entry is None or not _allowed(key, "allowed_prompts", name):
            return _rpc_err(id_, -32602, f"Unknown prompt: {name}")
        res, err = await _forward(gw, key, method, name, entry, lambda s: s.get_prompt(entry["upstream"], params.get("arguments")))
        if err is not None:
            return _rpc_err(id_, *public_error(err))
        return {"jsonrpc": "2.0", "id": id_, "result": res}
    return _rpc_err(id_, -32601, f"Method not found: {method}")


async def serve(gw: McpGateway, key, request: Request) -> Response:
    if request.method == "DELETE":
        return Response(status_code=200)
    if request.method == "GET":
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(_rpc_err(None, -32700, "Parse error"), status_code=400)
    if isinstance(body, list):
        if not body or len(body) > MAX_BATCH:    # an empty batch is an invalid request per JSON-RPC; an oversized one is refused before any work starts
            return JSONResponse(_rpc_err(None, -32600, "Invalid batch: send between 1 and %d messages" % MAX_BATCH), status_code=400)
        sem = asyncio.Semaphore(max(1, BATCH_CONCURRENCY))

        async def one(m):
            async with sem:
                return await safe_handle(gw, key, m)
        out = [r for r in await asyncio.gather(*(one(m) for m in body)) if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    res = await safe_handle(gw, key, body)
    return JSONResponse(res) if res is not None else Response(status_code=202)


async def safe_handle(gw, key, msg):
    """handle() with the failure of one message contained: it answers with an internal error instead of failing a whole batch."""
    try:
        return await handle(gw, key, msg)
    except Exception:  # noqa: BLE001
        return _rpc_err(msg.get("id") if isinstance(msg, dict) and isinstance(msg.get("id"), (str, int)) else None, -32603, "Internal error")


@router.api_route("/mcp/{slug}", methods=["GET", "POST", "DELETE"])
@router.api_route("/mcp/{slug}/", methods=["GET", "POST", "DELETE"])
async def mcp_route(slug: str, request: Request):
    gw, key, e = authenticate(slug, request)
    if e:
        return e
    return await serve(gw, key, request)


@router.api_route("/api/admin-mcp/{slug}", methods=["GET", "POST", "DELETE"])
async def admin_mcp_route(slug: str, request: Request):
    """Same server without a key, behind the admin login: used by the UI tester."""
    with session() as s:
        gw = s.exec(select(McpGateway).where(McpGateway.slug == slug)).first()
    if gw is None:
        return _http_err(404, f"MCP gateway '{slug}' not found")
    return await serve(gw, None, request)
