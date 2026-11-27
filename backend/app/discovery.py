"""Model listing + capability detection for OpenAI-compatible endpoints."""
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import httpx

from . import config, crypto

PNG_1x1 = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

NAME_RULES = {
    "embedding": r"embed|bge-|\bbge\b|e5-|\be5\b|gte-|nomic|minilm|mxbai|arctic-embed",
    "vision": r"(^|[-_/:.])vl([-_/:.]|$)|vision|llava|pixtral|gemma-?3|minicpm-v|molmo|internvl|qwen.*vl|llama-?3\.2-.*(11|90)b|gpt-4o|gpt-4\.1|claude|gemini",
    "audio": r"whisper|audio|omni|voxtral|speech|tts",
    "ocr": r"ocr|paddle|dots\.ocr|nanonets|olmocr|got-ocr",
    "reasoning": r"(^|[-_/:.])r1([-_/:.]|$)|qwq|reason|(^|[-_/])o[134]([-_/]|$)|thinking|gpt-oss|magistral|deepseek-v3\.[12]|qwen3(?!.*instruct)|phi-?4-reasoning",
    "video": r"video|qwen.*omni|internvl",
    "tts": r"(^|[-_/:.])tts([-_/:.]|$)|kokoro|piper|xtts|bark|speecht5|orpheus",
    "image_gen": r"flux|sdxl|stable-diffusion|(^|[-_/])sd[-_.]?[135]|dall-?e|imagen|playground-v",
    "tools": r"hermes|functionary|firefunction|gorilla",
}


# Hosted catalogs whose /models is authoritative (capabilities, context, prices): never probed with paid calls.
PROVIDERS = {
    "opencode-zen": {"label": "OpenCode Zen", "base_url": "https://opencode.ai/inference/v1"},
    "opencode-go": {"label": "OpenCode Go", "base_url": "https://opencode.ai/inference/go/v1"},
}


def detect_provider(url: str) -> Optional[str]:
    """opencode.ai/inference[/go]/... (or the older /zen[/go]/...) -> "opencode-zen" | "opencode-go"; anything else -> None."""
    try:
        u = urlsplit((url or "").strip())
    except ValueError:
        return None
    if (u.hostname or "").lower() not in ("opencode.ai", "www.opencode.ai"):
        return None
    path = u.path.rstrip("/").lower()
    if path.startswith(("/inference/go", "/zen/go")):
        return "opencode-go"
    if path.startswith(("/inference", "/zen")):
        return "opencode-zen"
    return None


def caps_from_opencode(item: dict) -> set:
    """OpenCode's /models metadata: capabilities.{tools,reasoning}.supported, modalities.input (image/*, audio/*, video/*), type."""
    caps: set = set()
    c = item.get("capabilities")
    if isinstance(c, dict):
        for name in ("tools", "reasoning"):
            v = c.get(name)
            if (isinstance(v, dict) and v.get("supported")) or v is True:
                caps.add(name)
    mods = item.get("modalities")
    for m in ((mods or {}).get("input") or []) if isinstance(mods, dict) else []:
        m = str(m).lower()
        if m.startswith("image"):
            caps.add("vision")
        elif m.startswith("audio"):
            caps.add("audio")
        elif m.startswith("video"):
            caps.add("video")
    t = str(item.get("type") or "").lower()
    if t == "embedding":
        caps.add("embedding")
    elif t in ("chat", "text", ""):
        caps.add("chat")
    return caps


@dataclass
class DiscoveredModel:
    upstream_id: str
    capabilities: set = field(default_factory=set)
    sources: dict = field(default_factory=dict)
    max_slots: Optional[int] = None
    context_length: Optional[int] = None
    free: bool = False  # hosted catalogs: listed price is 0 (safe to use for a key check)
    cost: Optional[dict] = None  # hosted catalogs: the catalog's own price block (see pricing.py)


def normalize_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def root_url(base: str) -> str:
    return re.sub(r"/v\d+$", "", base.rstrip("/"))


def headers_for(api_key: Optional[str]) -> dict:
    h = {"Content-Type": "application/json"}
    if crypto.is_unreadable(api_key):
        raise crypto.SecretUnreadable("The stored API key cannot be decrypted; enter it again")
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def heuristics_from_name(name: str) -> set:
    n = name.lower()
    caps = set()
    for cap, rx in NAME_RULES.items():
        if re.search(rx, n):
            caps.add(cap)
    return caps


def caps_from_metadata(item: dict) -> set:
    caps: set = set()
    arch = item.get("architecture") or {}
    mods = set(map(str.lower, (arch.get("input_modalities") or item.get("input_modalities") or [])))
    if "image" in mods:
        caps.add("vision")
    if "audio" in mods:
        caps.add("audio")
    if "video" in mods:
        caps.add("video")
    for c in item.get("capabilities") or []:
        c = str(c).lower()
        if c in ("vision", "image"):
            caps.add("vision")
        elif c in ("thinking", "reasoning"):
            caps.add("reasoning")
        elif c == "embedding" or c == "embeddings":
            caps.add("embedding")
        elif c == "audio":
            caps.add("audio")
        elif c in ("completion", "chat"):
            caps.add("chat")
        elif c in ("tools", "tool_use", "function_calling"):
            caps.add("tools")
    params = item.get("supported_parameters") or []
    if "reasoning" in params or "include_reasoning" in params:
        caps.add("reasoning")
    if "tools" in params or "tool_choice" in params:
        caps.add("tools")
    return caps


def _int(v):
    try:
        v = int(v)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def extract_context(item: dict, props: Optional[dict] = None) -> Optional[int]:
    """Context window from the many places OpenAI-compatible servers report it.
    Prefers the configured/runtime context (n_ctx) over the model's trained maximum."""
    meta = item.get("meta") or {}
    status_args = (item.get("status") or {}).get("args") or []
    candidates = [
        meta.get("n_ctx"), item.get("n_ctx"), item.get("max_model_len"), item.get("context_length"),
        item.get("context_window"), item.get("max_context_length"), item.get("max_seq_len"),
        (item.get("top_provider") or {}).get("context_length"),
        meta.get("n_ctx_train"), meta.get("context_length"),
        (item.get("limit") or {}).get("context") if isinstance(item.get("limit"), dict) else None,   # OpenCode: the real window; limit.input only caps the prompt
        (item.get("limit") or {}).get("input") if isinstance(item.get("limit"), dict) else None,
    ]
    for i, a in enumerate(status_args):  # llama-server router: args like ["-c", "32768"] / "--ctx-size"
        if a in ("-c", "--ctx-size", "--context-size") and i + 1 < len(status_args):
            candidates.insert(1, status_args[i + 1])
    if props:
        candidates.insert(2, ((props.get("default_generation_settings") or {}).get("n_ctx")))
    for c in candidates:
        v = _int(c)
        if v:
            return v
    return None


async def fetch_models(client: httpx.AsyncClient, base: str, api_key: Optional[str]) -> list[dict]:
    r = await client.get(f"{base}/models", headers=headers_for(api_key))
    r.raise_for_status()
    data = r.json()
    items = data.get("data") if isinstance(data, dict) else data
    if items is None and isinstance(data, dict):
        items = data.get("models")
    if not isinstance(items, list):
        raise ValueError("Unexpected /models response shape")
    out = []
    for it in items:
        if isinstance(it, str):
            it = {"id": it}
        mid = it.get("id") or it.get("name") or it.get("model")
        if mid:
            it["id"] = mid
            out.append(it)
    return out


async def probe_props(client, base, api_key) -> dict:
    """llama.cpp-style /props (server root)."""
    try:
        r = await client.get(f"{root_url(base)}/props", headers=headers_for(api_key))
        if r.status_code == 200 and isinstance(r.json(), dict):
            return r.json()
    except Exception:
        pass
    return {}


async def probe_ollama_show(client, base, api_key, model) -> tuple[set, Optional[int]]:
    try:
        r = await client.post(f"{root_url(base)}/api/show", json={"model": model}, headers=headers_for(api_key))
        if r.status_code == 200:
            j = r.json()
            ctx = next((_int(v) for k, v in (j.get("model_info") or {}).items() if k.endswith(".context_length")), None)
            return caps_from_metadata({"capabilities": j.get("capabilities") or []}), ctx
    except Exception:
        pass
    return set(), None


async def probe_embedding(client, base, api_key, model) -> bool:
    try:
        r = await client.post(
            f"{base}/embeddings", json={"model": model, "input": "ping"}, headers=headers_for(api_key)
        )
        if r.status_code == 200:
            d = r.json().get("data")
            return bool(d and d[0].get("embedding"))
    except Exception:
        pass
    return False


async def probe_chat(client, base, api_key, model, messages, max_tokens=24) -> Optional[dict]:
    try:
        r = await client.post(
            f"{base}/chat/completions",
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False},
            headers=headers_for(api_key),
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


async def probe_vision(client, base, api_key, model) -> bool:
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": PNG_1x1}},
    ]}]
    return (await probe_chat(client, base, api_key, model, msgs, 1)) is not None


async def probe_tools(client, base, api_key, model) -> bool:
    try:
        r = await client.post(
            f"{base}/chat/completions",
            json={"model": model, "max_tokens": 16, "stream": False,
                  "messages": [{"role": "user", "content": "What time is it? Use the tool."}],
                  "tools": [{"type": "function", "function": {"name": "get_time", "description": "Get the current time",
                                                              "parameters": {"type": "object", "properties": {}}}}]},
            headers=headers_for(api_key),
        )
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def probe_reasoning(client, base, api_key, model) -> Optional[bool]:
    res = await probe_chat(client, base, api_key, model, [{"role": "user", "content": "What is 2+2?"}], 64)
    if res is None:
        return None
    try:
        msg = res["choices"][0]["message"]
    except Exception:
        return None
    if msg.get("reasoning_content") or msg.get("reasoning"):
        return True
    return "<think>" in (msg.get("content") or "")


async def discover(base_url: str, api_key: Optional[str], probe: bool = True, provider: Optional[str] = None) -> list[DiscoveredModel]:
    """Return discovered models with capabilities. Raises on connectivity failure."""
    base = normalize_base_url(base_url)
    provider = provider or detect_provider(base)
    timeout = httpx.Timeout(20.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        items = await fetch_models(client, base, api_key)
        if provider:  # hosted catalog: metadata is authoritative; no probes (they cost money / allowance) and no local-engine lookups
            results = []
            for it in items:
                dm = DiscoveredModel(upstream_id=it["id"])
                dm.capabilities = caps_from_opencode(it) or {"chat"}
                dm.sources = {c: "metadata" for c in dm.capabilities}
                dm.context_length = extract_context(it)
                price = (((it.get("cost") or {}).get("text") or {}).get("input") or {}).get("price") if isinstance(it.get("cost"), dict) else None
                dm.free = price == 0
                dm.cost = it.get("cost") if isinstance(it.get("cost"), dict) else None
                results.append(dm)
            return results
        props = await probe_props(client, base, api_key)
        results: list[DiscoveredModel] = []
        for it in items:
            mid = it["id"]
            dm = DiscoveredModel(upstream_id=mid)
            caps: dict[str, str] = {}

            def add(cs, src):
                for c in cs:
                    caps[c] = src

            add(heuristics_from_name(mid), "name")
            add(caps_from_metadata(it), "metadata")
            meta = it.get("meta") or {}
            dm.context_length = extract_context(it, props)
            if props:
                mods = (props.get("modalities") or {})
                if mods.get("vision"):
                    caps["vision"] = "metadata"
                if mods.get("audio"):
                    caps["audio"] = "metadata"
                if props.get("total_slots"):
                    dm.max_slots = int(props["total_slots"])
                tcaps = props.get("chat_template_caps") or {}
                if tcaps.get("supports_tools") or tcaps.get("supports_tool_calls"):
                    caps["tools"] = "metadata"
            if it.get("max_slots"):
                dm.max_slots = int(it["max_slots"])
            oc, octx = await probe_ollama_show(client, base, api_key, mid)
            add(oc, "metadata")
            dm.context_length = dm.context_length or octx

            if probe:
                is_embed = await probe_embedding(client, base, api_key, mid)
                if is_embed:
                    caps["embedding"] = "probe"
                else:
                    caps.pop("embedding", None) if caps.get("embedding") == "name" else None
                if not is_embed:
                    if await probe_vision(client, base, api_key, mid):
                        caps["vision"] = "probe"
                    elif caps.get("vision") == "name":
                        caps.pop("vision")  # name suggested vision but upstream rejected images
                    r = await probe_reasoning(client, base, api_key, mid)
                    if r:
                        caps["reasoning"] = "probe"
                    if "tools" not in caps and await probe_tools(client, base, api_key, mid):
                        caps["tools"] = "probe"
            if not (caps.keys() & {"embedding", "tts", "image_gen"}):
                caps["chat"] = caps.get("chat", "default")
            dm.capabilities = set(caps)
            dm.sources = caps
            results.append(dm)
        return results


async def fetch_voices(base_url: str, api_key: Optional[str], provider: Optional[str] = None, model: Optional[str] = None) -> Optional[list]:
    """The voices an upstream reports via GET /audio/voices (Kokoro-FastAPI, oMLX and compatible servers); None when it has no such endpoint.
    `model` is sent as ?model=: oMLX lists voices per model and answers 400 without it, servers that serve one voice list ignore it."""
    from .voices import parse_voices
    base = normalize_base_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            r = await client.get(f"{base}/audio/voices", params={"model": model} if model else None,
                                   headers={**headers_for(api_key), **provider_headers_for(provider)})
        if r.status_code != 200:
            return None
        voices = parse_voices(r.json())
        return voices or None
    except Exception:  # noqa: BLE001  (unreachable, not JSON, ...)
        return None


async def ping(base_url: str, api_key: Optional[str]) -> tuple[bool, Optional[str]]:
    base = normalize_base_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
            r = await client.get(f"{base}/models", headers=headers_for(api_key))
            if r.status_code >= 500:
                return False, f"HTTP {r.status_code}"
            if r.status_code in (401, 403):
                return False, f"auth failed (HTTP {r.status_code})"
            return True, None
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:200]


async def detect_slot_source(base_url: str, api_key: Optional[str]) -> str:
    """Which live-load interface does this upstream expose? 'slots' (llama.cpp), 'vllm' (/metrics) or 'none'."""
    base = normalize_base_url(base_url)
    root = root_url(base)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=3.0)) as client:
            r = await client.get(f"{root}/slots", headers=headers_for(api_key))
            if r.status_code == 200 and isinstance(r.json(), list):
                return "slots"
            r = await client.get(f"{root}/metrics", headers=headers_for(api_key))
            if r.status_code == 200 and "vllm:num_requests_running" in r.text:
                return "vllm"
    except Exception:  # noqa: BLE001
        pass
    return "none"


async def read_busy_slots(client: httpx.AsyncClient, base_url: str, api_key: Optional[str], source: str) -> Optional[int]:
    root = root_url(normalize_base_url(base_url))
    try:
        if source == "slots":
            r = await client.get(f"{root}/slots", headers=headers_for(api_key))
            if r.status_code == 200:
                return sum(1 for x in r.json() if x.get("is_processing") or x.get("state") == 1)
        elif source == "vllm":
            r = await client.get(f"{root}/metrics", headers=headers_for(api_key))
            if r.status_code == 200:
                m = re.search(r"^vllm:num_requests_running(?:\{[^}]*\})?\s+([0-9.]+)", r.text, re.M)
                if m:
                    return int(float(m.group(1)))
    except Exception:  # noqa: BLE001
        pass
    return None


def provider_headers_for(provider: Optional[str]) -> dict:
    """Headers a hosted catalog needs even for probes (Go refuses requests without a session id)."""
    if not provider or not provider.startswith("opencode"):
        return {}
    from . import config
    return {"x-opencode-session": "sg-keycheck", "User-Agent": f"SmartGateway/{config.VERSION}"}


async def check_key(base_url: str, api_key: str, model: str, provider: Optional[str] = None) -> tuple[Optional[bool], str]:
    """Validate an API key with ONE 1-token request to a model the catalog lists as free.
    -> (True, "") accepted, (False, reason) rejected, (None, reason) could not tell (e.g. quota reached, network)."""
    base = normalize_base_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            r = await client.post(f"{base}/chat/completions", headers={**headers_for(api_key), **provider_headers_for(provider)},
                                  json={"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]})
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"[:160]
    if r.status_code in (401, 403):
        return False, f"HTTP {r.status_code}"
    if r.status_code < 400:
        return True, ""
    return None, f"HTTP {r.status_code}"
