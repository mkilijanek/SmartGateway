"""Sign in with a subscription account (ChatGPT / Codex: device code; Claude: link + pasted code) and list the signed-in accounts."""
import asyncio
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..accounts import AccountError, KIND, LABELS, claude_auth, codex_auth, limits as plan_limits, tokens
from ..api import endpoints as endpoints_api
from ..db import get_session
from ..models import Endpoint, Model, OAuthAccount
from ..router import health

router = APIRouter(prefix="/api/accounts")
CODEX, CLAUDE = "codex-oauth", "claude-oauth"
PENDING_TTL = 15 * 60
MAX_PENDING = 20
_pending: dict[str, dict] = {}   # session_id -> sign-in in progress (in memory: a restart just means "start again")


class StartIn(BaseModel):
    name: Optional[str] = None
    endpoint_id: Optional[int] = None   # sign in again for this account instead of adding a new one


class PollIn(BaseModel):
    session_id: str


class FinishIn(BaseModel):
    session_id: str
    code: str


def _prune() -> None:
    now = time.time()
    for k in [k for k, v in _pending.items() if v["expires"] < now]:
        _pending.pop(k, None)


def _begin(provider: str, body: StartIn, s: Session, extra: dict) -> str:
    _prune()
    if len(_pending) >= MAX_PENDING:
        raise HTTPException(429, "Too many sign-ins in progress; finish or wait for them to expire")
    if body.endpoint_id is not None:
        ep = s.get(Endpoint, body.endpoint_id)
        if not ep or ep.provider != provider:
            raise HTTPException(404, "No such account")
    sid = secrets.token_urlsafe(16)
    _pending[sid] = {**extra, "provider": provider, "name": (body.name or "").strip()[:80], "endpoint_id": body.endpoint_id, "expires": time.time() + PENDING_TTL, "busy": False}
    return sid


async def _complete(p: dict, tok: dict, ident: dict, s: Session) -> dict:
    """Store a finished sign-in: update the endpoint of the same account, or create one (its models are discovered but disabled)."""
    provider, kind = p["provider"], KIND[p["provider"]]
    ep_id = p["endpoint_id"] or tokens.find_endpoint_for(kind, ident["account_id"], ident["email"])
    if ep_id:   # the same account signing in again: replace its tokens, keep its endpoint, models and gateways
        ep = s.get(Endpoint, ep_id)
        tokens.save_tokens(ep.id, tok, kind)
        created = False
    else:
        label = ident["email"] or ident["plan"] or "account"
        base = codex_auth.CODEX_BASE if provider == CODEX else claude_auth.API_BASE
        ep = Endpoint(name=p["name"] or f"{LABELS[provider]} ({label})", base_url=base, api_key=None, provider=provider, status="online")
        s.add(ep)
        s.commit()
        s.refresh(ep)
        tokens.save_tokens(ep.id, tok, kind)
        created = True
    try:
        await endpoints_api.sync_models(ep, s, probe=False)   # new models stay off until the admin enables them, as on any endpoint
    except Exception as e:  # noqa: BLE001
        if created:
            for m in s.exec(select(Model).where(Model.endpoint_id == ep.id)).all():
                endpoints_api._delete_model(s, m)
            acc = s.get(OAuthAccount, ep.id)
            if acc:
                s.delete(acc)
            s.delete(ep)
            s.commit()
            raise HTTPException(400, f"Signed in, but the model list could not be read: {e}")
    asyncio.create_task(plan_limits.limits_for(ep.id, force=True))   # read its plan usage right away, so the cards are not empty until the next check
    health.status[ep.id] = "online"
    ep.status, ep.last_error = "online", None
    s.add(ep)
    s.commit()
    s.refresh(ep)
    return {"status": "done", "created": created, "endpoint": endpoints_api.ep_out(ep, s)}


# ------------------------------------------------------------------ ChatGPT / Codex: device code
@router.post("/codex/start")
async def codex_start(body: StartIn, s: Session = Depends(get_session)):
    try:
        d = await codex_auth.device_start()
    except AccountError as e:
        raise HTTPException(502, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Cannot reach OpenAI: {type(e).__name__}")
    sid = _begin(CODEX, body, s, d)
    return {"session_id": sid, "user_code": d["user_code"], "verification_url": d["verification_url"], "interval": d["interval"], "expires_in": PENDING_TTL}


@router.post("/codex/poll")
async def codex_poll(body: PollIn, s: Session = Depends(get_session)):
    _prune()
    p = _pending.get(body.session_id)
    if not p or p["provider"] != CODEX:
        raise HTTPException(404, "This sign-in expired; start again")
    if p["busy"]:
        return {"status": "pending"}
    p["busy"] = True
    try:
        tok = await codex_auth.device_poll(p["device_auth_id"], p["user_code"])
    except AccountError as e:
        _pending.pop(body.session_id, None)
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001 - a network blip: keep waiting
        p["busy"] = False
        return {"status": "pending", "note": type(e).__name__}
    p["busy"] = False
    if tok is None:
        return {"status": "pending"}
    _pending.pop(body.session_id, None)
    return await _complete(p, tok, codex_auth.identity(tok), s)


# ------------------------------------------------------------------ Claude: open a link, paste the code
@router.post("/claude/start")
def claude_start(body: StartIn, s: Session = Depends(get_session)):
    pk = claude_auth.pkce()
    sid = _begin(CLAUDE, body, s, {"pkce": pk})
    return {"session_id": sid, "authorize_url": claude_auth.authorize_url(pk), "expires_in": PENDING_TTL}


@router.post("/claude/finish")
async def claude_finish(body: FinishIn, s: Session = Depends(get_session)):
    _prune()
    p = _pending.get(body.session_id)
    if not p or p["provider"] != CLAUDE:
        raise HTTPException(404, "This sign-in expired; start again")
    if p["busy"]:
        raise HTTPException(409, "This sign-in is already being completed")
    p["busy"] = True
    try:
        tok = await claude_auth.exchange(body.code, p["pkce"])
    except AccountError as e:   # a wrong code can be retyped; the sign-in stays open until it expires
        p["busy"] = False
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        p["busy"] = False
        raise HTTPException(502, f"Cannot reach Anthropic: {type(e).__name__}")
    _pending.pop(body.session_id, None)
    return await _complete(p, tok, claude_auth.identity(tok), s)


@router.get("")
def list_accounts(s: Session = Depends(get_session)):
    out = []
    for ep in s.exec(select(Endpoint).where(Endpoint.provider.in_(list(KIND)))).all():
        out.append({"endpoint_id": ep.id, "name": ep.name, "provider": ep.provider, "status": ep.status, **(tokens.account_info(ep.id) or {})})
    return out
