"""Admin API for MCP gateways (several MCP endpoints combined behind one MCP server URL)."""
import re
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, func, select

from ..crypto import hash_key
from ..db import get_session
from ..mcp import registry
from ..models import McpApiKey, McpCallLog, McpEndpoint, McpGateway, McpGatewayMember

router = APIRouter(prefix="/api")


class MemberIn(BaseModel):
    endpoint_id: int
    prefix: Optional[str] = None
    enabled: bool = True
    enabled_items: list[str] = []


class GatewayIn(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    members: list[MemberIn] = []


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "mcp"


def _validate(body: GatewayIn, s: Session):
    if not body.name.strip():
        raise HTTPException(400, "Name required")
    seen_ep, seen_prefix = set(), set()
    for m in body.members:
        ep = s.get(McpEndpoint, m.endpoint_id)
        if not ep:
            raise HTTPException(400, f"MCP endpoint {m.endpoint_id} does not exist")
        if m.endpoint_id in seen_ep:
            raise HTTPException(400, f"Endpoint '{ep.name}' is added twice")
        seen_ep.add(m.endpoint_id)
        m.prefix = registry.clean_prefix(m.prefix or ep.name)
        if m.prefix in seen_prefix:
            raise HTTPException(400, f"The prefix '{m.prefix}' is used twice; give each endpoint its own prefix")
        seen_prefix.add(m.prefix)
        for it in m.enabled_items:
            if it.split(":", 1)[0] not in registry.KINDS or ":" not in it:
                raise HTTPException(400, f"Invalid item '{it}'")


def _apply(gw: McpGateway, body: GatewayIn, s: Session):
    for m in s.exec(select(McpGatewayMember).where(McpGatewayMember.gateway_id == gw.id)).all():
        s.delete(m)
    s.flush()
    for m in body.members:
        s.add(McpGatewayMember(gateway_id=gw.id, endpoint_id=m.endpoint_id, prefix=m.prefix, enabled=m.enabled, enabled_items=sorted(set(m.enabled_items))))


def gw_out(gw: McpGateway, s: Session) -> dict:
    d = gw.model_dump()
    d["path"] = f"/mcp/{gw.slug}"
    d["has_keys"] = s.exec(select(McpApiKey).where(McpApiKey.gateway_id == gw.id)).first() is not None   # any key (even disabled) keeps the gateway closed
    d["members"] = []
    for m in s.exec(select(McpGatewayMember).where(McpGatewayMember.gateway_id == gw.id).order_by(McpGatewayMember.id)).all():
        ep = s.get(McpEndpoint, m.endpoint_id)
        d["members"].append({**m.model_dump(exclude={"gateway_id"}), "endpoint_name": ep.name if ep else "?", "status": registry.status.get(m.endpoint_id, ep.status if ep else "unknown")})
    cat = registry.catalog(gw.id)
    d["exposed"] = {k: len(v) for k, v in cat.items()}
    since = time.time() - 86400
    d["calls_24h"] = s.exec(select(func.count()).select_from(McpCallLog).where(McpCallLog.gateway_id == gw.id, McpCallLog.ts >= since)).one()
    d["errors_24h"] = s.exec(select(func.count()).select_from(McpCallLog).where(McpCallLog.gateway_id == gw.id, McpCallLog.ts >= since, McpCallLog.ok == False)).one()  # noqa: E712
    return d


@router.get("/mcp-gateways")
def list_gateways(s: Session = Depends(get_session)):
    return [gw_out(g, s) for g in s.exec(select(McpGateway).order_by(McpGateway.id)).all()]


@router.get("/mcp-gateways/{gw_id}")
def get_gateway(gw_id: int, s: Session = Depends(get_session)):
    gw = s.get(McpGateway, gw_id)
    if not gw:
        raise HTTPException(404)
    return gw_out(gw, s)


@router.post("/mcp-gateways")
def create_gateway(body: GatewayIn, s: Session = Depends(get_session)):
    _validate(body, s)
    slug = slugify(body.slug or body.name)
    if s.exec(select(McpGateway).where(McpGateway.slug == slug)).first():
        raise HTTPException(400, f"Slug '{slug}' already in use")
    gw = McpGateway(slug=slug, name=body.name.strip(), description=body.description or None)
    s.add(gw)
    s.flush()
    _apply(gw, body, s)
    s.commit()
    s.refresh(gw)
    return gw_out(gw, s)


@router.put("/mcp-gateways/{gw_id}")
def update_gateway(gw_id: int, body: GatewayIn, s: Session = Depends(get_session)):
    gw = s.get(McpGateway, gw_id)
    if not gw:
        raise HTTPException(404)
    _validate(body, s)
    slug = slugify(body.slug or gw.slug)
    other = s.exec(select(McpGateway).where(McpGateway.slug == slug)).first()
    if other and other.id != gw.id:
        raise HTTPException(400, f"Slug '{slug}' already in use")
    gw.slug, gw.name, gw.description = slug, body.name.strip(), body.description or None
    s.add(gw)
    _apply(gw, body, s)
    s.commit()
    s.refresh(gw)
    return gw_out(gw, s)


@router.delete("/mcp-gateways/{gw_id}")
def delete_gateway(gw_id: int, s: Session = Depends(get_session)):
    gw = s.get(McpGateway, gw_id)
    if not gw:
        raise HTTPException(404)
    for m in s.exec(select(McpGatewayMember).where(McpGatewayMember.gateway_id == gw_id)).all():
        s.delete(m)
    for k in s.exec(select(McpApiKey).where(McpApiKey.gateway_id == gw_id)).all():
        s.delete(k)
    s.delete(gw)
    s.commit()
    return {"ok": True}


@router.get("/mcp-gateways/{gw_id}/catalog")
def catalog(gw_id: int, s: Session = Depends(get_session)):
    if not s.get(McpGateway, gw_id):
        raise HTTPException(404)
    return {k: [{"name": e["name"], "endpoint": e["endpoint"], "description": (e["definition"] or {}).get("description")} for e in v] for k, v in registry.catalog(gw_id).items()}


@router.get("/mcp-gateways/{gw_id}/calls")
def calls(gw_id: int, limit: int = 100, s: Session = Depends(get_session)):
    if not s.get(McpGateway, gw_id):
        raise HTTPException(404)
    recent = s.exec(select(McpCallLog).where(McpCallLog.gateway_id == gw_id).order_by(McpCallLog.id.desc()).limit(max(1, min(limit, 500)))).all()
    since = time.time() - 86400
    rows = s.exec(select(McpCallLog.name, func.count(), func.sum(McpCallLog.ok == False), func.avg(McpCallLog.total_ms))  # noqa: E712
                  .where(McpCallLog.gateway_id == gw_id, McpCallLog.ts >= since).group_by(McpCallLog.name).order_by(func.count().desc()).limit(50)).all()
    return {"recent": [r.model_dump() for r in recent],
            "stats": [{"name": n, "calls": c, "errors": int(e or 0), "avg_ms": int(a or 0)} for n, c, e, a in rows]}


# ---------------------------------------------------------------- API keys
class KeyIn(BaseModel):
    name: str = ""
    enabled: bool = True
    expires_at: Optional[float] = None
    rpm_limit: Optional[int] = None
    allowed_tools: Optional[list[str]] = None


def key_out(k: McpApiKey) -> dict:
    d = k.model_dump(exclude={"key_hash"})
    since = time.time() - 86400
    return d


@router.get("/mcp-gateways/{gw_id}/keys")
def list_keys(gw_id: int, s: Session = Depends(get_session)):
    return [key_out(k) for k in s.exec(select(McpApiKey).where(McpApiKey.gateway_id == gw_id)).all()]


@router.post("/mcp-gateways/{gw_id}/keys")
def create_key(gw_id: int, body: KeyIn, s: Session = Depends(get_session)):
    if not s.get(McpGateway, gw_id):
        raise HTTPException(404)
    plain = "sgm-" + secrets.token_urlsafe(24)
    k = McpApiKey(gateway_id=gw_id, key_hash=hash_key(plain), key_prefix=plain[:8] + "…", **body.model_dump())
    s.add(k)
    s.commit()
    s.refresh(k)
    return {**key_out(k), "key": plain}   # the only time the plaintext is returned


@router.patch("/mcp-keys/{key_id}")
def update_key(key_id: int, body: KeyIn, s: Session = Depends(get_session)):
    k = s.get(McpApiKey, key_id)
    if not k:
        raise HTTPException(404)
    for f, v in body.model_dump().items():
        setattr(k, f, v)
    s.add(k)
    s.commit()
    s.refresh(k)
    return key_out(k)


@router.delete("/mcp-keys/{key_id}")
def delete_key(key_id: int, s: Session = Depends(get_session)):
    k = s.get(McpApiKey, key_id)
    if not k:
        raise HTTPException(404)
    s.delete(k)
    s.commit()
    return {"ok": True}
