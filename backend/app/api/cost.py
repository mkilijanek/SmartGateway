"""Cost tracking: what the traffic cost (tokens x price), how full the plan limits are, and the price table behind both."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Integer, cast, func
from sqlmodel import Session, select

from .. import pricing
from ..accounts import limits as plan_limits
from ..api import endpoints as endpoints_api
from ..db import get_session
from ..models import Endpoint, Gateway, Model, RequestLog

router = APIRouter(prefix="/api/cost")
# how an endpoint's traffic is billed: pay as you go (real money per token) or a subscription (a flat fee: shown as "API-equivalent value")
BILLING = {"opencode-zen": "payg", "opencode-go": "subscription", "codex-oauth": "subscription", "claude-oauth": "subscription"}
MAX_DAYS = 365


class PriceIn(BaseModel):
    price_in: float = Field(ge=0, le=100000)           # USD per 1M input tokens
    price_out: float = Field(ge=0, le=100000)          # USD per 1M output tokens
    price_cache_read: Optional[float] = Field(default=None, ge=0, le=100000)   # USD per 1M cached input tokens (default: the input price)


def _kind(provider: Optional[str], price: Optional[dict]) -> str:
    if price is None:
        return "unpriced"
    if price["in"] == 0 and price["out"] == 0:
        return "free"
    return BILLING.get(provider or "", "payg")


def _round(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 6)


@router.get("/summary")
def summary(days: int = Query(7, ge=1, le=MAX_DAYS), s: Session = Depends(get_session)):
    since = time.time() - days * 86400
    models = {(m.endpoint_id, m.upstream_id): m for m in s.exec(select(Model)).all()}
    eps = {e.id: e for e in s.exec(select(Endpoint)).all()}
    gws = {g.id: g for g in s.exec(select(Gateway)).all()}
    hour = cast(RequestLog.ts / 3600, Integer)
    rows = s.exec(
        select(hour, RequestLog.endpoint_id, RequestLog.upstream_model, RequestLog.gateway_id, func.count(), func.sum(RequestLog.prompt_tokens),
               func.sum(RequestLog.completion_tokens), func.sum(RequestLog.cached_tokens))
        .where(RequestLog.ts >= since, RequestLog.ok == True, RequestLog.cached == False, RequestLog.endpoint_id.is_not(None))   # noqa: E712
        .group_by(hour, RequestLog.endpoint_id, RequestLog.upstream_model, RequestLog.gateway_id)).all()
    hits = s.exec(select(func.count()).where(RequestLog.ts >= since, RequestLog.cached == True)).one()   # noqa: E712

    tot = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "payg_cost": 0.0, "subscription_value": 0.0,
           "unpriced_requests": 0, "unpriced_tokens": 0, "gateway_cache_hits": hits}
    by_model: dict = {}
    by_gw: dict = {}
    daily: dict = {}
    for h, ep_id, up, gw_id, n, p, c, cached in rows:
        p, c, cached = int(p or 0), int(c or 0), int(cached or 0)
        ep, m = eps.get(ep_id), models.get((ep_id, up))
        price = pricing.effective(m) if m else None
        kind = _kind(ep.provider if ep else None, price)
        usd = pricing.cost(price, p, c, cached, h * 3600 + 1800) if price else None
        tot["requests"] += n; tot["prompt_tokens"] += p; tot["completion_tokens"] += c; tot["cached_tokens"] += cached
        if usd is None:
            tot["unpriced_requests"] += n; tot["unpriced_tokens"] += p + c
        elif kind == "subscription":
            tot["subscription_value"] += usd
        else:
            tot["payg_cost"] += usd
        key = (ep_id, up)
        d = by_model.setdefault(key, {"endpoint_id": ep_id, "endpoint": ep.name if ep else f"#{ep_id}", "provider": ep.provider if ep else None, "model": up,
                                      "billing": kind, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cost": None,
                                      "price": ({k: price[k] for k in ("in", "out", "cache_read", "source")} | {"has_rules": bool(price["rules"])}) if price else None})
        d["requests"] += n; d["prompt_tokens"] += p; d["completion_tokens"] += c; d["cached_tokens"] += cached
        if usd is not None:
            d["cost"] = (d["cost"] or 0.0) + usd
        g = by_gw.setdefault(gw_id, {"gateway_id": gw_id, "name": "Unified endpoint" if gws.get(gw_id) and gws[gw_id].slug == "__unified__" else (gws[gw_id].name if gws.get(gw_id) else f"#{gw_id}"),
                                     "requests": 0, "tokens": 0, "payg_cost": 0.0, "subscription_value": 0.0})
        g["requests"] += n; g["tokens"] += p + c
        if usd is not None:
            g["subscription_value" if kind == "subscription" else "payg_cost"] += usd
        day = datetime.fromtimestamp(h * 3600, timezone.utc).strftime("%Y-%m-%d")
        dd = daily.setdefault(day, {"day": day, "requests": 0, "tokens": 0, "payg_cost": 0.0, "subscription_value": 0.0, "by_endpoint": {}})
        dd["requests"] += n; dd["tokens"] += p + c
        if usd is not None:
            dd["subscription_value" if kind == "subscription" else "payg_cost"] += usd
            dd["by_endpoint"][ep.name if ep else f"#{ep_id}"] = dd["by_endpoint"].get(ep.name if ep else f"#{ep_id}", 0.0) + usd

    for d in by_model.values():
        d["cost"] = _round(d["cost"])
    # a continuous day axis (UTC), so quiet days show as empty bars; very long ranges list only the days that have traffic
    empty = lambda day: {"day": day, "requests": 0, "tokens": 0, "payg_cost": 0.0, "subscription_value": 0.0, "by_endpoint": {}}  # noqa: E731
    if days <= 120:
        today = datetime.now(timezone.utc).date()
        series = [daily.get(d) or empty(d) for d in ((today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days, -1, -1))]
    else:
        series = sorted(daily.values(), key=lambda x: x["day"])
    for d in series:
        d["payg_cost"], d["subscription_value"] = _round(d["payg_cost"]), _round(d["subscription_value"])
        d["by_endpoint"] = {k: _round(v) for k, v in d["by_endpoint"].items()}
    for g in by_gw.values():
        g["payg_cost"], g["subscription_value"] = _round(g["payg_cost"]), _round(g["subscription_value"])
    tot["payg_cost"], tot["subscription_value"] = _round(tot["payg_cost"]), _round(tot["subscription_value"])
    oldest = s.exec(select(func.min(RequestLog.ts))).one()
    return {"days": days, "since": since, "history_from": oldest, "totals": tot, "daily": series,
            "by_model": sorted(by_model.values(), key=lambda x: -((x["cost"] or 0) * 1e6 + x["prompt_tokens"] + x["completion_tokens"])),
            "by_gateway": sorted(by_gw.values(), key=lambda x: -x["tokens"])}


def _local_usage(s: Session, endpoint_id: int, seconds: int) -> dict:
    r = s.exec(select(func.count(), func.sum(RequestLog.prompt_tokens), func.sum(RequestLog.completion_tokens))
               .where(RequestLog.endpoint_id == endpoint_id, RequestLog.ts >= time.time() - seconds, RequestLog.ok == True, RequestLog.cached == False)).one()   # noqa: E712
    return {"requests": int(r[0] or 0), "tokens": int(r[1] or 0) + int(r[2] or 0)}


@router.get("/limits")
async def limits(refresh: bool = False, s: Session = Depends(get_session)):
    """Plan-limit windows for every subscription / hosted-plan endpoint, plus what went through this gateway in the same window."""
    eps = [e for e in s.exec(select(Endpoint)).all() if e.provider in ("opencode-go", "opencode-zen", "codex-oauth", "claude-oauth")]
    infos = await asyncio.gather(*(plan_limits.limits_for(e.id, refresh) for e in eps))
    out = []
    for e, info in zip(eps, infos):
        if info is None:
            continue
        info = {**info, "windows": [dict(w) for w in info["windows"]]}
        for w in info["windows"]:
            w["via_gateway"] = _local_usage(s, e.id, plan_limits.SECONDS.get(w["key"], 7 * 86400))
        info["via_gateway_30d"] = _local_usage(s, e.id, 30 * 86400)
        info["billing"] = BILLING.get(e.provider, "payg")
        out.append(info)
    return out


@router.get("/prices")
def prices(s: Session = Depends(get_session)):
    out = []
    for e in s.exec(select(Endpoint)).all():
        rows = []
        for m in s.exec(select(Model).where(Model.endpoint_id == e.id)).all():
            p = pricing.effective(m)
            rows.append({"model_id": m.id, "model": m.upstream_id, "enabled": m.enabled, "billing": _kind(e.provider, p),
                         "price": ({k: p[k] for k in ("in", "out", "cache_read", "source")} | {"has_rules": bool(p["rules"])}) if p else None,
                         "has_catalog_price": isinstance(m.catalog_cost, dict) and isinstance(m.catalog_cost.get("text"), dict)})
        out.append({"endpoint_id": e.id, "name": e.name, "provider": e.provider, "hosted_catalog": bool(e.provider and e.provider.startswith("opencode")), "models": rows})
    return out


@router.put("/prices/{model_id}")
def set_price(model_id: int, body: PriceIn, s: Session = Depends(get_session)):
    m = s.get(Model, model_id)
    if not m:
        raise HTTPException(404)
    m.price_in, m.price_out, m.price_cache_read, m.price_manual = body.price_in, body.price_out, body.price_cache_read, True
    s.add(m)
    s.commit()
    return {"ok": True, "price": {k: v for k, v in (pricing.effective(m) or {}).items() if k != "rules"}}


@router.delete("/prices/{model_id}")
def clear_price(model_id: int, s: Session = Depends(get_session)):
    """Drop a manual price: the catalog's price applies again (or none)."""
    m = s.get(Model, model_id)
    if not m:
        raise HTTPException(404)
    m.price_in = m.price_out = m.price_cache_read = None
    m.price_manual = False
    s.add(m)
    s.commit()
    return {"ok": True}


@router.post("/prices/refresh")
async def refresh_prices(s: Session = Depends(get_session)):
    """Read the hosted catalogs (OpenCode) again, which carry the current prices."""
    done, failed = 0, []
    for e in s.exec(select(Endpoint)).all():
        if e.provider and e.provider.startswith("opencode"):
            try:
                await endpoints_api.sync_models(e, s, probe=False)
                done += 1
            except Exception as ex:  # noqa: BLE001
                failed.append(f"{e.name}: {str(ex)[:80]}")
    return {"refreshed": done, "failed": failed}


# ------------------------------------------------------------------ plan usage for the Dashboard and the Endpoints page
limits_router = APIRouter(prefix="/api/limits")


def _plan_rows(s: Session) -> list:
    out = []
    for e in s.exec(select(Endpoint)).all():
        if e.provider in plan_limits.PLANNED:
            c = plan_limits.compact(plan_limits.peek(e.id))
            out.append({"endpoint_id": e.id, "name": e.name, "provider": e.provider, "status": e.status, **(c or {"windows": [], "error": None, "stale": False, "fetched_at": None, "last_ok": None, "note": "", "plan": None})})
    return out


@limits_router.get("")
def plan_usage(s: Session = Depends(get_session)):
    """The latest plan usage of every subscription / plan endpoint, as last read by the background check. Never calls a vendor."""
    return {"interval": plan_limits.INTERVAL, "polling": plan_limits.POLL, "accounts": _plan_rows(s)}


@limits_router.post("/refresh")
async def plan_usage_refresh(s: Session = Depends(get_session)):
    """Ask the vendors now (at most every 10 s per account) and return the fresh numbers."""
    for e in s.exec(select(Endpoint)).all():
        if e.provider in plan_limits.PLANNED:
            await plan_limits.limits_for(e.id, refresh=True)
    return {"interval": plan_limits.INTERVAL, "polling": plan_limits.POLL, "accounts": _plan_rows(s)}
