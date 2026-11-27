"""Stored OAuth accounts (one per endpoint): saving tokens, refreshing them, and what the admin may see. Provider specifics (how a refresh is
made, where the identity sits in the tokens) come from codex_auth / claude_auth through `KINDS`."""
import asyncio
import time
from typing import Optional

from sqlmodel import select

from ..db import session
from ..models import Endpoint, OAuthAccount
from . import AccountError, claude_auth, codex_auth

REFRESH_MARGIN = 300    # refresh when the access token expires within 5 minutes
KINDS = {"codex": codex_auth, "claude": claude_auth}


def save_tokens(endpoint_id: int, tokens: dict, kind: str = "codex") -> OAuthAccount:
    """Create or update the account row of an endpoint from a token response (a refresh keeps the old refresh token if none is returned)."""
    ident = KINDS[kind].identity(tokens)
    with session() as s:
        acc = s.get(OAuthAccount, endpoint_id) or OAuthAccount(endpoint_id=endpoint_id, kind=kind)
        acc.access_token = tokens["access_token"]
        acc.refresh_token = tokens.get("refresh_token") or acc.refresh_token
        acc.expires_at = time.time() + int(tokens.get("expires_in") or 3600)
        acc.account_id = ident["account_id"] or acc.account_id
        acc.email = ident["email"] or acc.email
        acc.plan = ident["plan"] or acc.plan
        acc.needs_login, acc.last_error, acc.last_refresh = False, None, time.time()
        s.add(acc)
        s.commit()
        s.refresh(acc)
        return acc


def _mark(endpoint_id: int, needs_login: bool, error: str) -> None:
    with session() as s:
        acc = s.get(OAuthAccount, endpoint_id)
        if acc:
            acc.needs_login = acc.needs_login or needs_login
            acc.last_error = error[:200]
            s.add(acc)
            s.commit()


_locks: dict[int, asyncio.Lock] = {}


async def fresh_access(endpoint_id: int, force: bool = False) -> tuple[str, Optional[str]]:
    """(access token, account id), refreshing first when the token is (nearly) expired. One refresh at a time per account:
    refresh tokens rotate, so two concurrent refreshes would kill each other."""
    lock = _locks.setdefault(endpoint_id, asyncio.Lock())
    async with lock:
        with session() as s:
            acc = s.get(OAuthAccount, endpoint_id)
            if acc is None:
                raise AccountError("This endpoint has no signed-in account")
            if acc.needs_login or not acc.refresh_token:
                raise AccountError("The account must sign in again", needs_login=True)
            kind, token, refresh, exp, account_id = acc.kind, acc.access_token, acc.refresh_token, acc.expires_at, acc.account_id
        if token and not force and exp - time.time() > REFRESH_MARGIN:
            return token, account_id
        try:
            tokens = await KINDS[kind].refresh(refresh)
        except AccountError as e:
            _mark(endpoint_id, e.needs_login, str(e))
            raise AccountError(f"Refreshing the account failed: {e}", needs_login=e.needs_login) from None
        except Exception as e:  # noqa: BLE001 - network trouble is not a reason to sign the account out
            _mark(endpoint_id, False, f"{type(e).__name__}")
            raise AccountError(f"Refreshing the account failed: {type(e).__name__}") from None
        acc = save_tokens(endpoint_id, tokens, kind)
        return acc.access_token, acc.account_id


def account_info(endpoint_id: int) -> Optional[dict]:
    """What the admin may see about an account (never the tokens)."""
    with session() as s:
        acc = s.get(OAuthAccount, endpoint_id)
        if acc is None:
            return None
        return {"kind": acc.kind, "email": acc.email, "plan": acc.plan, "account_id": acc.account_id, "needs_login": acc.needs_login,
                "expires_at": acc.expires_at, "last_refresh": acc.last_refresh, "last_error": acc.last_error}


def find_endpoint_for(kind: str, account_id: Optional[str], email: Optional[str]) -> Optional[int]:
    """The endpoint that already holds this account (a second sign-in of the same account updates it instead of adding a copy)."""
    if not account_id and not email:
        return None
    with session() as s:
        for acc in s.exec(select(OAuthAccount).where(OAuthAccount.kind == kind)).all():
            if (account_id and acc.account_id == account_id and (not email or not acc.email or acc.email == email)) or (not account_id and acc.email == email):
                if s.get(Endpoint, acc.endpoint_id):
                    return acc.endpoint_id
    return None
