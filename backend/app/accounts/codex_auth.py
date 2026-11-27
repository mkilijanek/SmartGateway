"""ChatGPT / Codex sign-in: the device-code flow the Codex CLI uses (no browser redirect, so it works inside Docker), token refresh and
the account identity found in the tokens. Tokens are only ever stored encrypted (models.OAuthAccount) and are never logged."""
import base64
import json
import os
from typing import Optional

import httpx
from . import AccountError

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"          # the public client id of the official Codex CLI
AUTH_BASE = os.environ.get("SG_CODEX_AUTH_BASE", "https://auth.openai.com").rstrip("/")
CODEX_BASE = os.environ.get("SG_CODEX_BASE", "https://chatgpt.com/backend-api/codex").rstrip("/")
AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"
DEAD_REFRESH = {"invalid_grant", "refresh_token_reused", "refresh_token_expired", "refresh_token_invalidated", "invalid_request"}


def _client() -> httpx.AsyncClient:
    from .. import proxy   # the shared client is also the test seam
    return proxy.client


def jwt_claims(token: Optional[str]) -> dict:
    """Payload of a JWT, unverified: the values are only shown to the admin or echoed back to their issuer, never trusted for access."""
    try:
        part = (token or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:  # noqa: BLE001
        return {}


def identity(tokens: dict) -> dict:
    a, i = jwt_claims(tokens.get("access_token")), jwt_claims(tokens.get("id_token"))
    auth = a.get(AUTH_CLAIM) or i.get(AUTH_CLAIM) or {}
    email = i.get("email") or (a.get(PROFILE_CLAIM) or {}).get("email") or a.get("email")
    return {"account_id": auth.get("chatgpt_account_id") or None, "plan": auth.get("chatgpt_plan_type") or None, "email": email or None}


# ------------------------------------------------------------------ device-code sign-in
async def device_start() -> dict:
    r = await _client().post(f"{AUTH_BASE}/api/accounts/deviceauth/usercode", json={"client_id": CLIENT_ID}, timeout=20)
    if r.status_code != 200:
        raise AccountError(f"OpenAI did not start the sign-in (HTTP {r.status_code})")
    d = r.json()
    try:
        interval = max(2, int(d.get("interval") or 5))
    except (TypeError, ValueError):
        interval = 5
    return {"device_auth_id": d["device_auth_id"], "user_code": d.get("user_code") or d.get("usercode") or "",
            "verification_url": f"{AUTH_BASE}/codex/device", "interval": interval}


async def device_poll(device_auth_id: str, user_code: str) -> Optional[dict]:
    """None while the user has not approved yet; the token set once they have."""
    r = await _client().post(f"{AUTH_BASE}/api/accounts/deviceauth/token", json={"device_auth_id": device_auth_id, "user_code": user_code}, timeout=20)
    if r.status_code in (403, 404):
        return None
    if r.status_code != 200:
        raise AccountError(f"OpenAI refused the sign-in (HTTP {r.status_code})")
    d = r.json()
    return await _token_request({"grant_type": "authorization_code", "code": d["authorization_code"], "redirect_uri": f"{AUTH_BASE}/deviceauth/callback",
                                 "client_id": CLIENT_ID, "code_verifier": d["code_verifier"]})


async def _token_request(form: dict) -> dict:
    r = await _client().post(f"{AUTH_BASE}/oauth/token", data=form, timeout=20)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("access_token"):
        err = data.get("error")
        err = err if isinstance(err, str) else (err or {}).get("code") or ""
        raise AccountError(f"token request refused (HTTP {r.status_code}{', ' + err if err else ''})", needs_login=err in DEAD_REFRESH or r.status_code in (400, 401))
    return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"), "id_token": data.get("id_token"),
            "expires_in": int(data.get("expires_in") or 3600)}


async def refresh(refresh_token: str) -> dict:
    return await _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID, "scope": "openid profile email"})
