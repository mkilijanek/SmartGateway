"""OpenAI chat/completions <-> Anthropic Messages (what a Claude subscription login is accepted on).

request(): a chat/completions body -> a Messages body (system blocks, user/assistant content blocks, tool_use / tool_result, tools, stream=true).
chunks():  Anthropic's SSE events -> chat.completion.chunk dicts (text, thinking as reasoning_content, tool calls, usage, finish reason)."""
import json
import os
import time
import uuid
from typing import AsyncIterator, Optional

from . import AccountError
from .adapted import events

DEFAULT_MAX_TOKENS = 8192
# Anthropic accepts subscription tokens only for requests that present themselves as Claude Code. That is against Anthropic's terms for third-party
# tools, so it is off unless the admin turns it on (SG_CLAUDE_CODE_IDENTITY=1).


def identity_enabled() -> bool:
    return os.environ.get("SG_CLAUDE_CODE_IDENTITY", "").lower() in ("1", "true", "yes")


IDENTITY_TEXT = "You are Claude Code, Anthropic's official CLI for Claude."
BUILTIN_MODELS = [
    {"id": "claude-opus-5-5", "context": 200_000},
    {"id": "claude-sonnet-5-5", "context": 200_000},
    {"id": "claude-haiku-5-5", "context": 200_000},
]
CAPS = ["chat", "tools", "vision", "reasoning"]
EFFORT_BUDGET = {"low": 2048, "medium": 8192, "high": 16384}
STOP = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls", "refusal": "content_filter", "pause_turn": "stop"}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


def _image(url: str) -> Optional[dict]:
    if url.startswith("data:") and ";base64," in url:
        head, data = url.split(";base64,", 1)
        return {"type": "image", "source": {"type": "base64", "media_type": head[5:] or "image/png", "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _blocks(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and p.get("text"):
            out.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image_url":
            url = p.get("image_url")
            img = _image((url.get("url") if isinstance(url, dict) else url) or "")
            if img:
                out.append(img)
    return out


def _append(msgs: list, role: str, blocks: list) -> None:
    """Anthropic wants strictly alternating roles: consecutive turns of one role (e.g. several tool results) share one message."""
    if not blocks:
        return
    if msgs and msgs[-1]["role"] == role:
        msgs[-1]["content"].extend(blocks)
    else:
        msgs.append({"role": role, "content": list(blocks)})


def request(chat: dict, identity: Optional[bool] = None) -> dict:
    system, msgs = [], []
    for m in chat.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            t = _text(m.get("content"))
            if t:
                system.append({"type": "text", "text": t})
        elif role == "user":
            _append(msgs, "user", _blocks(m.get("content")))
        elif role == "assistant":
            blocks = _blocks(m.get("content"))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or "toolu_" + uuid.uuid4().hex[:20], "name": fn.get("name", ""), "input": args if isinstance(args, dict) else {}})
            _append(msgs, "assistant", blocks)
        elif role == "tool":
            _append(msgs, "user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "", "content": _text(m.get("content"))}])
    if identity if identity is not None else identity_enabled():
        system.insert(0, {"type": "text", "text": IDENTITY_TEXT})
    max_tokens = int(chat.get("max_completion_tokens") or chat.get("max_tokens") or DEFAULT_MAX_TOKENS)
    body: dict = {"model": chat.get("model"), "messages": msgs or [{"role": "user", "content": [{"type": "text", "text": "."}]}], "max_tokens": max_tokens, "stream": True}
    if system:
        body["system"] = system
    for k in ("temperature", "top_p"):
        if chat.get(k) is not None:
            body[k] = chat[k]
    stop = chat.get("stop")
    if stop:
        body["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)[:4]
    tools = [{"name": (t.get("function") or {}).get("name", ""), "description": (t.get("function") or {}).get("description", ""),
              "input_schema": (t.get("function") or {}).get("parameters") or {"type": "object", "properties": {}}}
             for t in chat.get("tools") or [] if t.get("type") == "function"]
    tc = chat.get("tool_choice")
    if tools and tc != "none":
        body["tools"] = tools
        if tc == "required":
            body["tool_choice"] = {"type": "any"}
        elif isinstance(tc, dict) and (tc.get("function") or {}).get("name"):
            body["tool_choice"] = {"type": "tool", "name": tc["function"]["name"]}
        elif tc == "auto":
            body["tool_choice"] = {"type": "auto"}
        if chat.get("parallel_tool_calls") is False and "tool_choice" in body:
            body["tool_choice"]["disable_parallel_tool_use"] = True
    effort = chat.get("reasoning_effort")
    if effort in EFFORT_BUDGET:   # extended thinking: needs a budget below max_tokens and no sampling overrides
        budget = EFFORT_BUDGET[effort]
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        body["max_tokens"] = max(body["max_tokens"], budget + 1024)
        body.pop("temperature", None)
        body.pop("top_p", None)
    return body


def _usage(u: dict) -> dict:
    cached = int(u.get("cache_read_input_tokens") or 0)
    p = int(u.get("input_tokens") or 0) + cached + int(u.get("cache_creation_input_tokens") or 0)
    c = int(u.get("output_tokens") or 0)
    out = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
    if cached:
        out["prompt_tokens_details"] = {"cached_tokens": cached}
    return out


async def chunks(lines: AsyncIterator[str], model: str) -> AsyncIterator[dict]:
    """chat.completion.chunk dicts for an Anthropic event stream. The last chunk carries finish_reason, then one with usage."""
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    blocks: dict = {}           # content block index -> {"type", "tool"}
    ntools = 0
    usage: dict = {}
    finish: Optional[str] = None

    def mk(delta: dict, fin=None) -> dict:
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}

    started = False
    async for ev in events(lines):
        t = ev.get("type", "")
        if t == "message_start":
            usage = dict((ev.get("message") or {}).get("usage") or {})
            started = True
            yield mk({"role": "assistant", "content": ""})
        elif t == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "tool_use":
                blocks[ev.get("index")] = {"type": "tool_use", "tool": ntools}
                yield mk({"tool_calls": [{"index": ntools, "id": cb.get("id"), "type": "function", "function": {"name": cb.get("name", ""), "arguments": ""}}]})
                ntools += 1
            else:
                blocks[ev.get("index")] = {"type": cb.get("type")}
        elif t == "content_block_delta":
            d, b = ev.get("delta") or {}, blocks.get(ev.get("index")) or {}
            if d.get("type") == "text_delta" and d.get("text"):
                yield mk({"content": d["text"]})
            elif d.get("type") == "thinking_delta" and d.get("thinking"):
                yield mk({"reasoning_content": d["thinking"]})
            elif d.get("type") == "input_json_delta" and b.get("type") == "tool_use":
                yield mk({"tool_calls": [{"index": b["tool"], "function": {"arguments": d.get("partial_json", "")}}]})
        elif t == "message_delta":
            finish = STOP.get((ev.get("delta") or {}).get("stop_reason") or "", finish)
            usage = {**usage, **(ev.get("usage") or {})}
        elif t == "error":
            e = ev.get("error") or {}
            raise AccountError("The model run failed: " + str(e.get("message") or e.get("type") or "error")[:300])
    if not started:
        yield mk({"role": "assistant", "content": ""})
    yield mk({}, finish or ("tool_calls" if ntools else "stop"))
    if usage:
        yield {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [], "usage": _usage(usage)}
