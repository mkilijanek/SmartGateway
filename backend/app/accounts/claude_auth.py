"""Claude sign-in: OAuth with PKCE where the user opens a link, logs in at claude.ai and pastes the code shown on the callback page
(no redirect to this server, so it works in Docker). Tokens are only ever stored encrypted (models.OAuthAccount) and never logged.

The client id, endpoints and scopes are those of the official Claude Code app as used by third-party tools; none of this is a documented
public API, so every value can be overridden with SG_CLAUDE_* environment variables if Anthropic changes it."""
import base64
import hashlib
import os
import secrets
from urllib.parse import urlencode

import httpx

from . import AccountError

CLIENT_ID = os.environ.get("SG_CLAUDE_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
AUTHORIZE_URL = os.environ.get("SG_CLAUDE_AUTHORIZE_URL", "https://claude.ai/oauth/authorize")
TOKEN_URL = os.environ.get("SG_CLAUDE_TOKEN_URL", "https://platform.claude.com/v1/oauth/token")
REDIRECT_URI = os.environ.get("SG_CLAUDE_REDIRECT_URI", "https://platform.claude.com/oauth/code/callback")
API_BASE = os.environ.get("SG_CLAUDE_API_BASE", "https://api.anthropic.com").rstrip("/")
SCOPES = os.environ.get("SG_CLAUDE_SCOPES", "org:create_api_key user:profile user:inference")
DEAD_REFRESH = {"invalid_grant", "invalid_refresh_token", "invalid_request", "unauthorized_client"}


def _client() -> httpx.AsyncClient:
    from .. import proxy   # the shared client is also the test seam
    return proxy.client


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def pkce() -> dict:
    """A fresh PKCE verifier/challenge pair and a CSRF state (S256, 256 bits of entropy each)."""
    verifier = _b64url(secrets.token_bytes(32))
    return {"verifier": verifier, "challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "state": _b64url(secrets.token_bytes(32))}


def authorize_url(p: dict) -> str:
    return AUTHORIZE_URL + "?" + urlencode({"code": "true", "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": SCOPES,
                                            "code_challenge": p["challenge"], "code_challenge_method": "S256", "state": p["state"]})


def identity(tokens: dict) -> dict:
    acct, org = tokens.get("account") or {}, tokens.get("organization") or {}
    return {"account_id": acct.get("uuid") or None, "email": acct.get("email_address") or acct.get("email") or None,
            "plan": org.get("organization_type") or org.get("name") or None}


async def _token_request(body: dict) -> dict:
    r = await _client().post(TOKEN_URL, json=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=20)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("access_token"):
        err = data.get("error")
        err = err if isinstance(err, str) else (err or {}).get("type") or (err or {}).get("message") or ""
        raise AccountError(f"token request refused (HTTP {r.status_code}{', ' + str(err)[:60] if err else ''})",
                           needs_login=str(err) in DEAD_REFRESH or r.status_code in (400, 401))
    return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"), "expires_in": int(data.get("expires_in") or 3600),
            "account": data.get("account"), "organization": data.get("organization")}


async def exchange(pasted: str, p: dict) -> dict:
    """The user pastes what the callback page shows: `code#state` (or just the code). The state must be the one we issued."""
    code, _, state = pasted.strip().partition("#")
    if not code:
        raise AccountError("Paste the code shown after you logged in")
    if state and not secrets.compare_digest(state, p["state"]):
        raise AccountError("That code belongs to a different sign-in; start again")
    return await _token_request({"grant_type": "authorization_code", "code": code, "state": p["state"], "client_id": CLIENT_ID,
                                 "redirect_uri": REDIRECT_URI, "code_verifier": p["verifier"]})


async def refresh(refresh_token: str) -> dict:
    return await _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID})
