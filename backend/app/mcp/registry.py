"""MCP endpoints: connections, discovery cache, health, and the namespaced catalogue of an MCP gateway."""
import asyncio
import json
import re
import time
from typing import Optional

from sqlmodel import select

from .. import config
from ..db import session
from ..models import McpEndpoint, McpGatewayMember, McpItem
from .. import crypto
from .client import McpError, McpSession

status: dict[int, str] = {}                      # endpoint_id -> online | offline | unknown
_sessions: dict[int, tuple[tuple, McpSession]] = {}
KINDS = ("tool", "resource", "template", "prompt")
SEP = "__"


def headers_of(ep: McpEndpoint) -> dict:
    try:
        d = json.loads(ep.headers_enc or "{}")
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except ValueError:
        return {}


MAX_SCOPED_SESSIONS = 64   # per endpoint; the oldest scoped session is closed when exceeded


class _UnreadableHeaders(McpSession):
    """Stands in for an endpoint whose stored headers cannot be decrypted: every call fails instead of going out without its credentials."""

    async def ensure(self):
        raise McpError("The stored headers of this MCP endpoint cannot be decrypted (secret.key / SG_SECRET_KEY changed?). Enter them again.")


def session_for(ep: McpEndpoint, scope: str = "") -> McpSession:
    """One cached session per endpoint (and per scope, when the gateway isolates its clients); rebuilt when the endpoint is edited."""
    if crypto.is_unreadable(ep.headers_enc):
        return _UnreadableHeaders(ep.url, {}, ep.transport)
    sig = (ep.url, ep.headers_enc, ep.transport)
    k = (ep.id, scope)
    cur = _sessions.get(k)
    if cur and cur[0] == sig:
        return cur[1]
    if cur:
        asyncio.get_running_loop().create_task(cur[1].close())
    s = McpSession(ep.url, headers_of(ep), ep.transport)
    _sessions[k] = (sig, s)
    scoped = [x for x in _sessions if x[0] == ep.id and x[1]]
    for old in scoped[:max(0, len(scoped) - MAX_SCOPED_SESSIONS)]:   # dicts keep insertion order: the oldest go first
        asyncio.get_running_loop().create_task(_sessions.pop(old)[1].close())
    return s


async def drop_session(ep_id: int) -> None:
    for k in [k for k in _sessions if k[0] == ep_id]:
        await _sessions.pop(k)[1].close()
    status.pop(ep_id, None)


def _msg(e: Exception) -> str:
    return (str(e) or e.__class__.__name__)[:300]


async def discover(s: McpSession) -> dict:
    """Connect and list everything the server offers. Each list is independent: a server may advertise one it cannot serve."""
    await s.ensure()                              # connect first: errors here mean the endpoint is unreachable
    out: dict = {"tool": None, "resource": None, "template": None, "prompt": None}
    for kind, method, key, cap in (("tool", "tools/list", "tools", "tools"), ("resource", "resources/list", "resources", "resources"),
                                   ("template", "resources/templates/list", "resourceTemplates", "resources"), ("prompt", "prompts/list", "prompts", "prompts")):
        if cap not in s.capabilities:
            out[kind] = []
            continue
        try:
            out[kind] = await s.list_all(method, key)
        except McpError:
            out[kind] = None                     # keep the cached items of this kind
    return out


async def sync_endpoint(ep_id: int) -> tuple[bool, Optional[str]]:
    with session() as s:
        ep = s.get(McpEndpoint, ep_id)
        if not ep:
            return False, "not found"
    sess = session_for(ep)
    try:
        found = await discover(sess)
        ok, err = True, None
    except Exception as e:  # noqa: BLE001
        found, ok, err = None, False, _msg(e)
        sess._ready = False
    with session() as s:
        ep = s.get(McpEndpoint, ep_id)
        if not ep:
            return False, "deleted"
        ep.status, ep.last_error, ep.last_checked = ("online" if ok else "offline"), err, time.time()
        if ok:
            ep.server_info, ep.protocol_version = sess.server_info, sess.protocol
            for kind in KINDS:
                items = found.get(kind)
                if items is None:
                    continue
                have = {i.name: i for i in s.exec(select(McpItem).where(McpItem.endpoint_id == ep_id, McpItem.kind == kind)).all()}
                seen = set()
                for d in items:
                    name = d.get("uriTemplate") if kind == "template" else d.get("uri") if kind == "resource" else d.get("name")
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    row = have.get(name) or McpItem(endpoint_id=ep_id, kind=kind, name=name)
                    row.definition, row.seen_at = d, time.time()
                    s.add(row)
                for name, row in have.items():
                    if name not in seen:
                        s.delete(row)
        s.add(ep)
        s.commit()
    status[ep_id] = "online" if ok else "offline"
    return ok, err


async def probe(url: str, headers: dict, transport: str) -> dict:
    """Try a server without saving it: connect, list, close."""
    sess = McpSession(url, headers, transport)
    try:
        found = await discover(sess)
        return {"ok": True, "server_info": sess.server_info, "protocol": sess.protocol, "mode": sess.mode,
                "counts": {k: len(v or []) for k, v in found.items()}}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _msg(e)}
    finally:
        await sess.close()


async def loop() -> None:
    """Ping every endpoint each interval; re-discover about every 20 intervals."""
    n = 0
    while True:
        try:
            with session() as s:
                ids = [e.id for e in s.exec(select(McpEndpoint)).all()]
            if n % 20 == 0:
                await asyncio.gather(*(sync_endpoint(i) for i in ids), return_exceptions=True)
            else:
                await asyncio.gather(*(_ping(i) for i in ids), return_exceptions=True)
        except Exception:  # noqa: BLE001
            pass
        n += 1
        await asyncio.sleep(config.HEALTH_INTERVAL)


async def _ping(ep_id: int) -> None:
    with session() as s:
        ep = s.get(McpEndpoint, ep_id)
    if not ep:
        return
    try:
        await session_for(ep).ping()
        ok, err = True, None
    except Exception as e:  # noqa: BLE001
        ok, err = False, _msg(e)
        _s = _sessions.get((ep_id, ""))
        if _s:
            _s[1]._ready = False
    with session() as s:
        ep = s.get(McpEndpoint, ep_id)
        if ep:
            ep.status, ep.last_error, ep.last_checked = ("online" if ok else "offline"), err, time.time()
            s.add(ep)
            s.commit()
    status[ep_id] = "online" if ok else "offline"


# ---------------------------------------------------------------- catalogue of a gateway
def clean_prefix(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-").lower() or "mcp"


def item_key(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def exposed_name(prefix: str, kind: str, name: str) -> str:
    return f"mcp://{prefix}/{name}" if kind in ("resource", "template") else f"{prefix}{SEP}{name}"


def template_regex(t: str) -> "re.Pattern":
    parts = re.split(r"\{[^}]*\}", t)
    return re.compile(".+".join(re.escape(p) for p in parts) + r"\Z", re.S)


def catalog(gateway_id: int, enabled_only: bool = True) -> dict:
    """{tools|resources|templates|prompts: [{name (exposed), endpoint_id, endpoint, upstream, prefix, enabled, definition}]}."""
    out: dict = {"tools": [], "resources": [], "templates": [], "prompts": []}
    plural = {"tool": "tools", "resource": "resources", "template": "templates", "prompt": "prompts"}
    with session() as s:
        for m in s.exec(select(McpGatewayMember).where(McpGatewayMember.gateway_id == gateway_id).order_by(McpGatewayMember.id)).all():
            if enabled_only and not m.enabled:
                continue
            ep = s.get(McpEndpoint, m.endpoint_id)
            if not ep:
                continue
            on = set(m.enabled_items or [])
            for it in s.exec(select(McpItem).where(McpItem.endpoint_id == m.endpoint_id).order_by(McpItem.id)).all():
                enabled = item_key(it.kind, it.name) in on
                if enabled_only and not enabled:
                    continue
                name = exposed_name(m.prefix, it.kind, it.name)
                d = dict(it.definition or {})
                if it.kind == "tool":
                    d["name"] = name
                elif it.kind == "prompt":
                    d["name"] = name
                elif it.kind == "resource":
                    d["uri"] = name
                else:
                    d["uriTemplate"] = name
                out[plural[it.kind]].append({"name": name, "endpoint_id": ep.id, "endpoint": ep.name, "upstream": it.name, "prefix": m.prefix,
                                             "kind": it.kind, "enabled": enabled, "definition": d})
    return out


def resolve(gateway_id: int, kind: str, name: str) -> Optional[dict]:
    """The enabled catalogue entry behind an exposed tool/prompt name or resource uri; resource uris may also match an enabled template."""
    cat = catalog(gateway_id)
    if kind in ("tool", "prompt"):
        return next((e for e in cat["tools" if kind == "tool" else "prompts"] if e["name"] == name), None)
    hit = next((e for e in cat["resources"] if e["name"] == name), None)
    if hit:
        return hit
    for t in cat["templates"]:
        if name.startswith(f"mcp://{t['prefix']}/") and template_regex(t["upstream"]).match(name[len(f"mcp://{t['prefix']}/"):]):
            return {**t, "upstream": name[len(f"mcp://{t['prefix']}/"):]}
    return None
