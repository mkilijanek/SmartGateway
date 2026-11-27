import re
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..voices import abilities, clean_audio_defaults, clean_voices
from .. import config, guardrails
from ..db import get_session
from ..crypto import hash_key
from ..models import ApiKey, Endpoint, Gateway, GatewayModel, Member, Model, RequestLog
from ..router.limits import limiter
from .. import council
from ..unified import HIDDEN_SLUGS, INTERNAL_SLUG

router = APIRouter(prefix="/api")


class MemberIn(BaseModel):
    model_id: int
    weight: int = 1
    priority: int = 0
    slots_override: Optional[int] = None
    enabled: bool = True
    is_fallback: bool = False
    tier: int = 2


class GatewayModelIn(BaseModel):
    public_name: str
    enabled: bool = True
    aliases: list[str] = []  # additional exposed names for the same model
    voices: list[str] = []  # TTS: voices listed by GET /audio/voices when the upstream does not serve them
    strategy: Optional[str] = None
    sticky: bool = False
    system_prompt: Optional[str] = None
    max_tokens_cap: Optional[int] = None
    default_params: dict = {}
    audio_defaults: Optional[dict] = None   # speech / transcription defaults (see voices.clean_audio_defaults)
    cache_enabled: bool = False
    cache_ttl: Optional[int] = None
    cache_any_temperature: bool = False
    cache_streaming: bool = False
    cache_per_key: bool = False
    cache_replay_speed: str = "instant"
    compress_mode: Optional[str] = None  # None = inherit global; off | shadow | on
    guardrails: Optional[dict] = None  # None = all off
    members: list[MemberIn] = []


class GatewayIn(BaseModel):
    name: str
    slug: Optional[str] = None
    strategy: str = "least_busy"
    cache_mode: str = "off"  # deprecated, ignored (caching is per model now)
    queue_timeout: float = 60.0
    max_retries: int = 2
    models: list[GatewayModelIn] = []
    kind: str = "router"          # router | council
    council: Optional[dict] = None  # council config (see council.py); for a council, `models` holds just its public model's settings


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "gateway"


def gw_out(gw: Gateway, s: Session) -> dict:
    d = gw.model_dump()
    d.pop("api_key", None)
    d["has_keys"] = s.exec(select(ApiKey).where(ApiKey.gateway_id == gw.id, ApiKey.enabled == True)).first() is not None  # noqa: E712
    d["models"] = []
    for gm in s.exec(select(GatewayModel).where(GatewayModel.gateway_id == gw.id)).all():
        members = []
        caps: set = set()
        for mem in s.exec(select(Member).where(Member.gateway_model_id == gm.id)).all():
            m = s.get(Model, mem.model_id)
            ep = s.get(Endpoint, m.endpoint_id) if m else None
            if not m:
                continue
            caps |= set(m.capabilities or [])
            members.append({**mem.model_dump(), "upstream_id": m.upstream_id, "endpoint_id": m.endpoint_id,
                            "endpoint_name": ep.name if ep else "?", "provider": ep.provider if ep else None, "capabilities": m.capabilities, "voices": m.voices or [],
                            "max_slots": m.max_slots})
        d["models"].append({**gm.model_dump(), "members": members, "capabilities": sorted(caps), "abilities": abilities([x["capabilities"] for x in members])})
    return d


def validate(body: GatewayIn, s: Optional[Session] = None, self_id: Optional[int] = None):
    if body.kind not in ("router", "council"):
        raise HTTPException(400, "kind must be router or council")
    if body.kind == "council":
        try:
            body.council = council.normalize(body.council)
            if s is not None:
                council.check_refs(s, body.council, self_id)
        except ValueError as e:
            raise HTTPException(400, f"Council: {e}")
        if len(body.models) != 1:
            raise HTTPException(400, "A council exposes exactly one public model")
        body.models[0].members = []
    for m in body.models:
        if m.cache_replay_speed not in ("instant", "realistic"):
            raise HTTPException(400, "cache_replay_speed must be instant or realistic")
        if m.compress_mode not in (None, "", "off", "shadow", "on"):
            raise HTTPException(400, "compress_mode must be off, shadow, on or empty (inherit)")
        try:
            guardrails.normalize(m.guardrails)
        except ValueError as e:
            raise HTTPException(400, f"Guardrails of '{m.public_name}': {e}")
        if m.cache_ttl is not None and not (1 <= m.cache_ttl <= 30 * 86400):
            raise HTTPException(400, "Cache TTL must be between 1 second and 30 days")
    if body.strategy not in config.STRATEGIES:
        raise HTTPException(400, f"strategy must be one of {config.STRATEGIES}")
    for m in body.models:
        own, seen = m.public_name.strip().lower(), {}
        for a in m.aliases:
            a = a.strip()
            if len(a) > 200:
                raise HTTPException(400, "Alias too long")
            if a and a.lower() != own:
                seen.setdefault(a.lower(), a)   # case-insensitive de-duplication, first spelling wins
        m.aliases = list(seen.values())[:20]
    names = [n for m in body.models for n in [m.public_name, *m.aliases]]
    if len(set(n.strip().lower() for n in names)) != len(names):
        raise HTTPException(400, "Duplicate model names (public names and aliases must be unique within a gateway)")
    for m in body.models:
        if not m.public_name.strip():
            raise HTTPException(400, "Public model name required")
        if m.strategy and m.strategy not in config.STRATEGIES:
            raise HTTPException(400, "invalid model strategy")


def apply_models(gw: Gateway, body: GatewayIn, s: Session):
    for gm in s.exec(select(GatewayModel).where(GatewayModel.gateway_id == gw.id)).all():
        for mem in s.exec(select(Member).where(Member.gateway_model_id == gm.id)).all():
            s.delete(mem)
        s.delete(gm)
    s.flush()
    for gmi in body.models:
        missing = guardrails.library.missing_refs(s, gmi.guardrails)
        if missing:
            raise HTTPException(400, f"Guardrails of '{gmi.public_name}' reference unknown content filters: {', '.join(missing)}")
        try:
            audio_defaults = clean_audio_defaults(gmi.audio_defaults, clean_voices(gmi.voices))
        except ValueError as e:
            raise HTTPException(400, f"'{gmi.public_name}': {e}")
        gm = GatewayModel(gateway_id=gw.id, public_name=gmi.public_name.strip(), enabled=gmi.enabled, aliases=gmi.aliases or None, voices=clean_voices(gmi.voices) or None, strategy=gmi.strategy or None,
                          sticky=gmi.sticky, system_prompt=gmi.system_prompt or None,
                          max_tokens_cap=gmi.max_tokens_cap or None, default_params=gmi.default_params or {}, audio_defaults=audio_defaults,
                          cache_enabled=gmi.cache_enabled, cache_ttl=gmi.cache_ttl or None,
                          cache_any_temperature=gmi.cache_any_temperature, compress_mode=gmi.compress_mode or None,
                          cache_streaming=gmi.cache_streaming, cache_per_key=gmi.cache_per_key, cache_replay_speed=gmi.cache_replay_speed,
                          guardrails=gmi.guardrails or None)
        s.add(gm)
        s.flush()
        for mi in gmi.members:
            if not s.get(Model, mi.model_id):
                raise HTTPException(400, f"Unknown model id {mi.model_id}")
            s.add(Member(gateway_model_id=gm.id, **mi.model_dump()))


@router.get("/gateways")
def list_gateways(all: bool = False, s: Session = Depends(get_session)):
    # the hidden gateway that holds the unified endpoint's own models is only listed on request
    return [gw_out(g, s) for g in s.exec(select(Gateway)).all() if all or g.slug not in HIDDEN_SLUGS]


@router.get("/gateways/{gw_id}")
def get_gateway(gw_id: int, s: Session = Depends(get_session)):
    gw = s.get(Gateway, gw_id)
    if not gw:
        raise HTTPException(404)
    return gw_out(gw, s)


@router.post("/gateways")
def create_gateway(body: GatewayIn, s: Session = Depends(get_session)):
    validate(body, s)
    slug = slugify(body.slug or body.name)
    if s.exec(select(Gateway).where(Gateway.slug == slug)).first():
        raise HTTPException(400, f"Slug '{slug}' already in use")
    gw = Gateway(slug=slug, name=body.name, strategy=body.strategy,
                 queue_timeout=body.queue_timeout, max_retries=body.max_retries,
                 kind=body.kind, council=body.council if body.kind == "council" else None)
    s.add(gw)
    s.flush()
    apply_models(gw, body, s)
    s.commit()
    s.refresh(gw)
    if gw.kind == "council":
        council.sync_models()
    return gw_out(gw, s)


@router.put("/gateways/{gw_id}")
def update_gateway(gw_id: int, body: GatewayIn, s: Session = Depends(get_session)):
    gw = s.get(Gateway, gw_id)
    if not gw:
        raise HTTPException(404)
    if body.kind != gw.kind:
        raise HTTPException(400, "A gateway cannot change between router and council")
    validate(body, s, gw_id)
    if gw.slug in HIDDEN_SLUGS:
        raise HTTPException(400, "This gateway is managed internally")
    slug = slugify(body.slug or gw.slug)
    other = s.exec(select(Gateway).where(Gateway.slug == slug)).first()
    if other and other.id != gw.id:
        raise HTTPException(400, f"Slug '{slug}' already in use")
    gw.slug, gw.name = slug, body.name
    gw.strategy, gw.queue_timeout, gw.max_retries = body.strategy, body.queue_timeout, body.max_retries
    gw.council = body.council if gw.kind == "council" else None
    s.add(gw)
    apply_models(gw, body, s)
    s.commit()
    s.refresh(gw)
    if gw.kind == "council":
        council.sync_models()
    return gw_out(gw, s)


@router.delete("/gateways/{gw_id}")
def delete_gateway(gw_id: int, force: bool = False, s: Session = Depends(get_session)):
    gw = s.get(Gateway, gw_id)
    if not gw:
        raise HTTPException(404)
    users = council.used_by(s, gw_id)
    if users and not force:
        raise HTTPException(409, detail={"message": f"Used by council gateways: {', '.join(users)}", "used_by": users})
    if gw.slug in HIDDEN_SLUGS:
        raise HTTPException(400, "This gateway is managed internally")
    apply_models(gw, GatewayIn(name=gw.name, models=[]), s)
    for k in s.exec(select(ApiKey).where(ApiKey.gateway_id == gw_id)).all():
        s.delete(k)
    was_council = gw.kind == "council"
    s.delete(gw)
    s.commit()
    if was_council:
        council.sync_models()
    return {"ok": True}


# ---------------------------------------------------------------- API keys
class KeyIn(BaseModel):
    name: str
    enabled: bool = True
    expires_at: Optional[float] = None
    rpm_limit: Optional[int] = None
    tpm_limit: Optional[int] = None
    daily_token_quota: Optional[int] = None
    monthly_token_quota: Optional[int] = None
    max_concurrent: Optional[int] = None
    allowed_models: list[str] = []


def key_out(k: ApiKey, s: Session) -> dict:
    d = k.model_dump(exclude={"key_hash"})
    u = limiter.usage(k.id)
    d["usage"] = u
    d["requests_24h"] = len(s.exec(select(RequestLog.id).where(RequestLog.key_id == k.id, RequestLog.ts >= time.time() - 86400)).all())
    return d


@router.get("/gateways/{gw_id}/keys")
def list_keys(gw_id: int, s: Session = Depends(get_session)):
    return [key_out(k, s) for k in s.exec(select(ApiKey).where(ApiKey.gateway_id == gw_id)).all()]


@router.post("/gateways/{gw_id}/keys")
def create_key(gw_id: int, body: KeyIn, s: Session = Depends(get_session)):
    if not s.get(Gateway, gw_id):
        raise HTTPException(404)
    plain = "sg-" + secrets.token_urlsafe(24)
    k = ApiKey(gateway_id=gw_id, key_hash=hash_key(plain), key_prefix=plain[:7] + "…", **body.model_dump())
    s.add(k)
    s.commit()
    s.refresh(k)
    return {**key_out(k, s), "key": plain}  # the only time the plaintext is returned


@router.patch("/keys/{key_id}")
def update_key(key_id: int, body: KeyIn, s: Session = Depends(get_session)):
    k = s.get(ApiKey, key_id)
    if not k:
        raise HTTPException(404)
    for f, v in body.model_dump().items():
        setattr(k, f, v)
    s.add(k)
    s.commit()
    s.refresh(k)
    return key_out(k, s)


@router.delete("/keys/{key_id}")
def delete_key(key_id: int, s: Session = Depends(get_session)):
    k = s.get(ApiKey, key_id)
    if not k:
        raise HTTPException(404)
    s.delete(k)
    s.commit()
    return {"ok": True}
