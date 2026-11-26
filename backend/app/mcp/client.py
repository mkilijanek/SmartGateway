"""Minimal MCP client over httpx: Streamable HTTP (2025-06-18 and earlier) with a fallback to the legacy HTTP+SSE transport.

Only what a gateway needs: initialize, list tools/resources/prompts (with pagination), call a tool, read a resource, get a
prompt, ping. Server-initiated requests (sampling, roots, elicitation) are not supported and are ignored."""
import asyncio
import itertools
import json
import os
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

import httpx

PROTOCOL = "2025-06-18"
MAX_RESPONSE_BYTES = int(float(os.environ.get("SG_MCP_MAX_RESPONSE_MB", "16")) * 1024 * 1024)   # one response body, or one SSE event (after decompression)
CALL_TIMEOUT = float(os.environ.get("SG_MCP_CALL_TIMEOUT_SECONDS", "120"))                         # total time for one RPC (not per read)
SSE_IDLE_TIMEOUT = float(os.environ.get("SG_MCP_SSE_IDLE_SECONDS", "600"))                      # the long-lived legacy SSE stream is dropped after this much silence
CLIENT_INFO = {"name": "smartgateway", "version": "1.0"}

_http: Optional[httpx.AsyncClient] = None


def _origin(url) -> tuple:
    try:
        u = urlsplit(str(url))
        return u.scheme.lower(), (u.hostname or "").lower(), u.port or {"http": 80, "https": 443}.get(u.scheme.lower())
    except ValueError:   # malformed port etc.: never equal to a real origin
        return ("?", str(url), None)


def _allowed_redirect(src, dst) -> bool:
    """Same origin, or the same host upgrading http -> https on the default ports (the usual reverse-proxy setup)."""
    return src == dst or (src[0] == "http" and dst[0] == "https" and src[1] == dst[1] and src[2] == 80 and dst[2] == 443)


async def _same_origin_redirects(response: httpx.Response) -> None:
    """Endpoint headers are secrets: never let a redirect carry them to another origin (httpx only strips Authorization)."""
    if response.is_redirect:
        loc = response.headers.get("location", "")
        if not _allowed_redirect(_origin(response.request.url), _origin(urljoin(str(response.request.url), loc))):
            raise McpError("The MCP server redirected to a different origin; refusing to follow", -32000, {"status": response.status_code})


def new_http(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), follow_redirects=True, event_hooks={"response": [_same_origin_redirects]}, **kw)


def get_http() -> httpx.AsyncClient:
    """Shared client; tests replace this function with one backed by httpx.MockTransport."""
    global _http
    if _http is None:
        _http = new_http()
    return _http


class McpError(Exception):
    def __init__(self, message: str, code: int = -32000, data: Any = None):
        super().__init__(message)
        self.message, self.code, self.data = message, code, data


class SessionExpired(McpError):
    pass


def _err(obj: dict) -> McpError:
    e = obj.get("error") or {}
    return McpError(str(e.get("message") or "MCP error"), int(e.get("code") or -32000), e.get("data"))


def _too_big() -> McpError:
    return McpError(f"The MCP server sent more than {MAX_RESPONSE_BYTES // (1024 * 1024)} MB in one response; aborted", -32000)


async def _read_limited(resp: httpx.Response, limit: Optional[int] = None) -> bytes:
    """Read a body but give up as soon as it exceeds the limit (counted after decompression)."""
    limit = MAX_RESPONSE_BYTES if limit is None else limit
    out = bytearray()
    async for chunk in resp.aiter_bytes():
        out += chunk
        if len(out) > limit:
            raise _too_big()
    return bytes(out)


async def _read_head(resp: httpx.Response, n: int = 4096) -> bytes:
    """The first n bytes of a body (an error page); never raises for a long one."""
    out = bytearray()
    async for chunk in resp.aiter_bytes():
        out += chunk
        if len(out) >= n:
            break
    return bytes(out[:n])


async def _events(resp: httpx.Response, total_limit: Optional[int] = None):
    """Yield (event, data) pairs of a text/event-stream body. One event is capped at MAX_RESPONSE_BYTES; a finite stream can also be capped as a whole."""
    st = {"event": "message", "data": [], "size": 0}

    def feed(line: str):
        if line == "":
            out = (st["event"], "\n".join(st["data"])) if st["data"] else None
            st.update(event="message", data=[], size=0)
            return out
        if line.startswith(":"):
            return None
        st["size"] += len(line)
        if st["size"] > MAX_RESPONSE_BYTES:
            raise _too_big()
        if line.startswith("event:"):
            st["event"] = line[6:].strip()
        elif line.startswith("data:"):
            st["data"].append(line[5:][1:] if line[5:6] == " " else line[5:])
        return None

    buf, total = b"", 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total_limit is not None and total > total_limit:
            raise _too_big()
        *lines, buf = (buf + chunk).split(b"\n")   # one pass per chunk, not one copy per line
        for raw in lines:
            if (ev := feed(raw.rstrip(b"\r").decode(errors="replace"))):
                yield ev
        if len(buf) > MAX_RESPONSE_BYTES:   # a line without a newline
            raise _too_big()
    if buf and (ev := feed(buf.rstrip(b"\r").decode(errors="replace"))):
        yield ev
    if st["data"]:
        yield st["event"], "\n".join(st["data"])


def _match(payload: Any, want: int) -> Optional[dict]:
    for obj in payload if isinstance(payload, list) else [payload]:
        if isinstance(obj, dict) and obj.get("id") == want and ("result" in obj or "error" in obj):
            return obj
    return None


class McpSession:
    """One connection to one MCP server. Requests are serialised only while (re)initialising."""

    def __init__(self, url: str, headers: dict, transport: str = "auto"):
        self.url, self.headers, self.transport = url, dict(headers or {}), transport
        self.session_id: Optional[str] = None
        self.protocol: Optional[str] = None
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.mode: Optional[str] = None       # streamable | sse
        self._ready = False
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._post_url = url                   # SSE: the message endpoint announced by the server
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ transport
    def _hdrs(self) -> dict:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **self.headers}
        if self.protocol and self.mode == "streamable":
            h["MCP-Protocol-Version"] = self.protocol
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def _post_streamable(self, payload: dict, want: Optional[int], timeout: float) -> Optional[dict]:
        async with get_http().stream("POST", self.url, json=payload, headers=self._hdrs(), timeout=timeout) as r:
            if r.status_code == 404 and self.session_id:
                raise SessionExpired("MCP session expired", -32001)
            if r.status_code >= 400:
                body = (await _read_head(r)).decode(errors="replace")[:300]
                raise McpError(f"HTTP {r.status_code}: {body}".strip(), -32000, {"status": r.status_code})
            if sid := r.headers.get("mcp-session-id"):
                self.session_id = sid
            if want is None:
                return None
            if "text/event-stream" in r.headers.get("content-type", ""):
                async for _ev, data in _events(r, MAX_RESPONSE_BYTES):
                    try:
                        hit = _match(json.loads(data), want)
                    except ValueError:
                        continue
                    if hit:
                        return hit
                raise McpError("The MCP server closed the stream without answering")
            try:
                hit = _match(json.loads(await _read_limited(r)), want)
            except ValueError:
                raise McpError("The MCP server sent a response that is not JSON")
            if hit is None:
                raise McpError("The MCP server sent no answer to the request")
            return hit

    async def _open_sse(self):
        ready: asyncio.Future = asyncio.get_running_loop().create_future()

        async def read():
            try:
                async with get_http().stream("GET", self.url, headers={**self.headers, "Accept": "text/event-stream"},
                                              timeout=httpx.Timeout(None, connect=10, read=SSE_IDLE_TIMEOUT)) as r:
                    if r.status_code >= 400:
                        raise McpError(f"HTTP {r.status_code} opening the SSE stream", -32000, {"status": r.status_code})
                    async for ev, data in _events(r):
                        if ev == "endpoint":
                            post_url = urljoin(self.url, data.strip())
                            if _origin(post_url) != _origin(self.url):   # the server may only point us at itself, or our headers would be sent elsewhere
                                raise McpError("The MCP server announced a message endpoint on a different origin; refusing to use it")
                            self._post_url = post_url
                            if not ready.done():
                                ready.set_result(True)
                        elif ev == "message":
                            try:
                                obj = json.loads(data)
                            except ValueError:
                                continue
                            for o in obj if isinstance(obj, list) else [obj]:
                                f = self._pending.pop(o.get("id"), None) if isinstance(o, dict) else None
                                if f and not f.done():
                                    f.set_result(o)
                    raise McpError("The SSE stream ended")
            except Exception as e:  # noqa: BLE001
                if not ready.done():
                    ready.set_exception(e if isinstance(e, McpError) else McpError(str(e) or e.__class__.__name__))
                for f in self._pending.values():
                    if not f.done():
                        f.set_exception(McpError(str(e) or "The SSE stream ended"))
                self._pending.clear()
                self._ready = False

        self._reader = asyncio.create_task(read())
        await asyncio.wait_for(ready, 15)

    async def _post_sse(self, payload: dict, want: Optional[int], timeout: float) -> Optional[dict]:
        fut = None
        if want is not None:
            fut = asyncio.get_running_loop().create_future()
            self._pending[want] = fut
        async with get_http().stream("POST", self._post_url, json=payload, headers={**self.headers, "Content-Type": "application/json"}, timeout=30) as r:
            if r.status_code >= 400:
                self._pending.pop(want, None)
                body = (await _read_head(r)).decode(errors="replace")[:300]
                raise McpError(f"HTTP {r.status_code}: {body}", -32000, {"status": r.status_code})
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise McpError("Timed out waiting for the MCP server") from None
        finally:
            self._pending.pop(want, None)   # also when the outer deadline cancels us

    async def _send(self, payload: dict, want: Optional[int], timeout: float) -> Optional[dict]:
        # one deadline for the whole exchange: httpx timeouts are per read, so a server that trickles bytes could otherwise hold the request forever
        try:
            async with asyncio.timeout(timeout):
                return await (self._post_sse if self.mode == "sse" else self._post_streamable)(payload, want, timeout)
        except TimeoutError:
            raise McpError("Timed out waiting for the MCP server", -32000, {"status": 0}) from None   # status 0 = could not be reached, as httpx timeouts were reported before

    # ------------------------------------------------------------ lifecycle
    async def _initialize(self):
        init = lambda i: {"jsonrpc": "2.0", "id": i, "method": "initialize", "params": {  # noqa: E731
            "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT_INFO}}
        res = None
        if self.transport in ("auto", "streamable_http"):
            self.mode, self.session_id, self.protocol = "streamable", None, None
            i = next(self._ids)
            try:
                res = await self._send(init(i), i, 30)
            except McpError as e:
                status = (e.data or {}).get("status") if isinstance(e.data, dict) else None
                if not (self.transport == "auto" and status in (400, 404, 405)):
                    raise
        if res is None:
            self.mode, self.session_id = "sse", None
            await self._open_sse()
            i = next(self._ids)
            res = await self._send(init(i), i, 30)
        if "error" in res:
            raise _err(res)
        r = res.get("result") or {}
        self.protocol = r.get("protocolVersion") or PROTOCOL
        self.server_info, self.capabilities = r.get("serverInfo") or {}, r.get("capabilities") or {}
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, None, 15)
        self._ready = True

    async def close(self):
        if self._reader:
            self._reader.cancel()
        if self.mode == "streamable" and self.session_id:
            try:
                await get_http().delete(self.url, headers=self._hdrs(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._ready = False

    async def ensure(self):
        if not self._ready:
            async with self._lock:
                if not self._ready:
                    await self._initialize()

    async def request(self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
        timeout = CALL_TIMEOUT if timeout is None else timeout
        for attempt in (0, 1):
            await self.ensure()
            i = next(self._ids)
            try:
                res = await self._send({"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params is not None else {})}, i, timeout)
            except SessionExpired:
                self._ready = False
                if attempt:
                    raise
                continue
            if "error" in res:
                raise _err(res)
            return res.get("result")

    # ------------------------------------------------------------ operations
    async def list_all(self, method: str, key: str, limit: int = 50) -> list:
        out, cursor = [], None
        for _ in range(limit):
            r = await self.request(method, {"cursor": cursor} if cursor else {}) or {}
            out += r.get(key) or []
            cursor = r.get("nextCursor")
            if not cursor:
                break
        return out

    async def ping(self):
        await self.request("ping", None, 15)

    async def call_tool(self, name: str, arguments: Optional[dict]) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}}) or {}

    async def read_resource(self, uri: str) -> dict:
        return await self.request("resources/read", {"uri": uri}) or {}

    async def get_prompt(self, name: str, arguments: Optional[dict]) -> dict:
        return await self.request("prompts/get", {"name": name, **({"arguments": arguments} if arguments else {})}) or {}
