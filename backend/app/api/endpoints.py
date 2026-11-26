from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import config, crypto, discovery
from ..db import get_session
from ..models import Endpoint, GatewayModel, Member, Model
from ..router import health
from ..router.scheduler import scheduler

router = APIRouter(prefix="/api")


class EndpointIn(BaseModel):
    name: str
    base_url: str = ""  # may be empty when a provider preset is given
    api_key: Optional[str] = None
    probe: bool = True
    provider: Optional[str] = None  # opencode-zen | opencode-go (None = detect from the URL)


class EndpointPatch(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None


class ModelPatch(BaseModel):
    capabilities: Optional[list[str]] = None
    max_slots: Optional[int] = None
    enabled: Optional[bool] = None


def ep_out(ep: Endpoint, s: Session) -> dict:
    models = s.exec(select(Model).where(Model.endpoint_id == ep.id)).all()
    d = ep.model_dump()
    d["api_key"] = bool(ep.api_key)
    d["key_unreadable"] = crypto.is_unreadable(ep.api_key)
    d["models"] = [m.model_dump() for m in models]
    return d


def _delete_model(s: Session, m: Model) -> None:
    """Delete a model with its gateway memberships and runtime state (Member has no cascade)."""
    members = s.exec(select(Member).where(Member.model_id == m.id)).all()
    member_ids = [mem.id for mem in members]
    for mem in members:
        s.delete(mem)
    scheduler.forget_model(m.id, member_ids)
    s.delete(m)


def _need_readable_key(ep: Endpoint) -> None:
    if crypto.is_unreadable(ep.api_key):
        raise HTTPException(409, "The stored API key of this endpoint cannot be decrypted (secret.key / SG_SECRET_KEY changed?). Enter the key again.")


async def sync_models(ep: Endpoint, s: Session, probe: bool, free_out: Optional[list] = None) -> int:
    _need_readable_key(ep)
    ep.provider = ep.provider or discovery.detect_provider(ep.base_url)
    found = await discovery.discover(ep.base_url, ep.api_key, probe and not ep.provider, ep.provider)
    ep.slot_source = "none" if ep.provider else await discovery.detect_slot_source(ep.base_url, ep.api_key)
    s.add(ep)
    if free_out is not None:
        free_out.extend(dm.upstream_id for dm in found if dm.free)
    existing = {m.upstream_id: m for m in s.exec(select(Model).where(Model.endpoint_id == ep.id)).all()}
    for dm in found:
        m = existing.pop(dm.upstream_id, None)
        if m is None:
            m = Model(endpoint_id=ep.id, upstream_id=dm.upstream_id, enabled=False,  # new models stay off until enabled by hand
                      max_slots=dm.max_slots or (config.HOSTED_SLOTS if ep.provider else config.DEFAULT_SLOTS))
        # keep manual overrides, refresh detected ones
        manual = {c for c, src in (m.cap_sources or {}).items() if src == "manual"}
        caps = set(dm.capabilities) | manual
        m.capabilities = sorted(caps)
        m.cap_sources = {**dm.sources, **{c: "manual" for c in manual}}
        if dm.max_slots and not m.id:
            m.max_slots = dm.max_slots
        if dm.context_length is not None:
            m.context_length = dm.context_length
        s.add(m)
    # text-to-speech models: remember the voices the upstream offers; a failed lookup keeps the old list
    tts = [m for m in s.exec(select(Model).where(Model.endpoint_id == ep.id)).all() if "tts" in (m.capabilities or []) and m.upstream_id not in existing]
    for m in tts:   # per model: some servers (oMLX) keep a voice list for each model
        voices = await discovery.fetch_voices(ep.base_url, ep.api_key, ep.provider, m.upstream_id)
        if voices is not None:
            m.voices = voices
            s.add(m)
    # drop models the upstream no longer lists; an empty result is treated as "unknown", not "all gone"
    if found:
        for m in existing.values():
            _delete_model(s, m)
    s.commit()
    return len(found)


@router.get("/capabilities")
def capabilities():
    return {"capabilities": config.CAPABILITIES, "strategies": config.STRATEGIES}


@router.get("/endpoints")
def list_endpoints(s: Session = Depends(get_session)):
    return [ep_out(e, s) for e in s.exec(select(Endpoint)).all()]


@router.post("/endpoints")
async def add_endpoint(body: EndpointIn, s: Session = Depends(get_session)):
    provider = body.provider or None
    if provider and provider not in discovery.PROVIDERS:
        raise HTTPException(400, f"Unknown provider '{provider}'")
    base = discovery.normalize_base_url(body.base_url or (discovery.PROVIDERS[provider]["base_url"] if provider else ""))
    provider = provider or discovery.detect_provider(base)
    ok, err = await discovery.ping(base, body.api_key)
    if not ok:
        raise HTTPException(400, f"Cannot reach endpoint: {err}")
    ep = Endpoint(name=body.name, base_url=base, api_key=body.api_key or None, status="online", provider=provider)
    s.add(ep)
    s.commit()
    s.refresh(ep)
    free: list = []
    try:
        await sync_models(ep, s, body.probe, free)
    except Exception as e:  # noqa: BLE001
        s.delete(ep)
        s.commit()
        raise HTTPException(400, f"Model discovery failed: {e}")
    if provider and body.api_key and free:
        # the catalog is public, so a wrong key would go unnoticed until the first request: check it with one free 1-token call
        good, why = await discovery.check_key(base, body.api_key, free[0], provider)
        if good is False:
            for m in s.exec(select(Model).where(Model.endpoint_id == ep.id)).all():
                _delete_model(s, m)
            s.delete(ep)
            s.commit()
            raise HTTPException(400, f"The API key was rejected by {discovery.PROVIDERS[provider]['label']} ({why})")
    health.status[ep.id] = "online"
    return ep_out(ep, s)


@router.patch("/endpoints/{ep_id}")
def patch_endpoint(ep_id: int, body: EndpointPatch, s: Session = Depends(get_session)):
    ep = s.get(Endpoint, ep_id)
    if not ep:
        raise HTTPException(404)
    if body.name is not None:
        ep.name = body.name
    if body.base_url is not None:
        ep.base_url = discovery.normalize_base_url(body.base_url)
        ep.provider = discovery.detect_provider(ep.base_url) or ep.provider
    if body.api_key is not None:
        ep.api_key = body.api_key or None
    s.add(ep)
    s.commit()
    return ep_out(ep, s)


@router.post("/endpoints/{ep_id}/rescan")
async def rescan(ep_id: int, probe: bool = True, s: Session = Depends(get_session)):
    ep = s.get(Endpoint, ep_id)
    if not ep:
        raise HTTPException(404)
    try:
        await sync_models(ep, s, probe)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Model discovery failed: {e}")
    await health.check_endpoint(ep_id)
    s.refresh(ep)
    return ep_out(ep, s)


@router.delete("/endpoints/{ep_id}")
def delete_endpoint(ep_id: int, s: Session = Depends(get_session)):
    ep = s.get(Endpoint, ep_id)
    if not ep:
        raise HTTPException(404)
    for m in s.exec(select(Model).where(Model.endpoint_id == ep_id)).all():
        _delete_model(s, m)
    s.delete(ep)
    s.commit()
    health.status.pop(ep_id, None)
    return {"ok": True}


@router.post("/models/{model_id}/voices/refresh")
async def refresh_voices(model_id: int, s: Session = Depends(get_session)):
    """Ask the upstream for its voice list again (the gateway editor offers them as a checklist)."""
    m = s.get(Model, model_id)
    ep = s.get(Endpoint, m.endpoint_id) if m else None
    if not m or not ep:
        raise HTTPException(404)
    if "tts" not in (m.capabilities or []):
        raise HTTPException(400, "Only text-to-speech models have voices")
    _need_readable_key(ep)
    voices = await discovery.fetch_voices(ep.base_url, ep.api_key, ep.provider, m.upstream_id)
    if voices is None:
        raise HTTPException(400, "The upstream did not return a voice list (GET /audio/voices). Type the voice names in the gateway editor instead.")
    m.voices = voices
    s.add(m)
    s.commit()
    s.refresh(m)
    return m.model_dump()


@router.patch("/models/{model_id}")
def patch_model(model_id: int, body: ModelPatch, s: Session = Depends(get_session)):
    m = s.get(Model, model_id)
    if not m:
        raise HTTPException(404)
    if body.capabilities is not None:
        caps = [c for c in body.capabilities if c in config.CAPABILITIES]
        old = set(m.capabilities or [])
        src = dict(m.cap_sources or {})
        for c in set(caps) ^ old:
            src[c] = "manual"
        for c in old - set(caps):
            src.pop(c, None)
        m.capabilities = sorted(caps)
        m.cap_sources = src
    if body.max_slots is not None:
        m.max_slots = max(1, body.max_slots)
    if body.enabled is not None:
        m.enabled = body.enabled
    s.add(m)
    s.commit()
    s.refresh(m)
    return m.model_dump()
