"""Make a vendor's response look like a chat/completions upstream: AdaptedResponse exposes status_code, headers, aread, aiter_lines and
aclose, and yields chat.completion(.chunk) shaped data, so the rest of proxy._route needs no vendor knowledge."""
import json
import time
import uuid
from typing import AsyncIterator, Callable, Optional

import httpx

from . import AccountError


async def events(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    async for line in lines:
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    yield json.loads(payload)
                except ValueError:
                    continue


def sse(chunk: dict) -> str:
    return "data: " + json.dumps(chunk, separators=(",", ":"))


async def collect(chunks: AsyncIterator[dict]) -> dict:
    """Fold a chat.completion.chunk stream into one chat.completion (for clients that did not ask for a stream)."""
    text, think, calls, finish, usage, cid, created, model = [], [], {}, "stop", {}, None, int(time.time()), ""
    async for ch in chunks:
        cid, created, model = ch["id"], ch["created"], ch.get("model") or model
        if ch.get("usage"):
            usage = ch["usage"]
        for c in ch.get("choices") or []:
            d = c.get("delta") or {}
            if d.get("content"):
                text.append(d["content"])
            if d.get("reasoning_content"):
                think.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                cur = calls.setdefault(tc["index"], {"id": tc.get("id"), "type": "function", "function": {"name": "", "arguments": ""}})
                cur["id"] = tc.get("id") or cur["id"]
                fn = tc.get("function") or {}
                cur["function"]["name"] += fn.get("name") or ""
                cur["function"]["arguments"] += fn.get("arguments") or ""
            if c.get("finish_reason"):
                finish = c["finish_reason"]
    msg: dict = {"role": "assistant", "content": "".join(text) or (None if calls else "")}
    if think:
        msg["reasoning_content"] = "".join(think)
    if calls:
        msg["tool_calls"] = [calls[i] for i in sorted(calls)]
    return {"id": cid or "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def error_json(status: int, raw: bytes) -> bytes:
    """The vendor's error body as an OpenAI-style error (their shapes vary: {"detail": ...}, {"error": {...}})."""
    msg = ""
    try:
        d = json.loads(raw)
        e = d.get("error") if isinstance(d, dict) else None
        msg = (e.get("message") if isinstance(e, dict) else e) or (d.get("detail") if isinstance(d, dict) else "") or (d.get("message") if isinstance(d, dict) else "") or ""
        if isinstance(msg, (dict, list)):
            msg = json.dumps(msg)
    except ValueError:
        msg = raw.decode("utf-8", "replace")
    return json.dumps({"error": {"message": str(msg)[:400] or f"HTTP {status}", "type": "upstream_error", "code": status}}).encode()


class AdaptedResponse:
    """`chunker(lines, model)` turns the vendor's SSE lines into chat.completion.chunk dicts."""

    def __init__(self, upstream: httpx.Response, model: str, stream: bool, chunker: Callable):
        self.up, self.model, self.stream, self.chunker = upstream, model, stream, chunker
        self.status_code = upstream.status_code
        ctype = "text/event-stream" if upstream.status_code < 400 and stream else "application/json"
        self.headers = httpx.Headers({"content-type": ctype})
        self._body: Optional[bytes] = None

    async def prepare(self) -> "AdaptedResponse":
        """A client that did not ask for a stream gets the whole answer: collect it now, so a failed run is a 502 (the gateway tries the next account)."""
        if not self.stream and self.status_code < 400:
            try:
                self._body = json.dumps(await collect(self.chunker(self._lines(), self.model))).encode()
            except AccountError as e:
                self.status_code = 502
                self._body = json.dumps({"error": {"message": str(e), "type": "upstream_error", "code": 502}}).encode()
            except Exception as e:  # noqa: BLE001 - connection dropped mid-answer
                self.status_code = 502
                self._body = json.dumps({"error": {"message": f"upstream read error: {type(e).__name__}", "type": "upstream_error", "code": 502}}).encode()
            await self.up.aclose()
        return self

    async def _lines(self):
        async for line in self.up.aiter_lines():
            yield line

    async def aread(self) -> bytes:
        if self._body is not None:
            return self._body
        if self.status_code >= 400:
            return error_json(self.status_code, await self.up.aread())
        return json.dumps(await collect(self.chunker(self._lines(), self.model))).encode()

    async def aiter_lines(self):
        if self.status_code >= 400:
            yield (await self.aread()).decode()
            return
        try:
            async for ch in self.chunker(self._lines(), self.model):
                yield sse(ch)
                yield ""
        except AccountError as e:
            yield sse({"error": {"message": str(e), "type": "upstream_error", "code": 502}})
            yield ""
        yield "data: [DONE]"
        yield ""

    async def aclose(self):
        await self.up.aclose()
