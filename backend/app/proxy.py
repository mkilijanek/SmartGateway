"""OpenAI-compatible gateway surface with smart routing, per-key limits, caching and API compat layers."""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlmodel import select

from . import config, events, settings_store, unified
from .voices import parse_voices as _parse_voices, voice_id as _voice_id
from .compat import anthropic as anth
from .compat import responses as resp_api
from . import crypto
from .crypto import hash_key
from .db import session
from . import guardrails
from .guardrails import library
from .compression import pipeline as comp_pipeline
from .compression.aligner import analyze_alignment
from .compression.context import analyze_context
from .models import ApiKey, CompressionEvent, Endpoint, Gateway, GatewayModel, Member, Model, RequestLog
from .runtime_ctx import no_request_log, verbose
from .router import affinity, cache, health
from .router.limits import LimitError, limiter
from .router.scheduler import NoCandidates, NoCapacity, scheduler
from .router.stream_cache import META_KEY, StreamAggregator, split_meta, synthesize_stream
from .router.strategies import Candidate

router = APIRouter()
log = logging.getLogger("smartgateway.proxy")

client = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0), limits=httpx.Limits(max_connections=2000))

CHAT_ROUTES = {"chat/completions", "completions"}
SUPPORTED = CHAT_ROUTES | {"embeddings", "audio/transcriptions", "audio/translations", "audio/speech",
                           "images/generations", "images/edits", "models"}
HOP = {"content-length", "transfer-encoding", "connection", "content-encoding", "keep-alive"}
# Upstream response headers that must never reach a gateway client: cookies would be set on the gateway's origin, CORS headers would open
# the gateway to any website in a browser, and server/proxy banners reveal the upstream stack.
_STRIP_UP = HOP | {"set-cookie", "set-cookie2", "server", "x-powered-by", "via", "alt-svc", "report-to", "nel", "strict-transport-security",
                   "content-security-policy", "x-frame-options", "www-authenticate", "proxy-authenticate"}


def up_headers(h) -> dict:
    return {k: v for k, v in h.items() if k.lower() not in _STRIP_UP and not k.lower().startswith(("access-control-", "cf-", "x-amz", "x-envoy", "x-forwarded"))}


def public_error(error: str, attempts: int = 0) -> str:
    """Client-facing text for an upstream failure; the full text only goes to the log and to the admin."""
    if verbose.get():
        return f"Upstream failed after {attempts} attempt(s): {error}"
    return "The upstream model server failed to answer. Try again later." if attempts else "No upstream model server is available. Try again later."
OVERFLOW_RX = re.compile(r"(context (length|size|window)|exceeds? the (available )?context|maximum context|"
                         r"too many tokens|prompt is too long|n_ctx|exceed_context)", re.I)


def err(status: int, message: str, typ: str = "gateway_error", headers: Optional[dict] = None):
    return JSONResponse(
        {"error": {"message": message, "type": typ, "code": status}}, status_code=status, headers=headers
    )


# ---------------------------------------------------------------- request analysis
def _content_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    n = 0
    for p in content or []:
        if isinstance(p, dict):
            if p.get("type") == "text":
                n += len(p.get("text", ""))
            elif p.get("type") in ("image_url", "input_audio", "video_url"):
                n += 3000  # ~850 tokens
    return n


def estimate_tokens(route: str, body: dict) -> tuple[int, int]:
    """Rough (prompt_tokens, max_output_tokens) estimate: ~3.5 chars per token."""
    chars = 0
    if route in CHAT_ROUTES:
        for m in body.get("messages") or []:
            if isinstance(m, dict):
                chars += _content_chars(m.get("content")) + 16
        if isinstance(body.get("prompt"), str):
            chars += len(body["prompt"])
        for k in ("tools", "functions"):
            if body.get(k):
                chars += len(json.dumps(body[k]))
    elif route == "embeddings":
        inp = body.get("input")
        chars = len(inp) if isinstance(inp, str) else sum(len(str(x)) for x in inp or [])
    out = int(body.get("max_tokens") or body.get("max_completion_tokens") or 0)
    return int(chars / 3.5), out


def detect_requirements(route: str, body: dict, header_req: str = "") -> tuple[set, set]:
    """Return (hard_requirements, soft_preferences). Soft ones apply only if some member satisfies them."""
    hard: set = set()
    soft: set = set()
    if route == "embeddings":
        hard.add("embedding")
    elif route == "audio/speech":
        hard.add("tts")
    elif route.startswith("images/"):
        hard.add("image_gen")
    elif route.startswith("audio/"):
        hard.add("audio")
    elif route in CHAT_ROUTES:
        hard.add("chat")
        for m in body.get("messages") or []:
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, list):
                for part in content:
                    t = part.get("type") if isinstance(part, dict) else None
                    if t == "image_url":
                        hard.add("vision")
                    elif t in ("input_audio", "audio_url"):
                        hard.add("audio")
                    elif t == "video_url":
                        hard.add("video")
        if any(k in body for k in ("reasoning_effort", "reasoning", "thinking", "enable_thinking")):
            soft.add("reasoning")
        if body.get("tools") or body.get("functions"):
            soft.add("tools")  # soft: models that were never probed for tools stay eligible
    for r in header_req.split(","):
        r = r.strip().lower()
        if r in config.CAPABILITIES:
            hard.add(r)
    return hard, soft


def required_tier(prompt_tokens: int, body: dict, soft: set) -> int:
    t = 1 if prompt_tokens < 1500 else 2 if prompt_tokens < 6000 else 3
    if "reasoning" in soft:
        t += 1
    if body.get("tools"):
        t += 1
    if int(body.get("max_tokens") or body.get("max_completion_tokens") or 0) > 2000:
        t += 1
    return min(3, t)


# ---------------------------------------------------------------- data loading
def load_members(gm_id: int) -> list[Candidate]:
    out = []
    with session() as s:
        for mem in s.exec(select(Member).where(Member.gateway_model_id == gm_id)).all():
            if not mem.enabled:
                continue
            m = s.get(Model, mem.model_id)
            if not m or not m.enabled:
                continue
            ep = s.get(Endpoint, m.endpoint_id)
            if not ep:
                continue
            if crypto.is_unreadable(ep.api_key):   # never call an upstream without the credentials it needs
                log.warning("endpoint %s skipped: its stored API key cannot be decrypted", ep.name)
                continue
            health.status.setdefault(ep.id, ep.status)
            out.append(
                Candidate(
                    member_id=mem.id, model_id=m.id, endpoint_id=ep.id, endpoint_name=ep.name,
                    base_url=ep.base_url.rstrip("/"), api_key=ep.api_key, upstream_id=m.upstream_id,
                    caps=set(m.capabilities or []), limit=m.max_slots, member_limit=mem.slots_override,
                    weight=mem.weight, priority=mem.priority, context_length=m.context_length,
                    is_fallback=bool(mem.is_fallback), tier=mem.tier or 2, provider=ep.provider,
                )
            )
    return out


def _token_from(request: Request) -> str:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def authenticate(slug: Optional[str], request: Request, allow_internal: bool = False):
    """Return (gateway, api_key_row_or_None, error_response_or_None)."""
    token = _token_from(request)
    with session() as s:
        key = None
        if slug:
            gw = s.exec(select(Gateway).where(Gateway.slug == slug)).first()
            if not gw or gw.slug == "__council__" or (gw.slug == unified.INTERNAL_SLUG and not allow_internal):   # the hidden gateway behind unified models is not addressable on its own
                return None, None, err(404, f"Gateway '{slug}' not found", "not_found")
        else:
            if not token:
                return None, None, err(401, "Use /gw/<slug>/v1/... or a gateway API key as Bearer token", "invalid_api_key")
            key = s.exec(select(ApiKey).where(ApiKey.key_hash == hash_key(token))).first()
            gw = s.get(Gateway, key.gateway_id) if key else None
            if not gw:
                return None, None, err(401, "Invalid API key", "invalid_api_key")
        keys = s.exec(select(ApiKey).where(ApiKey.gateway_id == gw.id)).all()
        if not keys and not gw.api_key:
            return gw, None, None  # open gateway
        if key is None and token:
            key = next((k for k in keys if k.key_hash == hash_key(token)), None)
        if key is None and gw.api_key and token and hmac.compare_digest(token.encode(), gw.api_key.encode()):  # un-migrated legacy key
            return gw, None, None
        if key is None or not key.enabled or (key.expires_at and key.expires_at < time.time()):
            return None, None, err(401, "Invalid or missing gateway API key", "invalid_api_key")
        s.expunge(key)
        return gw, key, None


def public_models(gw: Gateway, key=None) -> list[dict]:
    out = []
    with session() as s:
        for gm in s.exec(select(GatewayModel).where(GatewayModel.gateway_id == gw.id, GatewayModel.enabled == True)).all():  # noqa: E712
            caps: set = set()
            for c in load_members(gm.id):
                caps |= c.caps
            for n in model_names(gm):
                if key and key.allowed_models and gm.public_name not in key.allowed_models and n not in key.allowed_models:
                    continue
                out.append({
                    "id": n, "object": "model", "created": int(gw.created_at),
                    "owned_by": f"smartgateway/{gw.slug}", "capabilities": sorted(caps),
                })
    return out


# ---------------------------------------------------------------- GET /audio/voices
_VOICES_TTL, _VOICES_NEG_TTL = 60, 10
_voices_cache: dict = {}  # (upstream base_url, upstream model id) -> (expires, voices)


async def _upstream_voices(c: Candidate) -> list:
    ckey = (c.base_url, c.upstream_id)
    hit = _voices_cache.get(ckey)
    if hit and hit[0] > time.time():
        return hit[1]
    headers = {"Authorization": f"Bearer {c.api_key}"} if c.api_key else {}
    if c.provider:
        headers.update(provider_headers(c.provider, {}, {}))
    voices: list = []
    try:
        r = await client.get(f"{c.base_url}/audio/voices", params={"model": c.upstream_id}, headers=headers, timeout=httpx.Timeout(10.0, connect=5.0))
        if r.status_code == 200:
            voices = _parse_voices(r.json())
        else:
            log.info("upstream %s has no /audio/voices (HTTP %s)", c.endpoint_name, r.status_code)
    except Exception as e:  # unreachable upstream, not JSON, ...
        log.info("voices request to %s failed: %s", c.endpoint_name, e)
    _voices_cache[ckey] = (time.time() + (_VOICES_TTL if voices else _VOICES_NEG_TTL), voices)
    return voices


async def _voices_list(gw: Gateway, key, wanted: str = ""):
    """(voices, error response) for one TTS model (`wanted`) or for every TTS model of the gateway the key may use. A voice list set on the
    model wins; otherwise the upstream's own /audio/voices answer is used. The list may be empty when no model has voices."""
    with session() as s:
        gms = [g for g in s.exec(select(GatewayModel).where(GatewayModel.gateway_id == gw.id, GatewayModel.enabled == True)).all()]  # noqa: E712
        for g in gms:
            s.expunge(g)
    allowed = key.allowed_models if key is not None and key.allowed_models else None
    if wanted:
        gm, shown = match_model(gms, wanted)
        if gm is None:
            return None, err(404, f"Model '{wanted}' not found on this gateway. Available: "
                                  + ", ".join(n for g in gms for n in model_names(g)), "model_not_found")
        if allowed and gm.public_name not in allowed and shown not in allowed:
            return None, err(403, f"This API key is not allowed to use model '{shown}'", "permission_denied")
        picked = [gm]
    else:
        picked = [g for g in gms if not allowed or g.public_name in allowed or any(n in allowed for n in model_names(g))]
    work = []  # (GatewayModel, tts members)
    for g in picked:
        tts = [c for c in load_members(g.id) if "tts" in c.caps]
        if tts:
            work.append((g, tts))
    if wanted and not work:
        return None, err(400, f"Model '{wanted}' has no text-to-speech member", "capability_unavailable")

    async def voices_of(g, tts) -> list:
        if g.voices:
            return list(g.voices)
        online = [c for c in tts if health.status.get(c.endpoint_id) != "offline"]
        found: list = []
        for part in await asyncio.gather(*(_upstream_voices(c) for c in online)):
            found += part
        return found

    merged: list = []
    for part in await asyncio.gather(*(voices_of(g, t) for g, t in work)):
        merged += part
    return merged, None


def _merge_voices(parts: list) -> list:
    out, seen = [], set()
    for v in parts:
        vid = _voice_id(v)
        if vid and vid not in seen:
            seen.add(vid)
            out.append(v)
    return out


def _voices_reply(voices: list):
    if not voices:
        return err(404, "No voices are available. Set a voice list on the TTS model in the gateway editor, or use an upstream "
                        "that serves /audio/voices.", "voices_unavailable")
    return {"voices": voices}


async def audio_voices(gw: Gateway, key, wanted: str = ""):
    """GET /audio/voices on a gateway URL."""
    voices, e = await _voices_list(gw, key, wanted)
    return e or _voices_reply(_merge_voices(voices))


async def unified_voices(aliases: list[dict], mode: str, request: Request):
    """GET /unified/v1/audio/voices without ?model=: the voices of every TTS model the caller may use on the unified endpoint
    (exposed gateways and directly exposed models), merged. Access is decided exactly like for /unified/v1/models."""
    strict = bool(unified.keys())
    parts: list = []
    seen: set = set()
    for a in aliases:
        gw = a["gateway"]
        if (gw.id, a["model"]) in seen:   # the gateway name, Name/model and extra names all point at the same model
            continue
        seen.add((gw.id, a["model"]))
        if mode == "all":
            key = None
        else:
            g, key, e = authenticate(gw.slug, request, allow_internal=True)
            if e or (strict and key is None):
                continue
        voices, e = await _voices_list(gw, key, a["model"])
        if not e:
            parts += voices
    return _voices_reply(_merge_voices(parts))


async def log_request(rec: RequestLog, comp_events: Optional[list] = None) -> None:
    if no_request_log.get():  # synthetic stress-test traffic is not logged
        return
    def _w():
        with session() as s:
            s.add(rec)
            if rec.key_id:
                k = s.get(ApiKey, rec.key_id)
                if k:
                    k.last_used = time.time()
                    s.add(k)
            if comp_events:
                s.flush()
                for e in comp_events:
                    s.add(CompressionEvent(request_log_id=rec.id, ts=rec.ts, gateway_id=rec.gateway_id,
                                           public_model=rec.public_model, **e))
            s.commit()
    await asyncio.to_thread(_w)


def provider_headers(provider: str, headers_in, body: dict) -> dict:
    """Extra upstream headers for hosted catalogs. OpenCode Go rejects requests without x-opencode-session (and both ask clients to identify
    themselves). The session id is stable for one conversation (X-SG-Session > user > hash of system prompt + first user message)."""
    if not provider.startswith("opencode"):
        return {}
    skey = affinity.session_key(headers_in.get("x-sg-session", ""), body)
    return {"x-opencode-session": "sg-" + hashlib.sha256(skey.encode()).hexdigest()[:24], "User-Agent": f"SmartGateway/{config.VERSION}"}


def analyze_request(route: str, body: dict, headers_in) -> dict:
    """Cache-alignment + context-composition fields for the request log (sizes/hashes only, never content)."""
    if route != "chat/completions" or not settings_store.get_cached().get("context_insights_enabled", True):
        return {}
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return {}
    al = analyze_alignment(msgs)
    cx = analyze_context(body)
    return {"alignment_score": al["score"], "volatile_findings": al["findings"], "volatile_labels": al["labels"] or None,
            "prefix_hash": al["prefix_hash"], "session_key": affinity.session_key(headers_in.get("x-sg-session", ""), body),
            "ctx_system_chars": cx["system_chars"], "ctx_tools_chars": cx["tools_chars"], "ctx_messages_chars": cx["messages_chars"],
            "ctx_blocks": cx["blocks"] or None}


def rewrite_model(obj, public: str):
    if isinstance(obj, dict) and "model" in obj:
        obj["model"] = public
    return obj


def cache_plan(gm, route: str, body: dict, header: str, is_stream: bool) -> str:
    """off | bypass | skip | use — whether this request should read/write the response cache (streams only if the model opted in)."""
    if not gm.cache_enabled or route not in (CHAT_ROUTES | {"embeddings"}):
        return "off"
    if is_stream:  # streams are cached only when the model opted in; legacy completions and multi-choice/logprobs are never cached
        if not getattr(gm, "cache_streaming", False) or route != "chat/completions":
            return "off"
        if body.get("logprobs") or body.get("top_logprobs") or int(body.get("n") or 1) > 1:
            return "off"
    if header == "0":
        return "bypass"
    if header == "1" or gm.cache_any_temperature or route == "embeddings" or body.get("temperature") == 0:
        return "use"
    return "skip"  # non-deterministic request on a model that only caches temperature 0


_bg_tasks: set = set()


def _spawn(coro) -> None:
    """Fire-and-forget background task with a strong reference (cache writes must not delay a client's stream)."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


# ---------------------------------------------------------------- core dispatch
def model_names(gm) -> list[str]:
    """The public name followed by its aliases."""
    return [gm.public_name, *(gm.aliases or [])]


def match_model(gms, wanted: str):
    """(GatewayModel, the exposed name that matched) - exact match on a name or alias first, then case-insensitive."""
    for exact in (True, False):
        for g in gms:
            for n in model_names(g):
                if n == wanted if exact else n.lower() == wanted.lower():
                    return g, n
    return None, None


async def dispatch(gw: Gateway, key, route: str, body: dict, form_files, headers_in):
    """Route one parsed request. Returns a FastAPI Response (JSON or streaming)."""
    with session() as s:
        every = s.exec(select(GatewayModel).where(GatewayModel.gateway_id == gw.id)).all()
    gms = [g for g in every if g.enabled]
    if not every:
        return err(404, "Gateway has no models configured", "not_found")
    wanted = str(body.get("model") or "").strip()
    if not gms:
        return err(404, "All models of this gateway are disabled", "model_not_found")
    gm, shown = match_model(gms, wanted)
    if gm is None and match_model([g for g in every if not g.enabled], wanted)[0] is not None:
        return err(404, f"Model '{wanted}' is disabled on this gateway. Available: " + ", ".join(n for g in gms for n in model_names(g)), "model_not_found")
    if gm is None and not wanted and len(gms) == 1:
        gm = gms[0]
        shown = gm.public_name
    if gm is None:
        return err(404, f"Model '{wanted}' not found on this gateway. Available: "
                        + ", ".join(n for g in gms for n in model_names(g)), "model_not_found")
    if key is not None and key.allowed_models and gm.public_name not in key.allowed_models and shown not in key.allowed_models:
        return err(403, f"This API key is not allowed to use model '{shown}'", "permission_denied")
    if shown != gm.public_name:  # requested under an alias: answer, log and cache under that name
        gm = gm.model_copy(update={"public_name": shown})

    # per-model defaults / system prompt / ceiling (pass-through unless configured)
    body = dict(body)
    if route in CHAT_ROUTES:
        for k, v in (gm.default_params or {}).items():
            body.setdefault(k, v)
        if gm.system_prompt and route == "chat/completions":
            body["messages"] = [{"role": "system", "content": gm.system_prompt}] + list(body.get("messages") or [])
        if gm.max_tokens_cap:
            have = False
            for k in ("max_tokens", "max_completion_tokens"):
                if body.get(k):
                    have = True
                    body[k] = min(int(body[k]), gm.max_tokens_cap)
            if not have:
                body["max_tokens"] = gm.max_tokens_cap

    prompt_est, out_est = estimate_tokens(route, body)
    try:
        token = limiter.admit(key, prompt_est + out_est)
    except LimitError as e:
        return err(e.status, e.message, e.code, {"Retry-After": str(e.retry_after)})
    resp = None
    try:
        if gw.kind == "council":
            from . import council
            resp = await council.run(gw, gm, key, route, body, headers_in, token)
        else:
            resp = await _route(gw, gm, key, route, body, form_files, headers_in, token, prompt_est, out_est)
        return resp
    finally:
        if not (resp is not None and getattr(resp, "_sg_stream", False)):
            token.release()


async def _route(gw, gm, key, route, body, form_files, headers_in, token, prompt_est, out_est):
    hard, soft = detect_requirements(route, body, headers_in.get("x-sg-require", ""))
    all_members = load_members(gm.id)
    capable = [c for c in all_members if hard <= c.caps]
    if not capable:
        have = sorted(set().union(*[c.caps for c in all_members])) if all_members else []
        return err(400, f"No member of '{gm.public_name}' supports required capabilities {sorted(hard)} "
                        f"(model offers: {have})", "capability_unavailable")
    for sft in sorted(soft):
        pref = [c for c in capable if sft in c.caps]
        if pref:
            capable = pref
    strategy = gm.strategy or gw.strategy
    is_stream = bool(body.get("stream")) and route in CHAT_ROUTES
    rid = uuid.uuid4().hex[:10]
    t0 = time.time()
    tried: set = set()
    req_label = ",".join(sorted(hard - {"chat"}) + sorted("~" + x for x in soft))
    base_evt = {"id": rid, "gateway_id": gw.id, "public_model": gm.public_name, "route": route,
                "requirements": req_label, "stream": is_stream}
    key_id = key.id if key else None

    # analysis of the ORIGINAL request (alignment, context composition) -> attached to every log row of this request
    analysis = analyze_request(route, body, headers_in)
    comp_events: list = []

    def L(**kw):
        return RequestLog(**{**analysis, **kw})

    async def emit(rec):
        await log_request(rec, comp_events)

    # guardrails, input side (PII / injection / content filters). Runs before the cache so redacted text is what gets keyed and sent.
    gcfg = guardrails.safe_normalize(gm.guardrails)
    grules = library.resolve(gcfg) if gcfg and gcfg["filters"]["mode"] == "on" else []   # content filters picked from the library
    gr_buffered = guardrails.buffered(gcfg, grules)
    if gcfg:
        gres = guardrails.apply_input(route, body, gcfg, grules)
        if gres.action:
            analysis["guardrail_action"] = gres.action
            analysis["guardrail_findings"] = gres.findings or None
        if gres.action == "blocked":
            events.publish({"type": "end", "ok": False, "status": 400, "error": "blocked by guardrails", **base_evt})
            await emit(L(gateway_id=gw.id, key_id=key_id, public_model=gm.public_name, route=route, requirements=req_label,
                         status=400, ok=False, outcome="blocked", attempts=0, total_ms=int((time.time() - t0) * 1000),
                         error="blocked by guardrails"))
            return err(400, gres.reason, "guardrail_blocked")
        if gres.body is not body:
            body = gres.body
            prompt_est, out_est = estimate_tokens(route, body)

    # response cache (exact match; streams too if the model opted in; configured per public model). Key uses the original body.
    ckey = None
    plan = cache_plan(gm, route, body, headers_in.get("x-sg-cache", ""), is_stream)
    cache_state = plan if plan in ("bypass", "skip") else None
    if plan in ("use", "bypass", "skip"):
        analysis["cache_state"] = "miss" if plan == "use" else plan  # a hit overrides this below
    if plan == "use":
        gfp = guardrails.fingerprint(gcfg, grules)
        ckey = cache.make_key(gw.id, route, {**body, "model": gm.public_name, **({"_sg_guardrails": gfp} if gfp else {}),
                                                  **({"_sg_key": key_id if key_id is not None else "none"} if getattr(gm, "cache_per_key", False) else {})})
        hit = await cache.cache.get(ckey)
        if hit is not None and is_stream and len(hit.get("choices") or []) != 1:
            hit = None  # cannot replay a multi-choice entry as a stream: serve live
        if hit is not None:
            clean, meta = split_meta(hit)
            u = clean.get("usage") or {}
            if is_stream:
                speed = gm.cache_replay_speed or "instant"

                async def replay():
                    t_first = None
                    try:
                        async for line in synthesize_stream(clean, speed, meta):
                            if t_first is None:
                                t_first = int((time.time() - t0) * 1000)
                            yield line
                    finally:
                        total = int((time.time() - t0) * 1000)
                        token.release((int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)) or None)
                        await asyncio.shield(emit(L(gateway_id=gw.id, key_id=key_id, public_model=gm.public_name, route=route,
                                                    requirements=req_label, status=200, ok=True, cached=True, cache_state="hit", stream=True, ttft_ms=t_first,
                                                    prompt_tokens=int(u.get("prompt_tokens") or 0),
                                                    completion_tokens=int(u.get("completion_tokens") or 0), total_ms=total)))
                        events.publish({"type": "end", "ok": True, "status": 200, "cached": True, "total_ms": total, **base_evt})

                sr = StreamingResponse(replay(), media_type="text/event-stream", headers={"X-SG-Cache": "hit", "X-SG-Replay": speed})
                sr._sg_stream = True  # the generator releases the limiter token
                return sr
            await emit(L(gateway_id=gw.id, key_id=key_id, public_model=gm.public_name, route=route,
                         requirements=req_label, status=200, ok=True, cached=True, cache_state="hit",
                         prompt_tokens=int(u.get("prompt_tokens") or 0),
                         completion_tokens=int(u.get("completion_tokens") or 0),
                         total_ms=int((time.time() - t0) * 1000)))
            events.publish({"type": "end", "ok": True, "status": 200, "cached": True, "total_ms": 0, **base_evt})
            return JSONResponse(clean, headers={"X-SG-Cache": "hit"})

    # context compression of older tool results (off | shadow | on; per-model override of the global setting)
    compressed_tokens = 0
    cmode = gm.compress_mode or settings_store.get_cached().get("compress_mode", "off")
    if headers_in.get("x-sg-compress", "").lower() == "off":
        cmode = "off"  # a client may always opt out of lossy compression
    if route == "chat/completions" and cmode in ("on", "shadow") and isinstance(body.get("messages"), list):
        router_ = comp_pipeline.get_router(settings_store.get_cached().get("compress_config"))
        msgs = body["messages"]
        if comp_pipeline.tool_chars(msgs) > comp_pipeline.HEAVY_CHARS:
            cres = await asyncio.to_thread(comp_pipeline.apply_compression, msgs, router_, cmode)
        else:
            cres = comp_pipeline.apply_compression(msgs, router_, cmode)
        comp_events.extend(cres.events)
        analysis["compress_mode"] = cmode
        analysis["tokens_saved"] = cres.tokens_saved
        if cres.applied and cres.mutated:
            body = {**body, "messages": cres.messages}
            prompt_est, out_est = estimate_tokens(route, body)  # context-fit now sees the smaller prompt
            compressed_tokens = cres.tokens_saved

    need_ctx = prompt_est + out_est
    if route in CHAT_ROUTES or route == "embeddings":
        fits = [c for c in capable if not c.context_length or need_ctx <= c.context_length]
        if not fits:
            biggest = max((c.context_length or 0) for c in capable)
            return err(400, f"This request needs about {need_ctx} tokens but the largest context window available on "
                            f"'{gm.public_name}' is {biggest} tokens", "context_length_exceeded")
        capable = fits

    # routing hints: sticky affinity, tiered strategy
    hint: dict = {}
    skey = None
    if gm.sticky:
        skey = affinity.session_key(headers_in.get("x-sg-session", ""), body)
        pm = affinity.get(gm.id, skey)
        if pm is not None:
            hint["prefer_member"] = pm
    if strategy == "tiered":
        hint["tier"] = required_tier(prompt_est, body, soft)

    tq = time.time()

    def candidates():
        base = [c for c in capable if c.member_id not in tried and health.status.get(c.endpoint_id) != "offline"]
        fb = [c for c in base if c.is_fallback]
        if not fb:
            return base
        prim = [c for c in base if not c.is_fallback]
        usable = [c for c in prim if scheduler.breaker.state(c.model_id) != "open"]
        if not usable or time.time() - tq > config.FALLBACK_GRACE:
            return base
        return prim

    last_error = "no available upstream"
    overflow_body: Optional[str] = None
    attempts = 0
    queue_ms = 0
    buffered_out = gr_buffered and route == "chat/completions"
    up_stream = is_stream and not buffered_out   # buffered output checks need the complete answer: the upstream call is not streamed
    out_retries = 0
    retry_msgs: list = []
    pre_raw: Optional[bytes] = None
    gr_failed = False
    while True:
        tq = time.time()
        try:
            c = await scheduler.acquire(
                gw.id, strategy, hint, candidates, gw.queue_timeout,
                on_queued=lambda: events.publish({"type": "queued", **base_evt}),
            )
        except NoCandidates:
            if overflow_body is not None:
                events.publish({"type": "end", "ok": False, "status": 400, **base_evt})
                await emit(L(gateway_id=gw.id, key_id=key_id, public_model=gm.public_name, route=route,
                                             requirements=req_label, status=400, ok=False, attempts=attempts,
                                             total_ms=int((time.time() - t0) * 1000), error="context overflow on all members"))
                return Response(overflow_body, status_code=400, media_type="application/json")
            events.publish({"type": "end", "ok": False, "status": 502, **base_evt})
            await emit(L(gateway_id=gw.id, key_id=key_id, public_model=gm.public_name, route=route,
                                         requirements=req_label, status=502, ok=False, attempts=attempts,
                                         total_ms=int((time.time() - t0) * 1000), error=last_error))
            return err(502, public_error(last_error) if not verbose.get() else f"All upstreams unavailable: {last_error}", "upstream_error")
        except NoCapacity:
            events.publish({"type": "end", "ok": False, "status": 429, **base_evt})
            await emit(L(gateway_id=gw.id, key_id=key_id, public_model=gm.public_name, route=route,
                                         requirements=req_label, status=429, ok=False, attempts=attempts,
                                         queue_ms=int((time.time() - tq) * 1000),
                                         total_ms=int((time.time() - t0) * 1000), error="queue timeout"))
            return err(429, "All upstream slots busy; queue timeout", "rate_limit_exceeded",
                       {"Retry-After": "5"})
        queue_ms += int((time.time() - tq) * 1000)
        attempts += 1
        events.publish({"type": "start", "model_id": c.model_id, "endpoint_id": c.endpoint_id,
                        "upstream": c.upstream_id, "queue_ms": queue_ms, "attempt": attempts, **base_evt})

        fwd = dict(body)
        fwd["model"] = c.upstream_id
        if retry_msgs and isinstance(body.get("messages"), list):
            fwd["messages"] = [*body["messages"], *retry_msgs]
        if buffered_out:
            fwd["stream"] = False
            fwd.pop("stream_options", None)
        elif is_stream and "stream_options" not in fwd:
            fwd["stream_options"] = {"include_usage": True}
        headers = {"Authorization": f"Bearer {c.api_key}"} if c.api_key else {}
        if c.provider:
            headers.update(provider_headers(c.provider, headers_in, body))
        url = f"{c.base_url}/{route}"
        t_send = time.time()
        try:
            if form_files is not None:
                req = client.build_request("POST", url, data={k: str(v) for k, v in fwd.items()},
                                           files=form_files, headers=headers)
            else:
                headers["Content-Type"] = "application/json"
                req = client.build_request("POST", url, content=json.dumps(fwd), headers=headers)
            resp = await client.send(req, stream=True)
        except asyncio.CancelledError:
            scheduler.release(c, ok=True)
            raise
        except Exception as e:  # noqa: BLE001 - connect/timeouts
            last_error = f"{c.endpoint_name}: {type(e).__name__}: {str(e)[:120]}"
            scheduler.fail(c)
            scheduler.release(c, ok=False)
            tried.add(c.member_id)
            events.publish({"type": "retry", "model_id": c.model_id, "endpoint_id": c.endpoint_id,
                            "error": last_error, **base_evt})
            if attempts > gw.max_retries:
                return await _finish_error(gw, gm, key_id, route, req_label, 502, last_error, attempts, t0, queue_ms, base_evt, L, emit)
            continue

        if resp.status_code >= 500 or resp.status_code == 429:
            text = (await resp.aread()).decode("utf-8", "replace")[:200]
            await resp.aclose()
            last_error = f"{c.endpoint_name}: HTTP {resp.status_code} {text}"
            if resp.status_code >= 500:
                scheduler.fail(c)
            scheduler.release(c, ok=False)
            tried.add(c.member_id)
            events.publish({"type": "retry", "model_id": c.model_id, "endpoint_id": c.endpoint_id,
                            "error": last_error, **base_evt})
            if attempts > gw.max_retries:
                return await _finish_error(gw, gm, key_id, route, req_label, 502, last_error, attempts, t0, queue_ms, base_evt, L, emit)
            continue
        if resp.status_code in (400, 413):
            raw_err = await resp.aread()
            if OVERFLOW_RX.search(raw_err.decode("utf-8", "replace")[:600]):
                await resp.aclose()
                overflow_body = raw_err.decode("utf-8", "replace")
                last_error = f"{c.endpoint_name}: context window exceeded"
                scheduler.release(c, ok=True)  # not an upstream fault
                tried.add(c.member_id)
                events.publish({"type": "retry", "model_id": c.model_id, "endpoint_id": c.endpoint_id,
                                "error": last_error, **base_evt})
                continue
        if buffered_out and resp.status_code == 200:
            try:
                raw0 = await resp.aread()
            except Exception as e:  # noqa: BLE001
                await resp.aclose()
                last_error = f"{c.endpoint_name}: read error: {e}"
                scheduler.fail(c)
                scheduler.release(c, ok=False)
                tried.add(c.member_id)
                if attempts > gw.max_retries:
                    return await _finish_error(gw, gm, key_id, route, req_label, 502, last_error, attempts, t0, queue_ms, base_evt, L, emit)
                continue
            await resp.aclose()
            try:
                data0 = json.loads(raw0)
            except ValueError:
                data0 = None
            if isinstance(data0, dict):
                ores = guardrails.check_output(data0, gcfg, body, grules)
                if ores.findings:
                    analysis["guardrail_findings"] = {**(analysis.get("guardrail_findings") or {}), **ores.findings}
                if ores.blocked or (not ores.ok and out_retries >= gcfg["output"]["max_retries"] and gcfg["output"]["on_fail"] == "error"):
                    blocked = ores.blocked
                    analysis["guardrail_action"] = guardrails.worst(analysis.get("guardrail_action"), "blocked" if blocked else "failed")
                    if out_retries:
                        analysis["output_retries"] = out_retries
                    scheduler.release(c, ok=True)
                    msg_ = "Response blocked by guardrails: " + ores.error if blocked else \
                        f"The model's output failed validation after {out_retries} retr{'y' if out_retries == 1 else 'ies'}: {ores.error}"
                    events.publish({"type": "end", "ok": False, "status": 422, "error": msg_[:200], **base_evt})
                    await emit(L(gateway_id=gw.id, key_id=key_id, public_model=gm.public_name, endpoint_id=c.endpoint_id,
                                 model_id=c.model_id, upstream_model=c.upstream_id, route=route, requirements=req_label,
                                 status=422, ok=False, outcome="blocked" if blocked else "invalid_output", attempts=attempts,
                                 queue_ms=queue_ms, total_ms=int((time.time() - t0) * 1000), error=msg_[:300]))
                    return err(422, msg_, "guardrail_blocked" if blocked else "output_validation_failed")
                if not ores.ok:
                    if out_retries < gcfg["output"]["max_retries"]:   # ask again (same model, optionally telling it what was wrong)
                        out_retries += 1
                        scheduler.release(c, ok=True)
                        retry_msgs = []
                        if gcfg["output"]["feedback"]:
                            prev = ((data0.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                            retry_msgs = [{"role": "assistant", "content": str(prev)[:4000]},
                                          {"role": "user", "content": f"Your previous reply was rejected: {ores.error}. Reply again with a corrected answer only."}]
                        events.publish({"type": "retry", "model_id": c.model_id, "endpoint_id": c.endpoint_id,
                                        "error": "output validation: " + ores.error[:120], **base_evt})
                        continue
                    gr_failed = True   # on_fail == passthrough: deliver the answer, flagged
                analysis["guardrail_action"] = guardrails.worst(
                    analysis.get("guardrail_action"),
                    "failed" if gr_failed else "retried" if out_retries else ores.action)
                if out_retries:
                    analysis["output_retries"] = out_retries
                pre_raw = json.dumps(ores.data).encode()
            else:
                pre_raw = raw0
        break

    if skey is not None:
        affinity.put(gm.id, skey, c.member_id)
    out_headers = up_headers(resp.headers)
    if gr_failed:
        out_headers["X-SG-Guardrail"] = "output-invalid"
    out_headers.update({"X-SG-Request-Id": rid,
                        **({"X-SG-Upstream": c.upstream_id, "X-SG-Endpoint": c.endpoint_name, "X-SG-Queue-Ms": str(queue_ms), "X-SG-Attempts": str(attempts)} if verbose.get() else {}),
                        **({"X-SG-Compressed": str(compressed_tokens)} if compressed_tokens else {}),
                        "Access-Control-Expose-Headers": "X-SG-Request-Id,X-SG-Upstream,X-SG-Endpoint,X-SG-Queue-Ms,X-SG-Attempts,X-SG-Cache,X-SG-Compressed"})
    rec = L(gateway_id=gw.id, key_id=key_id, public_model=gm.public_name, endpoint_id=c.endpoint_id,
                     model_id=c.model_id, upstream_model=c.upstream_id, route=route,
                     requirements=req_label, stream=is_stream, queue_ms=queue_ms, attempts=attempts,
                     status=resp.status_code)

    async def finalize(ok: bool, err_text: Optional[str] = None):
        rec.total_ms = int((time.time() - t0) * 1000)
        rec.ok = ok and resp.status_code < 400
        rec.error = err_text or (None if rec.ok else f"HTTP {resp.status_code}")
        scheduler.release(c, ok=ok, latency=(rec.ttft_ms or rec.total_ms) / 1000.0 if ok else None)
        token.release((rec.prompt_tokens + rec.completion_tokens) or None)
        events.publish({"type": "end", "ok": rec.ok, "status": rec.status, "model_id": c.model_id,
                        "endpoint_id": c.endpoint_id, "total_ms": rec.total_ms, "ttft_ms": rec.ttft_ms,
                        "prompt_tokens": rec.prompt_tokens, "completion_tokens": rec.completion_tokens,
                        "error": rec.error, **base_evt})
        await emit(rec)

    if not up_stream or resp.status_code >= 400:
        try:
            raw = pre_raw if pre_raw is not None else await resp.aread()
        except Exception as e:  # noqa: BLE001
            await resp.aclose()
            await finalize(False, f"read error: {e}")
            return err(502, f"Upstream read error: {e}" if verbose.get() else public_error("", 1), "upstream_error")
        await resp.aclose()
        rec.ttft_ms = int((time.time() - t_send) * 1000)
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype or raw[:1] in (b"{", b"["):
            try:
                data = json.loads(raw)
                usage = data.get("usage") or {} if isinstance(data, dict) else {}
                rec.prompt_tokens = int(usage.get("prompt_tokens") or 0)
                rec.completion_tokens = int(usage.get("completion_tokens") or 0)
                if resp.status_code == 200 and isinstance(data, dict) and route in CHAT_ROUTES:
                    ch0 = (data.get("choices") or [{}])[0] or {}
                    msg = ch0.get("message") or {}
                    rec.finish_reason = ch0.get("finish_reason")
                    rec.outcome = classify_outcome(rec.finish_reason, len(msg.get("content") or ""), len(msg.get("reasoning_content") or msg.get("reasoning") or ""), bool(msg.get("tool_calls")))
                rewrite_model(data, gm.public_name)
                entry = None
                if ckey and resp.status_code == 200:  # read rec BEFORE finalize(): committing the log row expires its attributes
                    entry = {**data, META_KEY: {"gen_ms": max(50, int((time.time() - t_send) * 1000)), "origin": "json"}} if isinstance(data, dict) else data
                await finalize(True)
                if entry is not None:
                    await cache.cache.put(ckey, entry, gm.cache_ttl)
                    out_headers["X-SG-Cache"] = "miss"
                elif cache_state:
                    out_headers["X-SG-Cache"] = cache_state
                if is_stream and buffered_out and resp.status_code == 200 and isinstance(data, dict) and len(data.get("choices") or []) == 1:
                    # the client asked for a stream but the answer had to be checked first: replay the checked answer as SSE
                    sse_headers = {k: v for k, v in out_headers.items() if k.lower() != "content-type"}
                    return StreamingResponse(synthesize_stream(data, "instant"), media_type="text/event-stream", headers=sse_headers)
                return JSONResponse(data, status_code=resp.status_code, headers=out_headers)
            except Exception:  # noqa: BLE001 non-json upstream body
                pass
        await finalize(True)
        return Response(raw, status_code=resp.status_code, headers=out_headers, media_type=ctype or None)

    agg = StreamAggregator(int(cache.cache.settings.get("cache_max_entry_kb", 512)) * 1024) if (ckey and up_stream) else None

    async def gen():
        ok, err_text, chunks, completed = True, None, 0, False
        finish, n_content, n_reason, has_tools, aborted = None, 0, 0, False, False
        try:
            async for line in resp.aiter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    payload = line[5:].strip()
                    try:
                        obj = json.loads(payload)
                        if agg is not None:
                            agg.feed(obj)
                        ch0 = (obj.get("choices") or [{}])[0] if obj.get("choices") else {}
                        delta = ch0.get("delta") or {}
                        if rec.ttft_ms is None and (delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")):
                            rec.ttft_ms = int((time.time() - t_send) * 1000)
                            events.publish({"type": "first_token", "model_id": c.model_id, **base_evt})
                        if delta.get("content"):
                            chunks += 1
                            n_content += len(delta["content"])
                        n_reason += len(delta.get("reasoning_content") or delta.get("reasoning") or "")
                        has_tools = has_tools or bool(delta.get("tool_calls"))
                        finish = ch0.get("finish_reason") or finish
                        if obj.get("usage"):
                            rec.prompt_tokens = int(obj["usage"].get("prompt_tokens") or 0)
                            rec.completion_tokens = int(obj["usage"].get("completion_tokens") or 0)
                        rewrite_model(obj, gm.public_name)
                        line = "data: " + json.dumps(obj, separators=(",", ":"))
                    except Exception:  # noqa: BLE001 pass through unparsed
                        pass
                yield line + "\n"
            completed = True  # upstream closed the stream normally (not an error, not a client disconnect)
        except (asyncio.CancelledError, GeneratorExit):
            ok, err_text, aborted = True, "client disconnected", True
            raise
        except Exception as e:  # noqa: BLE001
            ok, err_text = False, f"stream error: {type(e).__name__}: {e}"
            scheduler.fail(c)
        finally:
            if not rec.completion_tokens:
                rec.completion_tokens = chunks
            try:
                await asyncio.shield(resp.aclose())
            except BaseException:  # noqa: BLE001
                pass
            entry = None
            if agg is not None and completed and ok and agg.complete:  # only complete, clean streams are stored
                try:  # build the entry BEFORE finalize(): committing the log row expires rec's attributes
                    gen_ms = max(50, int(time.time() * 1000 - t_send * 1000 - (rec.ttft_ms or 0)))
                    entry = agg.result(gm.public_name, {"prompt_tokens": rec.prompt_tokens, "completion_tokens": rec.completion_tokens,
                                                        "total_tokens": rec.prompt_tokens + rec.completion_tokens})
                    entry[META_KEY] = {"ttft_ms": rec.ttft_ms, "gen_ms": gen_ms, "origin": "stream"}
                except Exception:  # noqa: BLE001
                    log.exception("could not build the cache entry for a streamed response")
            if resp.status_code == 200 and route in CHAT_ROUTES:
                rec.finish_reason = finish
                rec.outcome = classify_outcome(finish, n_content, n_reason, has_tools, completed=completed, client_abort=aborted, error=not ok and not aborted)
            await asyncio.shield(finalize(ok, err_text))
            if entry is not None:
                _spawn(cache.cache.put(ckey, entry, gm.cache_ttl))

    if is_stream:
        if ckey:
            out_headers["X-SG-Cache"] = "miss"
        elif cache_state:
            out_headers["X-SG-Cache"] = cache_state
    sr = StreamingResponse(gen(), status_code=resp.status_code, headers=out_headers,
                           media_type=resp.headers.get("content-type", "text/event-stream"))
    sr._sg_stream = True  # the generator releases the limiter token itself
    return sr



def classify_outcome(finish: Optional[str], content: int, reasoning: int, tools: bool, *, completed: bool = True, client_abort: bool = False, error: bool = False) -> str:
    """How a model response ended, for the request log. content/reasoning = characters of answer / reasoning text seen."""
    if finish is None:
        if client_abort:
            return "client_aborted"
        if error:
            return "stream_error"
        return "truncated" if completed and (content or reasoning or tools) else "empty"   # upstream closed the stream without a finish reason
    if finish == "length":
        return "cut_by_limit" if content or tools else "cut_in_reasoning"   # token limit hit: mid-answer / still thinking
    if finish == "content_filter":
        return "filtered"
    if not content and not tools:
        return "no_answer" if reasoning else "empty"   # finished, but produced no answer text
    return "complete"


async def _finish_error(gw, gm, key_id, route, req_label, status, error, attempts, t0, queue_ms, base_evt, L=None, emit=None):
    events.publish({"type": "end", "ok": False, "status": status, "error": error, **base_evt})
    await (emit or log_request)((L or RequestLog)(gateway_id=gw.id, key_id=key_id, public_model=gm.public_name, route=route,
                                 requirements=req_label, status=status, ok=False, attempts=attempts,
                                 queue_ms=queue_ms, total_ms=int((time.time() - t0) * 1000), error=error))
    return err(status, public_error(error, attempts), "upstream_error")


# ---------------------------------------------------------------- HTTP entry points
async def parse_body(request: Request):
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        body, files = {}, []
        for k, v in form.multi_items():
            if hasattr(v, "read"):
                files.append((k, (v.filename, await v.read(), v.content_type)))
            else:
                body[k] = v
        return body, files, None
    try:
        body = await request.json()
        assert isinstance(body, dict)
        return body, None, None
    except Exception:  # noqa: BLE001
        return None, None, err(400, "Request body must be a JSON object", "invalid_request_error")


def _rid() -> str:
    return "req_" + uuid.uuid4().hex[:24]


async def adapt(resp, mode: str, public_model_hint: str, req_body: dict, strict: bool = False, input_tokens: int = 0):
    """Convert a chat-completions Response into Anthropic Messages / Responses API shape.
    strict = Anthropic client behaviour (Messages mode only; always on for /messages)."""
    mod = anth if mode == "anthropic" else resp_api
    strict = strict and mode == "anthropic"
    rid = _rid()
    if isinstance(resp, StreamingResponse) and resp.status_code < 400:
        async def lines():
            async for chunk in resp.body_iterator:
                for ln in (chunk.decode() if isinstance(chunk, bytes) else chunk).split("\n"):
                    yield ln
        gen = anth.stream_from_chat(lines(), public_model_hint, strict, input_tokens) if strict else (mod.stream_from_chat(lines(), public_model_hint) if mode == "anthropic" else mod.stream_from_chat(lines(), public_model_hint, req_body))
        out = StreamingResponse(gen, media_type="text/event-stream",
                                headers={**{k: v for k, v in resp.headers.items() if k.lower().startswith("x-sg-")}, **({"request-id": rid} if strict else {})})
        out._sg_stream = True
        return out
    raw = resp.body if hasattr(resp, "body") else b""
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        data = {}
    hdrs = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-sg-")}
    if strict:
        hdrs["request-id"] = rid
    if resp.status_code >= 400:
        e = data.get("error") or {}
        if strict:
            return JSONResponse(anth.error_body(e.get("message") or "error", e.get("type") or "api_error", resp.status_code, rid),
                                status_code=resp.status_code, headers=hdrs)
        return JSONResponse(mod.error_body(e.get("message") or "error", e.get("type") or "api_error"),
                            status_code=resp.status_code, headers=hdrs)
    if mode == "anthropic":
        return JSONResponse(anth.from_chat(data, public_model_hint, strict), headers=hdrs)
    return JSONResponse(resp_api.from_chat(data, public_model_hint, req_body), headers=hdrs)


async def handle(gw: Gateway, key, path: str, request: Request):
    route = path.strip("/")
    if is_anthropic(request, route):
        return await _strict(await _handle(gw, key, route, request, True), request)
    return await _handle(gw, key, route, request, False)


def is_anthropic(request: Request, route: str = "") -> bool:
    """Every gateway speaks both APIs. A request is treated as Anthropic when it targets a Messages route, carries the
    `anthropic-version` header the SDKs always send, or authenticates with x-api-key alone."""
    h = request.headers
    return (route.strip("/").startswith("messages") or "anthropic-version" in h
            or ("x-api-key" in h and "authorization" not in h))


async def _handle(gw: Gateway, key, route: str, request: Request, strict: bool = False):
    if route == "models" and request.method == "GET":
        if strict:
            return anthropic_models(public_models(gw, key), request)
        return {"object": "list", "data": public_models(gw, key)}
    if strict and request.method == "GET" and route.startswith("models/"):
        return anthropic_model(public_models(gw, key), route[7:])
    if route == "audio/voices" and request.method == "GET":
        return await audio_voices(gw, key, request.query_params.get("model", "").strip())
    if request.method != "POST":
        return err(404, f"Unsupported route: {request.method} /v1/{route}", "not_found")
    body, files, e = await parse_body(request)
    if e:
        return e
    return await handle_parsed(gw, key, route, body, files, request)


def _anth_model(m: dict) -> dict:
    return {"type": "model", "id": m["id"], "display_name": m["id"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(m.get("created") or 0))}


def anthropic_models(models: list[dict], request: Request) -> dict:
    """Anthropic GET /v1/models: {data:[{type:"model",...}], has_more, first_id, last_id}, with after_id / before_id / limit paging."""
    items = [_anth_model(m) for m in models]
    q = request.query_params
    try:
        limit = max(1, min(int(q.get("limit", 20)), 1000))
    except ValueError:
        limit = 20
    ids = [m["id"] for m in items]
    if q.get("after_id") in ids:
        items = items[ids.index(q["after_id"]) + 1:]
    elif q.get("before_id") in ids:
        items = items[:ids.index(q["before_id"])][-limit:]
    page = items[:limit]
    return {"data": page, "has_more": len(items) > limit, "first_id": page[0]["id"] if page else None, "last_id": page[-1]["id"] if page else None}


def anthropic_model(models: list[dict], mid: str):
    for m in models:
        if m["id"] == mid:
            return _anth_model(m)
    return JSONResponse(anth.error_body(f"model: {mid}", "not_found", 404), status_code=404)


async def _strict(r, request: Request):
    """Anthropic clients get every error in Anthropic's shape, whatever the route, and a request-id."""
    if isinstance(r, dict):
        return JSONResponse(r, headers={"request-id": _rid()})
    if isinstance(r, JSONResponse) and r.status_code >= 400:
        try:
            j = json.loads(r.body)
        except Exception:  # noqa: BLE001
            return r
        if j.get("type") == "error":  # already Anthropic-shaped (from adapt)
            return r
        e = j.get("error") or {}
        rid = _rid()
        return JSONResponse(anth.error_body(e.get("message") or "error", e.get("type") or "api_error", r.status_code, rid),
                            status_code=r.status_code, headers={"request-id": rid})
    if isinstance(r, JSONResponse) and "request-id" not in r.headers:
        r.headers["request-id"] = _rid()
    return r


async def handle_parsed(gw: Gateway, key, route: str, body: dict, files, request: Request, alias: Optional[str] = None):
    """Serve one parsed POST. `alias` = the model name the client used on the unified endpoint (echoed back in responses)."""
    if route in ("messages", "responses"):
        mode = "anthropic" if route == "messages" else "responses"
        strict = mode == "anthropic"
        chat_body = anth.to_chat(body, strict) if mode == "anthropic" else resp_api.to_chat(body)
        hint = alias or body.get("model") or ""
        r = await dispatch(gw, key, "chat/completions", chat_body, None, request.headers)
        return await adapt(r, mode, hint, body, strict, estimate_tokens("chat/completions", chat_body)[0] if strict else 0)
    if route == "messages/count_tokens":
        pt, _ = estimate_tokens("chat/completions", anth.to_chat(body, True))
        return {"input_tokens": pt}
    if route not in SUPPORTED:
        return err(404, f"Unsupported route: POST /v1/{route}", "not_found")
    r = await dispatch(gw, key, route, body, files, request.headers)
    return alias_response(r, alias) if alias else r


def alias_response(resp, alias: str):
    """Report the unified-endpoint model name (the gateway name) instead of the gateway's internal public model name."""
    def fix(obj):
        if isinstance(obj, dict) and "model" in obj:
            obj["model"] = alias
        return obj

    if isinstance(resp, StreamingResponse) and resp.status_code < 400:
        async def gen():
            it = resp.body_iterator
            try:
                async for chunk in it:
                    text = chunk.decode() if isinstance(chunk, bytes) else chunk
                    out = []
                    for ln in text.split("\n"):
                        if ln.startswith("data: ") and ln[6:].strip() not in ("", "[DONE]"):
                            try:
                                ln = "data: " + json.dumps(fix(json.loads(ln[6:])), separators=(",", ":"))
                            except Exception:  # noqa: BLE001 pass through unparsed
                                pass
                        out.append(ln)
                    yield "\n".join(out)
            finally:  # a client disconnect must reach the upstream stream so it is released and accounted for
                aclose = getattr(it, "aclose", None)
                if aclose:
                    await aclose()
        return StreamingResponse(gen(), status_code=resp.status_code, media_type=resp.media_type,
                                 headers={k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-type")})
    if isinstance(resp, JSONResponse) and resp.status_code < 400:
        try:
            data = json.loads(resp.body)
        except Exception:  # noqa: BLE001
            return resp
        return JSONResponse(fix(data), status_code=resp.status_code,
                            headers={k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-type")})
    return resp


@router.api_route("/gw/{slug}/v1/{path:path}", methods=["GET", "POST"])
async def gw_route(slug: str, path: str, request: Request):
    gw, key, e = authenticate(slug, request)
    if e:
        return await _compat_error(e, path, request)
    return await handle(gw, key, path, request)


@router.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def v1_route(path: str, request: Request):
    gw, key, e = authenticate(None, request)
    if e:
        return await _compat_error(e, path, request)
    return await handle(gw, key, path, request)


async def _compat_error(e: JSONResponse, path: str, request: Optional[Request] = None):
    if request is not None and is_anthropic(request, path):
        return await _strict(e, request)
    if path.strip("/").startswith("messages"):
        j = json.loads(e.body)["error"]
        return JSONResponse(anth.error_body(j["message"], j["type"]), status_code=e.status_code)
    return e


@router.api_route("/api/admin-gw/{slug}/v1/{path:path}", methods=["GET", "POST"])
async def admin_gw_route(slug: str, path: str, request: Request):
    """Used by the admin UI (Test dialog, Playground): same pipeline, no API key, protected by admin auth."""
    verbose.set(True)   # admin-only route: show routing headers and full upstream errors
    with session() as s:
        gw = s.exec(select(Gateway).where(Gateway.slug == slug)).first()
    if not gw or gw.slug == "__council__":
        return err(404, f"Gateway '{slug}' not found", "not_found")
    return await handle(gw, None, path, request)


# ---------------------------------------------------------------- unified endpoint (many gateways, one API)
def _unified_models(aliases: list[dict], request: Request, mode: Optional[str] = None) -> list[dict]:
    """The models this caller may use: with a unified key every exposed gateway, otherwise open gateways plus the gateway the supplied key belongs to."""
    mode = mode or unified.access(_token_from(request))
    strict = bool(unified.keys())
    out, cache = [], {}
    for a in aliases:
        gw = a["gateway"]
        if mode == "all":
            g, key = gw, None
            if gw.id not in cache:
                cache[gw.id] = {m["id"]: m for m in public_models(gw, None)}
        elif gw.id not in cache:
            g, key, e = authenticate(gw.slug, request, allow_internal=True)
            if strict and key is None:   # unified keys are configured: a junk token must not unlock open gateways
                e = True
            cache[gw.id] = None if e else {m["id"]: m for m in public_models(g, key)}
        m = (cache[gw.id] or {}).get(a["model"])
        if m:
            out.append({**m, "id": a["alias"], "owned_by": f"smartgateway/{gw.slug}"})
    return out


def _unified_target(wanted: str, aliases, mode: str, request: Request, allow_default: bool = False):
    """(gateway, key, entry, error) for a model name sent to the unified endpoint."""
    entry = unified.resolve(wanted, aliases)
    if entry is None and not wanted and allow_default:
        mine = _unified_models(aliases, request, mode)
        entry = unified.resolve(mine[0]["id"], aliases) if len(mine) == 1 else None
    if entry is None:
        avail = ", ".join(m["id"] for m in _unified_models(aliases, request, mode)) or "none"
        return None, None, None, err(404, f"Model '{wanted}' not found. Available: {avail}", "model_not_found")
    if mode == "all":
        return entry["gateway"], None, entry, None   # the unified key replaces the gateway's own key (and its limits)
    gw, key, e = authenticate(entry["gateway"].slug, request, allow_internal=True)
    if not e and key is None and unified.keys():
        e = err(401, "Invalid API key", "invalid_api_key")
    return gw, key, entry, e


@router.api_route("/unified/v1/{path:path}", methods=["GET", "POST"])
async def unified_route(path: str, request: Request):
    if not settings_store.get_cached().get("unified_enabled"):
        return await _compat_error(err(404, "The unified endpoint is not enabled", "not_found"), path, request)
    route = path.strip("/")
    mode = unified.access(_token_from(request))
    if mode == "denied":
        return await _compat_error(err(401, "An API key is required for the unified endpoint", "invalid_api_key"), route, request)
    aliases = unified.exposed()
    if route == "models" and request.method == "GET":
        if is_anthropic(request, route):
            return await _strict(anthropic_models(_unified_models(aliases, request, mode), request), request)
        return {"object": "list", "data": _unified_models(aliases, request, mode)}
    if route == "audio/voices" and request.method == "GET":
        wanted = request.query_params.get("model", "").strip()
        if not wanted:   # clients such as Open WebUI send none: everything the caller may use
            return await unified_voices(aliases, mode, request)
        gw, key, entry, e = _unified_target(wanted, aliases, mode, request)
        if e:
            return await _compat_error(e, route, request)
        return await audio_voices(gw, key, entry["model"])
    if request.method != "POST":
        return err(404, f"Unsupported route: {request.method} /unified/v1/{route}", "not_found")
    body, files, e = await parse_body(request)
    if e:
        return e
    if route == "messages/count_tokens":
        return await handle_parsed(None, None, route, body, files, request)
    wanted = str(body.get("model") or "").strip()
    gw, key, entry, e = _unified_target(wanted, aliases, mode, request, allow_default=True)
    if e:
        return await _compat_error(e, route, request)
    body = {**body, "model": entry["model"]}
    return await handle_parsed(gw, key, route, body, files, request, alias=entry["alias"])
