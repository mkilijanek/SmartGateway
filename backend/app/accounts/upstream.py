"""What proxy._route calls for an account endpoint: send one chat/completions request to the vendor and return a chat/completions-shaped response."""
import json

from ..router import health
from . import AccountError, claude_adapter, claude_auth, codex_adapter, codex_auth, tokens
from .adapted import AdaptedResponse

CODEX_UA = "codex_cli_rs/0.99.0 (SmartGateway)"


def _codex(token: str, account_id, fwd: dict):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream", "originator": "codex_cli_rs", "User-Agent": CODEX_UA}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return f"{codex_auth.CODEX_BASE}/responses", headers, json.dumps(codex_adapter.request(fwd)), codex_adapter.chunks


def _claude(token: str, account_id, fwd: dict):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream", "anthropic-version": "2023-06-01",
               "anthropic-beta": "oauth-2025-04-20"}
    return f"{claude_auth.API_BASE}/v1/messages", headers, json.dumps(claude_adapter.request(fwd)), claude_adapter.chunks


BUILDERS = {"codex-oauth": _codex, "claude-oauth": _claude}


async def send(client, c, route: str, fwd: dict, form_files=None):
    """Raises AccountError when the account cannot serve this request (the gateway then tries the next member)."""
    if route != "chat/completions" or form_files is not None:
        raise AccountError(f"This account only serves chat completions (not {route})")
    stream = bool(fwd.get("stream"))
    resp = None
    for attempt in (0, 1):
        try:
            token, account_id = await tokens.fresh_access(c.endpoint_id, force=bool(attempt))
        except AccountError as e:
            if e.needs_login:
                health.status[c.endpoint_id] = "offline"
            raise
        url, headers, body, chunker = BUILDERS[c.provider](token, account_id, fwd)
        resp = await client.send(client.build_request("POST", url, content=body, headers=headers), stream=True)
        if resp.status_code == 401 and attempt == 0:   # the token was revoked or rotated elsewhere: refresh once and retry
            await resp.aclose()
            continue
        break
    return await AdaptedResponse(resp, str(fwd.get("model") or ""), stream, chunker).prepare()
