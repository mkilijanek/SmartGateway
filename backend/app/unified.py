"""Unified endpoint: several gateways exposed through ONE OpenAI-compatible API (`/unified/v1`).

The model name a client sends is the *gateway name*. A gateway with several public models is reachable as
`<gateway>` (its default model) and, additionally, as `<gateway>/<public model>`. Authentication stays with
each gateway (its own API keys, limits and quotas), so nothing new has to be managed.
"""
import secrets
import time
from typing import Optional

from sqlmodel import select

from . import settings_store as st
from .crypto import hash_key
from .db import session
from .models import ApiKey, Endpoint, Gateway, GatewayModel, Member, Model
from .voices import clean_voices, voice_id

HIDDEN_SLUGS = ("__unified__", "__council__")   # internal gateways that are never listed or addressable on their own
INTERNAL_SLUG = "__unified__"   # hidden gateway that holds the upstream models exposed directly on the unified endpoint


def build_aliases(cfg: list[dict], gateways: dict[int, Gateway], models: dict[int, list[str]], direct: Optional[list[dict]] = None) -> list[dict]:
    """Pure: turn the saved selection into the list of model names the unified endpoint serves.

    Each item: {alias, gateway (Gateway), model (public name on that gateway), default (bool)}.
    Selected gateways that no longer exist (or have no models) are skipped; clashing names get the slug appended.
    `direct`: upstream models exposed on their own, [{name, gateway (the internal Gateway), model (its public name)}].
    """
    out: list[dict] = []
    used: set[str] = set()

    def unique(name: str, gw: Gateway) -> str:
        for cand in (name, f"{name} ({gw.slug})", gw.slug):
            if cand.lower() not in used:
                used.add(cand.lower())
                return cand
        return name

    for sel in cfg or []:
        gw = gateways.get(sel.get("gateway_id"))
        names = models.get(sel.get("gateway_id")) or []
        if gw is None or not names:
            continue
        default = sel.get("model") if sel.get("model") in names else names[0]
        base = unique((gw.name or "").strip() or gw.slug, gw)
        out.append({"alias": base, "gateway": gw, "model": default, "default": True})
        for extra in sel.get("aliases") or []:   # additional names for the same (default) model
            if extra and extra.lower() not in used:
                used.add(extra.lower())
                out.append({"alias": extra, "gateway": gw, "model": default, "default": False, "extra": True})
        if len(names) > 1:
            for m in names:
                out.append({"alias": unique(f"{base}/{m}", gw), "gateway": gw, "model": m, "default": False})
    for d in direct or []:
        n = (d["name"] or "").strip()
        for cand in (n, f"{n} (model)", f"{n} (model {d['model']})"):
            if cand and cand.lower() not in used:
                used.add(cand.lower())
                out.append({"alias": cand, "gateway": d["gateway"], "model": d["model"], "default": False, "direct": True})
                for extra in d.get("aliases") or []:
                    if extra and extra.lower() not in used:
                        used.add(extra.lower())
                        out.append({"alias": extra, "gateway": d["gateway"], "model": d["model"], "default": False, "direct": True, "extra": True})
                break
    return out


def internal_gateway(s) -> Optional[Gateway]:
    return s.exec(select(Gateway).where(Gateway.slug == INTERNAL_SLUG)).first()


def sync_models(entries: list[dict]) -> None:
    """Make the internal gateway hold exactly one public model (with one upstream member) per entry {model_id, name}."""
    with session() as s:
        gw = internal_gateway(s)
        if gw is None:
            if not entries:
                return
            gw = Gateway(slug=INTERNAL_SLUG, name="Unified models", strategy="least_busy", queue_timeout=60.0, max_retries=2)
            s.add(gw)
            s.flush()
        have = {gm.public_name: gm for gm in s.exec(select(GatewayModel).where(GatewayModel.gateway_id == gw.id)).all()}
        wanted = {e["name"]: e["model_id"] for e in entries}
        off = {e["name"] for e in entries if e.get("disabled")}
        voices_of = {e["name"]: e.get("voices") for e in entries}
        audio_of = {e["name"]: e.get("audio_defaults") for e in entries}
        for name, gm in have.items():
            if name not in wanted:
                for mem in s.exec(select(Member).where(Member.gateway_model_id == gm.id)).all():
                    s.delete(mem)
                s.delete(gm)
        for name, model_id in wanted.items():
            gm = have.get(name)
            if gm is None:
                gm = GatewayModel(gateway_id=gw.id, public_name=name)
                s.add(gm)
                s.flush()
            if gm.enabled == (name in off):   # keep the internal model in step with the "disabled" switch
                gm.enabled = name not in off
                s.add(gm)
            voices = clean_voices(voices_of.get(name)) or None   # TTS: the voices offered by GET /audio/voices (None = whatever the upstream reports)
            if (gm.voices or None) != voices:
                gm.voices = voices
                s.add(gm)
            audio = audio_of.get(name) or None   # audio defaults of a directly exposed audio model
            if (gm.audio_defaults or None) != audio:
                gm.audio_defaults = audio
                s.add(gm)
            mems = s.exec(select(Member).where(Member.gateway_model_id == gm.id)).all()
            if len(mems) == 1 and mems[0].model_id == model_id:
                continue
            for mem in mems:
                s.delete(mem)
            s.flush()
            s.add(Member(gateway_model_id=gm.id, model_id=model_id))
        s.commit()


def exposed(settings: Optional[dict] = None) -> list[dict]:
    cfg_all = settings or st.get_cached()
    cfg = cfg_all.get("unified_gateways") or []
    direct_cfg = cfg_all.get("unified_models") or []
    if not cfg and not direct_cfg:
        return []
    with session() as s:
        gws = {g.id: g for g in s.exec(select(Gateway)).all()}
        models: dict[int, list[str]] = {}
        for gm in s.exec(select(GatewayModel).where(GatewayModel.enabled == True).order_by(GatewayModel.id)).all():  # noqa: E712
            models.setdefault(gm.gateway_id, []).append(gm.public_name)
    internal = next((g for g in gws.values() if g.slug == INTERNAL_SLUG), None)
    direct = [{"name": e["name"], "gateway": internal, "model": e["name"], "aliases": e.get("aliases") or []} for e in direct_cfg
              if internal is not None and e.get("name") in models.get(internal.id, [])]
    return build_aliases(cfg, gws, models, direct)


def resolve(wanted: str, aliases: list[dict]) -> Optional[dict]:
    """Exact name, then case-insensitive, then the gateway slug (-> its default model)."""
    w = (wanted or "").strip()
    if not w:
        return None
    for a in aliases:
        if a["alias"] == w:
            return a
    low = w.lower()
    for a in aliases:
        if a["alias"].lower() == low:
            return a
    for a in aliases:
        if a["default"] and a["gateway"].slug.lower() == low:
            return a
    return None


def has_keys(gw_id: int) -> bool:
    with session() as s:
        gw = s.get(Gateway, gw_id)
        return bool(gw and (gw.api_key or s.exec(select(ApiKey).where(ApiKey.gateway_id == gw_id)).first()))


# ---- API keys of the unified endpoint
def keys(settings: Optional[dict] = None) -> list[dict]:
    return list((settings or st.get_cached()).get("unified_keys") or [])


def match_key(token: str, settings: Optional[dict] = None) -> Optional[dict]:
    if not token:
        return None
    h = hash_key(token)
    return next((k for k in keys(settings) if secrets.compare_digest(k.get("key_hash", ""), h)), None)


def make_key(name: str) -> tuple[dict, str]:
    """-> (stored record without plaintext, plaintext). The plaintext is only ever returned once."""
    plain = "sgu-" + secrets.token_urlsafe(24)
    return {"id": secrets.token_hex(4), "name": name, "key_hash": hash_key(plain), "prefix": plain[:8] + "…", "created_at": time.time()}, plain


def access(token: str, settings: Optional[dict] = None) -> str:
    """How a caller may use the unified endpoint:
    "all"     a valid unified key: every exposed gateway, regardless of the gateways' own keys
    "gateway" no unified key needed (none configured) or the caller sent some other token: each gateway checks its own key
    "denied"  unified keys are configured and the caller sent nothing: anonymous access is closed, even for open gateways
    """
    if match_key(token, settings):
        return "all"
    if keys(settings) and not token:
        return "denied"
    return "gateway"
