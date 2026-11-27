"""Plan-limit usage of the accounts and subscriptions behind endpoints, read live from each vendor (when the Cost page asks, cached for a
minute: no background polling, no quota used). The vendors' usage endpoints are not public APIs, so every fetch is best-effort: a failure is
reported as text, never raised."""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from .. import discovery
from sqlmodel import select

from ..db import session
from ..models import Endpoint
from . import AccountError, claude_adapter, claude_auth, codex_auth, tokens

log = logging.getLogger("smartgateway.limits")

TTL = 60.0
INTERVAL = max(60.0, float(os.environ.get("SG_LIMITS_INTERVAL", "300") or 300))      # how often the gateway asks the vendors (seconds, at least 60)
POLL = os.environ.get("SG_LIMITS_POLL", "1").lower() not in ("0", "false", "no")      # SG_LIMITS_POLL=0 turns the background check off
PLANNED = ("opencode-go", "codex-oauth", "claude-oauth")                              # endpoints that have usage windows to watch
OPENCODE_GO_USAGE = os.environ.get("SG_OPENCODE_GO_USAGE_URL", "https://opencode.ai/zen/go/v1/usage")
CODEX_USAGE = os.environ.get("SG_CODEX_USAGE_URL", "https://chatgpt.com/backend-api/wham/usage")
CLAUDE_USAGE_PATH = "/api/oauth/usage"
LABELS = {"five_hour": "5 hours", "seven_day": "Weekly (7 days)", "monthly": "Monthly",
          "seven_day_opus": "Weekly · Opus", "seven_day_sonnet": "Weekly · Sonnet", "seven_day_oauth_apps": "Weekly · apps"}
SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400, "monthly": 30 * 86400}
_cache: dict[int, tuple[float, dict]] = {}


def _iso(v) -> Optional[str]:
    """resets_at as an ISO string, from an ISO string or epoch seconds/milliseconds."""
    try:
        if isinstance(v, (int, float)) and v > 0:
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc).isoformat()
        if isinstance(v, str) and v:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _window(key: str, percent, resets) -> Optional[dict]:
    if not isinstance(percent, (int, float)) or isinstance(percent, bool):
        return None
    return {"key": key, "label": LABELS.get(key, key), "percent": round(min(100.0, max(0.0, float(percent))), 1), "resets_at": _iso(resets)}


def parse_opencode_go(d) -> list:
    u = (d or {}).get("usage") if isinstance(d, dict) else None
    out = []
    for key, src in (("five_hour", "rolling"), ("seven_day", "weekly"), ("monthly", "monthly")):
        w = (u or {}).get(src) if isinstance(u, dict) else None
        if isinstance(w, dict) and (x := _window(key, w.get("percent"), w.get("resetsAt"))):
            out.append(x)
    return out


def parse_codex(d) -> tuple[list, dict]:
    rl = (d or {}).get("rate_limit") if isinstance(d, dict) else None
    out = []
    for w in (rl or {}).values() if isinstance(rl, dict) else []:
        if not isinstance(w, dict):
            continue
        secs = w.get("limit_window_seconds")
        key = "five_hour" if isinstance(secs, (int, float)) and 0 < secs <= SECONDS["five_hour"] else "seven_day" if isinstance(secs, (int, float)) and secs >= SECONDS["seven_day"] else None
        resets = w.get("reset_at") or (time.time() + w["reset_after_seconds"] if isinstance(w.get("reset_after_seconds"), (int, float)) else None)
        if key and not any(o["key"] == key for o in out) and (x := _window(key, w.get("used_percent"), resets)):
            out.append(x)
    extra = {}
    cr = (d or {}).get("credits") if isinstance(d, dict) else None
    if isinstance(cr, dict):
        extra["credits"] = {k: cr.get(k) for k in ("has_credits", "unlimited", "balance") if k in cr}
    if isinstance(d, dict) and d.get("plan_type"):
        extra["plan"] = str(d["plan_type"])
    return out, extra


def parse_claude(d) -> list:
    out = []
    for key, w in (d.items() if isinstance(d, dict) else []):
        if isinstance(w, dict) and key in LABELS and (x := _window(key, w.get("utilization"), w.get("resets_at"))):
            out.append(x)
    order = list(LABELS)
    return sorted(out, key=lambda x: order.index(x["key"]))


async def _get(url: str, headers: dict):
    from .. import proxy
    return await proxy.client.get(url, headers=headers, timeout=httpx.Timeout(8.0, connect=4.0))


async def _fetch(ep: Endpoint) -> dict:
    res = {"windows": [], "extra": {}, "note": ""}
    if ep.provider == "opencode-go":
        if not ep.api_key:
            raise AccountError("no API key stored")
        h = {"Authorization": f"Bearer {ep.api_key}", "Accept": "application/json", **discovery.provider_headers_for(ep.provider)}
        r = await _get(OPENCODE_GO_USAGE, h)
        if r.status_code != 200:
            r = await _get(ep.base_url.rstrip("/") + "/usage", h)
        if r.status_code != 200:
            raise AccountError(f"OpenCode answered HTTP {r.status_code}")
        res["windows"] = parse_opencode_go(r.json())
    elif ep.provider == "opencode-zen":
        res["note"] = "Pay-as-you-go: billed per token from your Zen credits, no usage limits."
    elif ep.provider == "codex-oauth":
        token, account_id = await tokens.fresh_access(ep.id)
        h = {"Authorization": f"Bearer {token}", "Accept": "application/json", "originator": "codex_cli_rs", "User-Agent": "codex_cli_rs/0.99.0"}
        if account_id:
            h["ChatGPT-Account-Id"] = account_id
        r = await _get(CODEX_USAGE, h)
        if r.status_code != 200:
            raise AccountError(f"ChatGPT answered HTTP {r.status_code}")
        res["windows"], res["extra"] = parse_codex(r.json())
    elif ep.provider == "claude-oauth":
        token, _ = await tokens.fresh_access(ep.id)
        h = {"Authorization": f"Bearer {token}", "Accept": "application/json", "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"}
        if claude_adapter.identity_enabled():   # the admin chose to present Claude account calls as Claude Code
            h["User-Agent"] = "claude-code/2.1.0"
        r = await _get(claude_auth.API_BASE + CLAUDE_USAGE_PATH, h)
        if r.status_code != 200:
            raise AccountError(f"Anthropic answered HTTP {r.status_code}")
        res["windows"] = parse_claude(r.json())
    if not res["windows"] and not res["note"]:
        res["note"] = "The vendor returned no usage windows."
    return res


def peek(endpoint_id: int) -> Optional[dict]:
    """The latest known limits of an endpoint, whatever their age: no call to any vendor."""
    c = _cache.get(endpoint_id)
    return c[1] if c else None


def compact(info: Optional[dict]) -> Optional[dict]:
    """What the Dashboard and the Endpoints page need (no notes, no extras)."""
    if not info:
        return None
    return {"windows": info["windows"], "error": info.get("error"), "stale": bool(info.get("stale")), "fetched_at": info.get("fetched_at"),
            "last_ok": info.get("last_ok"), "note": info.get("note") or "", "plan": (info.get("extra") or {}).get("plan")}


async def limits_for(endpoint_id: int, refresh: bool = False, force: bool = False) -> Optional[dict]:
    """Limits of one endpoint, or None when it is not a subscription / hosted-plan endpoint. `force` skips the cache (the background check).
    When a check fails the last good numbers are kept and flagged stale, so a hiccup does not blank the dashboard."""
    cached = _cache.get(endpoint_id)
    if not force and cached and time.time() - cached[0] < (10.0 if refresh else TTL):
        return cached[1]
    with session() as s:
        ep = s.get(Endpoint, endpoint_id)
        if ep is None or ep.provider not in ("opencode-go", "opencode-zen", "codex-oauth", "claude-oauth"):
            return None
        info = {"endpoint_id": ep.id, "name": ep.name, "provider": ep.provider, "windows": [], "extra": {}, "note": "", "error": None, "fetched_at": time.time(),
                "stale": False, "last_ok": None}
        s.expunge(ep)
    try:
        info.update(await _fetch(ep))
        info["last_ok"] = info["fetched_at"]
    except AccountError as e:
        info["error"] = str(e)[:200]
    except Exception as e:  # noqa: BLE001 - a timeout or a changed response must not break the page
        info["error"] = f"unavailable ({type(e).__name__})"
    prev = cached[1] if cached else None
    if info["error"] and prev and prev.get("windows"):
        info.update(windows=prev["windows"], extra=prev.get("extra") or {}, stale=True, last_ok=prev.get("last_ok") or prev["fetched_at"])
    _cache[endpoint_id] = (time.time(), info)
    return info


_failures: dict[int, int] = {}
_not_before: dict[int, float] = {}


async def poll_all(respect_backoff: bool = True) -> int:
    """Ask the vendors about every account / plan endpoint (one at a time). A failing endpoint is asked less and less often (up to hourly)."""
    with session() as s:
        ids = [e.id for e in s.exec(select(Endpoint)).all() if e.provider in PLANNED]
    n = 0
    for i in ids:
        if respect_backoff and time.time() < _not_before.get(i, 0):
            continue
        info = await limits_for(i, force=True)
        if info is None:
            continue
        n += 1
        if info["error"]:
            _failures[i] = _failures.get(i, 0) + 1
            _not_before[i] = time.time() + min(3600.0, INTERVAL * 2 ** _failures[i])
            log.info("limits of %s: %s (next try in %d s)", info["name"], info["error"], min(3600, INTERVAL * 2 ** _failures[i]))
        else:
            _failures.pop(i, None)
            _not_before.pop(i, None)
            log.info("limits of %s: %s", info["name"], ", ".join(f"{w['label']} {w['percent']}%" for w in info["windows"]) or "no windows")
    return n


async def loop() -> None:
    """Background check: shortly after start, then every SG_LIMITS_INTERVAL seconds. Reading usage costs no quota."""
    await asyncio.sleep(20)
    while True:
        try:
            await poll_all()
        except Exception:  # noqa: BLE001
            log.exception("limits check failed")
        await asyncio.sleep(INTERVAL)
