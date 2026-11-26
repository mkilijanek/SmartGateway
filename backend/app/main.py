import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlmodel import select

from pydantic import BaseModel

from . import audit, config, crypto, proxy, retention
from .bodylimit import BodyLimit
from .api import compression as compression_api, mcp_endpoints as mcp_endpoints_api, mcp_gateways as mcp_gateways_api, content_filters as content_filters_api, endpoints, gateways, insights as insights_api, maintenance as maintenance_api, settings as settings_api, stats, stress as stress_api, unified as unified_api
from .db import engine, init_db, session
from .mcp import registry as mcp_registry, server as mcp_server
from .models import Endpoint, McpEndpoint
from .router import health


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log = logging.getLogger("smartgateway")
    if config.GENERATED_ADMIN_PASSWORD and config.GENERATED_ADMIN_PASSWORD_IS_NEW:
        print(f"SmartGateway: SG_ADMIN_PASSWORD is not set, so a random admin password was generated (user '{os.environ.get('SG_ADMIN_USER', 'admin')}'): "
              f"{config.ADMIN_PASSWORD}  (shown only this once; also stored in {config.DATA_DIR}/admin_password; set SG_ADMIN_PASSWORD or change it in Settings)", flush=True)
    elif config.GENERATED_ADMIN_PASSWORD:
        print(f"SmartGateway: using the generated admin password stored in {config.DATA_DIR}/admin_password (user '{os.environ.get('SG_ADMIN_USER', 'admin')}'); "
              "change it in Settings -> Admin password or set SG_ADMIN_PASSWORD", flush=True)
    elif not config.ADMIN_PASSWORD:
        log.warning("SG_ALLOW_OPEN_ADMIN is set: the web UI and management API are open to anyone who can reach this port")
    with session() as s:
        for e in s.exec(select(Endpoint)).all():
            health.status[e.id] = e.status
        for m in s.exec(select(McpEndpoint)).all():
            mcp_registry.status[m.id] = m.status
    from . import council, settings_store
    council.sync_models()   # drops the hidden council gateway when no council uses endpoint models
    from .router.cache import cache
    from .router.scheduler import scheduler
    await cache.reconfigure(settings_store.get_all())
    tasks = [asyncio.create_task(health.loop()), asyncio.create_task(health.slot_sync_loop()),
             asyncio.create_task(scheduler.run_ticker()), asyncio.create_task(retention.loop()), asyncio.create_task(mcp_registry.loop())]
    yield
    for t in tasks:
        t.cancel()
    await cache.redis.close() if cache.redis else None
    await proxy.client.aclose()


config.ensure_admin_password()
app = FastAPI(title="SmartGateway", lifespan=lifespan)


COOKIE = "sg_session"
SESSION_TTL = 7 * 86400
_fails: dict[str, list] = {}


def _sign(exp: int) -> str:
    # the password is part of the signed text, so changing SG_ADMIN_PASSWORD signs every existing session out
    pw = hmac.new(crypto._load_secret(), b"pw:" + config.ADMIN_PASSWORD.encode(), hashlib.sha256).hexdigest()   # keyed, not a bare hash of the password
    return hmac.new(crypto._load_secret(), f"admin:{exp}:{pw}".encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    exp = int(time.time()) + SESSION_TTL
    return f"{exp}.{_sign(exp)}"


def valid_session(token: str | None) -> bool:
    try:
        exp, sig = (token or "").split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _sign(int(exp)))
    except Exception:  # noqa: BLE001
        return False


def request_authed(headers, cookies) -> bool:
    if not config.ADMIN_PASSWORD:
        return True
    if valid_session(cookies.get(COOKIE)):
        return True
    h = headers.get("authorization", "")
    try:
        if h.lower().startswith("bearer "):
            return config.verify_password(h[7:])
        if h.lower().startswith("basic "):
            return config.verify_password(base64.b64decode(h[6:]).decode().split(":", 1)[1])
    except Exception:  # noqa: BLE001
        return False
    return False


_ip_hits: dict[str, deque] = {}
MAX_TRACKED_IPS = 5000
IP_RPM = int(os.environ.get("SG_IP_RPM", "0") or 0)   # optional per-client-IP limit (requests/minute) on the public gateway URLs; 0 = off


def _ip_limited(ip: str) -> bool:
    """Sliding one-minute window per client IP. Protects open gateways (no API key) from being hammered; keyed gateways have their own limits.
    Only accepted requests are remembered, so a deque never holds more than IP_RPM entries and the table is capped at MAX_TRACKED_IPS addresses."""
    now = time.time()
    w = _ip_hits.get(ip)
    if w is None:
        if len(_ip_hits) >= MAX_TRACKED_IPS:
            for k in [k for k, v in _ip_hits.items() if not v or now - v[-1] >= 60]:   # forget idle addresses first ...
                _ip_hits.pop(k, None)
            while len(_ip_hits) >= MAX_TRACKED_IPS:                                    # ... then the oldest ones, so the cap is hard
                _ip_hits.pop(next(iter(_ip_hits)))
        w = _ip_hits[ip] = deque()
    while w and now - w[0] >= 60:
        w.popleft()
    if len(w) >= IP_RPM:
        return True
    w.append(now)
    return False


def _locked(ip: str) -> bool:
    """True while this client has too many recent failed admin logins (shared by the login form and Basic/Bearer API calls)."""
    now = time.time()
    if len(_fails) > 1000:   # forget old entries so the table cannot grow without bound
        for k in [k for k, v in _fails.items() if now - v[1] > 300]:
            _fails.pop(k, None)
    f = _fails.get(ip)
    if f and now - f[1] > 300:
        _fails.pop(ip, None)
        return False
    return bool(f and f[0] >= 5)


def _fail(ip: str) -> None:
    f = _fails.setdefault(ip, [0, time.time()])
    f[0] += 1


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; "
       "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
ALLOWED_ORIGINS = [o.strip().rstrip("/").lower() for o in os.environ.get("SG_ALLOWED_ORIGINS", "").split(",") if o.strip()]   # extra origins allowed to call the admin API (rarely needed)


def origin_ok(headers) -> bool:
    """Browsers always send Origin on cross-origin and on state-changing requests. If it is present it must be this site (same host:port as the
    Host / X-Forwarded-Host the client used, whatever the scheme, since TLS is often terminated by a proxy) or an explicitly allowed origin.
    No Origin = not a browser form/fetch (curl, scripts), which cookie-based CSRF cannot use."""
    origin = (headers.get("origin") or "").strip()
    if not origin:
        return True
    if origin.lower().rstrip("/") in ALLOWED_ORIGINS:
        return True
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip().lower()
    try:
        return bool(host) and urlsplit(origin).netloc.lower() == host
    except ValueError:
        return False


CORS_ORIGINS = [o.strip() for o in os.environ.get("SG_CORS_ORIGINS", "").split(",") if o.strip()]


@app.middleware("http")
async def guard(request: Request, call_next):
    p = request.url.path
    is_gateway = p.startswith(("/gw/", "/v1/", "/unified/", "/mcp/"))
    if is_gateway and IP_RPM and request.method != "OPTIONS" and _ip_limited(request.client.host if request.client else "?"):
        return JSONResponse({"error": {"message": "Too many requests from this address", "type": "rate_limit_exceeded", "code": 429}}, status_code=429, headers={"Retry-After": "30"})
    # CORS for the OpenAI-compatible surface only
    cors = {}
    if is_gateway and CORS_ORIGINS:
        origin = request.headers.get("origin", "")
        if "*" in CORS_ORIGINS or origin in CORS_ORIGINS:
            cors = {"Access-Control-Allow-Origin": "*" if "*" in CORS_ORIGINS else origin, "Vary": "Origin",
                    "Access-Control-Allow-Headers": "authorization,content-type,x-api-key,anthropic-version,x-sg-require,x-sg-session,x-sg-cache,mcp-session-id,mcp-protocol-version,last-event-id,accept",
                    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS", "Access-Control-Expose-Headers": "X-SG-Upstream,X-SG-Endpoint,X-SG-Request-Id,X-SG-Cache,Mcp-Session-Id"}
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=cors)
    if p.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS") and not origin_ok(request.headers):
        return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)   # CSRF: SameSite=Lax alone doesn't cover same-site origins
    # admin protection for the management API (UI shell stays public so the login page can load)
    if config.ADMIN_PASSWORD and p.startswith("/api/") and p not in ("/api/login", "/api/session", "/api/ws"):
        ip = request.client.host if request.client else "?"
        if request.headers.get("authorization") and not valid_session(request.cookies.get(COOKIE)) and _locked(ip):
            # a locked-out client must not make us run the password hash at all (it is the expensive part)
            return JSONResponse({"detail": "Too many failed attempts, try again in a few minutes"}, status_code=429)
        if not await asyncio.to_thread(request_authed, request.headers, request.cookies):   # scrypt must not block the event loop
            if request.headers.get("authorization"):   # a wrong password was presented: count it, like the login form does
                _fail(ip)
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    audited = None if is_gateway else audit.classify(request.method, p)
    fields: list = []
    if audited and "json" in request.headers.get("content-type", ""):
        fields = audit.body_fields(await request.body())
    resp = await call_next(request)
    if audited and resp.status_code < 400:
        audit.record(admin_actor(), request.client.host if request.client else "", audited["action"], audited["target_type"],
                     audited["target_id"], resp.status_code, request.method, p, fields)
    for k, v in cors.items():
        resp.headers[k] = v
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if not is_gateway:   # the admin UI must not be framed (clickjacking) and may only load its own scripts
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    return resp


def admin_actor() -> str:
    return os.environ.get("SG_ADMIN_USER", "admin") if config.ADMIN_PASSWORD else "open-access"


class LoginIn(BaseModel):
    username: str = "admin"
    password: str


@app.get("/api/session")
def session_info(request: Request):
    authed = request_authed(request.headers, request.cookies)
    return {"auth_required": bool(config.ADMIN_PASSWORD), "authenticated": authed, **({"password_source": config.PASSWORD_SOURCE} if authed else {})}


class PasswordIn(BaseModel):
    current: str
    new: str


@app.post("/api/admin/password")
def change_password(body: PasswordIn, request: Request):
    if config.PASSWORD_SOURCE not in ("ui", "generated"):
        raise HTTPException(400, "The admin password is set by the SG_ADMIN_PASSWORD environment variable; change it there")
    ip = request.client.host if request.client else "?"
    if _locked(ip):
        return JSONResponse({"detail": "Too many failed attempts, try again in a few minutes"}, status_code=429)
    if not config.verify_password(body.current):
        _fail(ip)
        audit.record(admin_actor(), ip, "password change failed", "session", status=403, method="POST", path="/api/admin/password")
        return JSONResponse({"detail": "Current password is wrong"}, status_code=403)
    if len(body.new) < config.MIN_PASSWORD:
        raise HTTPException(400, f"The new password must be at least {config.MIN_PASSWORD} characters")
    if body.new == body.current:
        raise HTTPException(400, "The new password must differ from the current one")
    config.set_admin_password(body.new)
    _fails.pop(ip, None)
    r = JSONResponse({"ok": True})   # every other session is now invalid; keep this browser signed in
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    r.set_cookie(COOKIE, make_session(), max_age=SESSION_TTL, httponly=True, samesite="lax", secure=secure)
    return r


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "?"
    if _locked(ip):
        return JSONResponse({"detail": "Too many failed attempts, try again in a few minutes"}, status_code=429)
    ok = (not config.ADMIN_PASSWORD) or (
        config.verify_password(body.password)
        and secrets.compare_digest(body.username, os.environ.get("SG_ADMIN_USER", "admin")))
    if not ok:
        _fail(ip)
        audit.record(body.username[:60], ip, "login failed", "session", status=401, method="POST", path="/api/login")
        return JSONResponse({"detail": "Invalid credentials"}, status_code=401)
    _fails.pop(ip, None)
    audit.record(body.username[:60], ip, "login", "session", method="POST", path="/api/login")
    r = JSONResponse({"ok": True})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    r.set_cookie(COOKIE, make_session(), max_age=SESSION_TTL, httponly=True, samesite="lax", secure=secure)
    return r


@app.post("/api/logout")
def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.add_middleware(BodyLimit)   # added after the guard middleware, so it is the outermost layer
app.include_router(endpoints.router)
app.include_router(gateways.router)
app.include_router(stats.router)
app.include_router(maintenance_api.router)
app.include_router(settings_api.router)
app.include_router(compression_api.router)
app.include_router(content_filters_api.router)
app.include_router(insights_api.router)
app.include_router(stress_api.router)
app.include_router(unified_api.router)
app.include_router(mcp_endpoints_api.router)
app.include_router(mcp_gateways_api.router)
app.include_router(mcp_server.router)
app.include_router(proxy.router)

if os.path.isdir(config.STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(config.STATIC_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith(("api/", "gw/", "v1/", "unified/", "mcp/")):
            return JSONResponse({"error": "not found"}, status_code=404)
        # only files inside the static dir are served: resolve symlinks/".." and absolute paths first
        root = os.path.realpath(config.STATIC_DIR)
        f = os.path.realpath(os.path.join(root, full_path))
        if full_path and f.startswith(root + os.sep) and os.path.isfile(f):
            return FileResponse(f)
        # the HTML shell must always be revalidated so a new deploy is picked up (assets are content-hashed)
        return FileResponse(os.path.join(config.STATIC_DIR, "index.html"), headers={"Cache-Control": "no-cache"})
