"""Admin API for MCP endpoints (external MCP servers)."""
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, func, select

from ..db import get_session
from ..mcp import registry
from ..models import McpEndpoint, McpGatewayMember, McpGateway, McpItem

router = APIRouter(prefix="/api")


class EndpointIn(BaseModel):
    name: str
    url: str
    transport: str = "auto"
    headers: dict[str, str] = {}


class EndpointPatch(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    transport: Optional[str] = None
    headers: Optional[dict[str, str]] = None   # an empty value keeps the saved value of that header; headers left out are removed


def _check(name: str, url: str, transport: str):
    if not name.strip():
        raise HTTPException(400, "Name required")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "The URL must start with http:// or https://")
    if transport not in ("auto", "streamable_http", "sse"):
        raise HTTPException(400, "transport must be auto, streamable_http or sse")


def _counts(s: Session, ep_id: int) -> dict:
    rows = s.exec(select(McpItem.kind, func.count()).where(McpItem.endpoint_id == ep_id).group_by(McpItem.kind)).all()
    return {k: n for k, n in rows} | {k: 0 for k in registry.KINDS if k not in dict(rows)}


def ep_out(ep: McpEndpoint, s: Session) -> dict:
    d = ep.model_dump(exclude={"headers_enc"})
    d["status"] = registry.status.get(ep.id, ep.status)
    d["header_names"] = sorted(registry.headers_of(ep))
    d["counts"] = _counts(s, ep.id)
    d["used_by"] = sorted(g.name for g in s.exec(select(McpGateway).join(McpGatewayMember, McpGatewayMember.gateway_id == McpGateway.id).where(McpGatewayMember.endpoint_id == ep.id)).all())
    return d


@router.get("/mcp-endpoints")
def list_endpoints(s: Session = Depends(get_session)):
    return [ep_out(e, s) for e in s.exec(select(McpEndpoint).order_by(McpEndpoint.id)).all()]


class TestIn(EndpointIn):
    endpoint_id: Optional[int] = None   # editing: an empty header value means "the value saved for that header of this endpoint"


@router.post("/mcp-endpoints/test")
async def test_endpoint(body: TestIn, s: Session = Depends(get_session)):
    _check(body.name or "x", body.url, body.transport)
    headers = dict(body.headers)
    if body.endpoint_id:
        ep = s.get(McpEndpoint, body.endpoint_id)
        saved = registry.headers_of(ep) if ep else {}
        headers = {k: (saved[k] if v == "" and k in saved else v) for k, v in headers.items()}
    return await registry.probe(body.url, {k: v for k, v in headers.items() if v != ""}, body.transport)


@router.post("/mcp-endpoints")
async def create_endpoint(body: EndpointIn, s: Session = Depends(get_session)):
    _check(body.name, body.url, body.transport)
    ep = McpEndpoint(name=body.name.strip(), url=body.url.strip(), transport=body.transport, headers_enc=json.dumps(body.headers) if body.headers else None)
    s.add(ep)
    s.commit()
    s.refresh(ep)
    ok, err = await registry.sync_endpoint(ep.id)
    if not ok:
        await registry.drop_session(ep.id)
        s.exec(select(McpItem).where(McpItem.endpoint_id == ep.id))
        for it in s.exec(select(McpItem).where(McpItem.endpoint_id == ep.id)).all():
            s.delete(it)
        s.delete(s.get(McpEndpoint, ep.id))
        s.commit()
        raise HTTPException(400, f"Cannot connect to the MCP server: {err}")
    s.refresh(ep)
    return ep_out(ep, s)


@router.get("/mcp-endpoints/{ep_id}")
def get_endpoint(ep_id: int, s: Session = Depends(get_session)):
    ep = s.get(McpEndpoint, ep_id)
    if not ep:
        raise HTTPException(404)
    return ep_out(ep, s)


@router.get("/mcp-endpoints/{ep_id}/items")
def endpoint_items(ep_id: int, s: Session = Depends(get_session)):
    if not s.get(McpEndpoint, ep_id):
        raise HTTPException(404)
    return [{"kind": i.kind, "name": i.name, "title": (i.definition or {}).get("title") or (i.definition or {}).get("name"),
             "description": (i.definition or {}).get("description")} for i in s.exec(select(McpItem).where(McpItem.endpoint_id == ep_id).order_by(McpItem.kind, McpItem.name)).all()]


@router.patch("/mcp-endpoints/{ep_id}")
async def update_endpoint(ep_id: int, body: EndpointPatch, s: Session = Depends(get_session)):
    ep = s.get(McpEndpoint, ep_id)
    if not ep:
        raise HTTPException(404)
    name, url, transport = body.name or ep.name, body.url or ep.url, body.transport or ep.transport
    _check(name, url, transport)
    ep.name, ep.url, ep.transport = name.strip(), url.strip(), transport
    if body.headers is not None:
        old = registry.headers_of(ep)
        merged = {k: (old[k] if v == "" and k in old else v) for k, v in body.headers.items()}
        ep.headers_enc = json.dumps(merged) if merged else None
    s.add(ep)
    s.commit()
    await registry.sync_endpoint(ep_id)
    s.refresh(ep)
    return ep_out(ep, s)


@router.post("/mcp-endpoints/{ep_id}/rescan")
async def rescan_endpoint(ep_id: int, s: Session = Depends(get_session)):
    ep = s.get(McpEndpoint, ep_id)
    if not ep:
        raise HTTPException(404)
    await registry.sync_endpoint(ep_id)
    s.refresh(ep)
    return ep_out(ep, s)


@router.delete("/mcp-endpoints/{ep_id}")
async def delete_endpoint(ep_id: int, force: bool = False, s: Session = Depends(get_session)):
    ep = s.get(McpEndpoint, ep_id)
    if not ep:
        raise HTTPException(404)
    used = ep_out(ep, s)["used_by"]
    if used and not force:
        raise HTTPException(409, detail={"message": f"Used by MCP gateways: {', '.join(used)}", "used_by": used})
    for m in s.exec(select(McpGatewayMember).where(McpGatewayMember.endpoint_id == ep_id)).all():
        s.delete(m)
    for it in s.exec(select(McpItem).where(McpItem.endpoint_id == ep_id)).all():
        s.delete(it)
    s.delete(ep)
    s.commit()
    await registry.drop_session(ep_id)
    return {"ok": True}
