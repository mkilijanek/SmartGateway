import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from .. import config, settings_store as st
from ..db import get_session
from ..models import RequestLog
from ..router.cache import RedisBackend, _safe, cache

router = APIRouter(prefix="/api")


class SettingsIn(BaseModel):
    redis_enabled: Optional[bool] = None
    redis_url: Optional[str] = None       # None = keep, "" = remove the Redis configuration (also clears username/password)
    redis_username: Optional[str] = None  # None = keep, "" = clear
    redis_password: Optional[str] = None  # None = keep, "" = clear (never returned by the API)
    redis_key_prefix: Optional[str] = None
    cache_default_ttl: Optional[int] = None
    cache_max_entry_kb: Optional[int] = None
    cache_fallback_memory: Optional[bool] = None
    log_retention_days: Optional[int] = None          # 0 = keep forever
    compression_retention_days: Optional[int] = None
    stress_retention_days: Optional[int] = None
    audit_retention_days: Optional[int] = None
    council_retention_days: Optional[int] = None
    mcp_retention_days: Optional[int] = None
    ip_rpm: Optional[int] = None                       # requests/minute per client IP on public gateway URLs; 0 = off, -1 = back to SG_IP_RPM
    password_hash_log_n: Optional[int] = None          # 14..16: scrypt cost for the UI-chosen admin password


class TestIn(BaseModel):
    url: Optional[str] = None       # None/"" = the saved URL
    username: Optional[str] = None  # None = the saved username
    password: Optional[str] = None  # None = the saved password (only when testing the saved URL), "" = no password


RETENTION_KEYS = ("log_retention_days", "compression_retention_days", "stress_retention_days", "audit_retention_days", "council_retention_days", "mcp_retention_days")


def view() -> dict:
    s = st.get_all()
    return {
        "redis_enabled": s["redis_enabled"], "redis_url": s["redis_url"], "redis_username": s["redis_username"],
        "has_password": bool(s["redis_password"]), "has_url": bool(s["redis_url"]),
        "redis_url_source": st.source_of_url(), "redis_key_prefix": s["redis_key_prefix"],
        "cache_default_ttl": s["cache_default_ttl"], "cache_max_entry_kb": s["cache_max_entry_kb"],
        "cache_fallback_memory": s["cache_fallback_memory"],
        **{k: s[k] for k in RETENTION_KEYS},
        "ip_rpm": s["ip_rpm"], "ip_rpm_env": int(os.environ.get("SG_IP_RPM", "0") or 0),
        "password_hash_log_n": s["password_hash_log_n"], "password_source": config.PASSWORD_SOURCE,
    }


def _valid_url(url: str) -> bool:
    return bool(re.match(r"^(redis|rediss|unix)://", url))


@router.get("/settings")
def get_settings():
    return view()


@router.put("/settings")
async def put_settings(body: SettingsIn):
    vals = {k: v for k, v in body.model_dump().items() if v is not None}
    if "redis_url" in vals:
        url = vals["redis_url"].strip()
        if not url:  # removing the configuration removes its credentials too
            vals.update(redis_url="", redis_username=vals.get("redis_username", ""), redis_password=vals.get("redis_password", ""))
        else:
            if not _valid_url(url):
                raise HTTPException(400, "Redis URL must start with redis://, rediss:// or unix://")
            clean, user, pw = st.split_url(url)  # accept redis://user:pass@host too: credentials go to their own fields
            vals["redis_url"] = clean
            if user and "redis_username" not in vals:
                vals["redis_username"] = user
            if pw and "redis_password" not in vals:
                vals["redis_password"] = pw
    if "redis_key_prefix" in vals and not re.match(r"^[A-Za-z0-9_.:-]{1,40}$", vals["redis_key_prefix"]):
        raise HTTPException(400, "Key prefix may contain letters, digits and _ . : - (max 40 chars)")
    if "cache_default_ttl" in vals and not (1 <= vals["cache_default_ttl"] <= 30 * 86400):
        raise HTTPException(400, "Default TTL must be between 1 second and 30 days")
    if "cache_max_entry_kb" in vals and not (1 <= vals["cache_max_entry_kb"] <= 65536):
        raise HTTPException(400, "Max entry size must be between 1 KB and 64 MB")
    for k in RETENTION_KEYS:
        if k in vals and not (0 <= vals[k] <= 3650):
            raise HTTPException(400, "Retention must be between 0 (keep forever) and 3650 days")
    if "ip_rpm" in vals:
        if not (-1 <= vals["ip_rpm"] <= 1_000_000):
            raise HTTPException(400, "The per-IP limit must be 0 (off) or a number of requests per minute; -1 returns to SG_IP_RPM")
        if vals["ip_rpm"] == -1:
            vals["ip_rpm"] = None
    if "password_hash_log_n" in vals and not (config.MIN_LOG_N <= vals["password_hash_log_n"] <= config.MAX_LOG_N):
        raise HTTPException(400, f"The password hash cost must be between {config.MIN_LOG_N} and {config.MAX_LOG_N}")
    merged = {**st.get_all(), **vals}
    if merged["redis_enabled"] and not merged["redis_url"]:
        raise HTTPException(400, "Enter a Redis URL before enabling Redis")
    st.update(vals)
    await cache.reconfigure(st.get_all())
    return {**view(), "cache": await cache.stats()}


@router.post("/settings/redis/test")
async def test_redis(body: TestIn):
    stored = st.get_all()
    url = (body.url or "").strip() or stored["redis_url"]
    if not url:
        return {"ok": False, "error": "No Redis URL configured"}
    if not _valid_url(url):
        return {"ok": False, "error": "URL must start with redis://, rediss:// or unix://"}
    url, u_in_url, p_in_url = st.split_url(url)
    user = body.username if body.username is not None else (u_in_url or stored["redis_username"])
    if body.password is not None:
        password = body.password
    elif p_in_url:
        password = p_in_url
    else:  # the saved password is only ever tried against the saved server
        password = stored["redis_password"] if url == stored["redis_url"] else ""
    rb = None
    try:
        rb = RedisBackend(url, "sg:", username=user, password=password)
        lat, ver = await rb.ping()
        return {"ok": True, "latency_ms": round(lat, 1), "version": ver, "target": url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _safe(e, (password,)), "target": url}
    finally:
        if rb:
            await rb.close()


@router.get("/cache/stats")
async def cache_stats(s: Session = Depends(get_session)):
    out = await cache.stats()
    since = time.time() - 86400
    q = select(func.count(RequestLog.id), func.coalesce(func.sum(RequestLog.prompt_tokens + RequestLog.completion_tokens), 0)) \
        .where(RequestLog.cached == True, RequestLog.ts >= since)  # noqa: E712
    hits, saved = s.exec(q).one()
    out.update({"hits_24h": int(hits or 0), "tokens_saved_24h": int(saved or 0)})
    return out


@router.post("/cache/clear")
async def cache_clear():
    return await cache.clear()
