"""Runtime settings kept in the DB (editable from the UI). Secrets are encrypted at rest."""
import json
import os
import re
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from sqlmodel import select

from . import crypto
from .db import session
from .models import Setting

DEFAULTS: dict[str, Any] = {
    "redis_enabled": False,
    "redis_url": "",                 # plain host URL, e.g. redis://host:6379/0 (credentials live in the fields below)
    "redis_username": "",
    "redis_password": "",            # secret: stored encrypted, never returned by the API
    "redis_key_prefix": "sg:",
    "cache_default_ttl": 600,
    "cache_max_entry_kb": 512,
    "cache_fallback_memory": True,
    "compress_mode": "off",          # off | shadow | on  (global default; per-model override possible)
    "compress_config": {},           # overrides of ContentRouterConfig (partial dict)
    "context_insights_enabled": True,
    "ip_rpm": None,                  # requests/minute per client IP on the public gateway URLs; None = SG_IP_RPM (default 0 = off), 0 = off
    "log_retention_days": 0,         # 0 = keep forever
    "compression_retention_days": 0,
    "stress_retention_days": 0,
    "audit_retention_days": 0,
    "council_retention_days": 0,
    "mcp_retention_days": 0,
    "unified_enabled": False,        # expose several gateways as one endpoint (/unified/v1), model name = gateway name
    "unified_keys": [],              # API keys of the unified endpoint: [{id, name, key_hash, prefix, created_at}] (hashed, plaintext shown once)
    "unified_models": [],            # [{"model_id": int, "name": public name}]: upstream models exposed directly (backed by a hidden internal gateway)
    "unified_gateways": [],          # [{"gateway_id": int, "model": optional default public model}]
}
SECRETS = {"redis_password"}


def _read_all() -> dict[str, Any]:
    out: dict[str, Any] = {}
    with session() as s:
        for row in s.exec(select(Setting)).all():
            try:
                v = json.loads(row.value)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(v, str) and v.startswith(crypto.PREFIX):  # secrets, and URLs saved encrypted by older versions
                try:
                    v = crypto.decrypt(v)
                except ValueError:
                    v = ""
            out[row.key] = v
    return out


def get_all() -> dict[str, Any]:
    """Effective settings: defaults < SG_REDIS_URL env (when nothing saved yet) < saved values."""
    saved = _read_all()
    eff = {**DEFAULTS}
    env_url = os.environ.get("SG_REDIS_URL", "").strip()
    if env_url and "redis_url" not in saved:
        eff["redis_url"] = env_url
        eff["redis_enabled"] = True
    eff.update({k: v for k, v in saved.items() if k in DEFAULTS})
    # credentials embedded in the URL (env var, or saved by older versions) are split into the separate fields
    clean, user, pw = split_url(eff["redis_url"])
    if clean != eff["redis_url"]:
        eff["redis_url"] = clean
        eff["redis_username"] = eff["redis_username"] or user
        eff["redis_password"] = eff["redis_password"] or pw
    return eff


_cached: dict = {"t": 0.0, "v": None}


def get_cached(ttl: float = 3.0) -> dict[str, Any]:
    """get_all() with a short TTL for hot paths (the request pipeline); invalidated on update()."""
    import time
    if _cached["v"] is None or time.time() - _cached["t"] > ttl:
        _cached["v"], _cached["t"] = get_all(), time.time()
    return _cached["v"]


def source_of_url() -> str:
    saved = _read_all()
    if "redis_url" in saved:
        return "ui"
    return "env" if os.environ.get("SG_REDIS_URL", "").strip() else "none"


def update(values: dict[str, Any]) -> None:
    with session() as s:
        for k, v in values.items():
            if k not in DEFAULTS:
                continue
            stored = crypto.encrypt(v) if (k in SECRETS and isinstance(v, str) and v) else v
            row = s.get(Setting, k) or Setting(key=k)
            row.value = json.dumps(stored)
            s.add(row)
        s.commit()
    _cached["v"] = None


def split_url(url: str) -> tuple[str, str, str]:
    """redis://user:pass@host:6379/0 -> ("redis://host:6379/0", "user", "pass"). URLs without credentials are returned unchanged."""
    try:
        u = urlsplit(url or "")
        if u.scheme not in ("redis", "rediss") or (u.username is None and u.password is None):
            return url, "", ""
        host = u.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if u.port:
            host += f":{u.port}"
        clean = urlunsplit((u.scheme, host, u.path, u.query, u.fragment))
        return clean, unquote(u.username or ""), unquote(u.password or "")
    except ValueError:
        return url, "", ""


def redact_url(url: str) -> str:
    """Hide the password in a redis URL."""
    return re.sub(r"(://[^:/@]*:)[^@]*@", r"\1***@", url or "")


def describe_url(url: str) -> str:
    """host:port/db without credentials, for display."""
    if not url:
        return ""
    try:
        u = urlsplit(url)
        if u.scheme == "unix":
            return f"unix:{u.path}"
        db = (u.path or "/0").lstrip("/") or "0"
        return f"{u.scheme}://{u.hostname}:{u.port or 6379}/{db}"
    except Exception:  # noqa: BLE001
        return "(invalid url)"
