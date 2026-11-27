"""The models of a ChatGPT/Codex account: the account's own list when the backend answers, else the built-in one."""
import logging

from .. import discovery
from . import AccountError, claude_adapter, claude_auth, codex_adapter, codex_auth, tokens

log = logging.getLogger("smartgateway.accounts")
CLIENT_VERSION = "0.99.0"


def _found(models: list, caps: list) -> list:
    out = []
    for m in models:
        dm = discovery.DiscoveredModel(upstream_id=m["id"])
        dm.capabilities = set(caps)
        dm.sources = {c: "metadata" for c in dm.capabilities}
        dm.context_length = m.get("context")
        dm.max_slots = 4
        out.append(dm)
    return out


async def discover(endpoint_id: int, provider: str = "codex-oauth") -> list:
    if provider == "claude-oauth":
        return await _claude(endpoint_id)
    return await _codex(endpoint_id)


async def _claude(endpoint_id: int) -> list:
    """GET /v1/models with the account's token; the built-in list when the call is refused."""
    from .. import proxy
    rows = None
    try:
        token, _ = await tokens.fresh_access(endpoint_id)
        r = await proxy.client.get(f"{claude_auth.API_BASE}/v1/models", params={"limit": 100}, timeout=15,
                                   headers={"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"})
        if r.status_code == 200:
            rows = [{"id": x["id"], "context": x.get("max_input_tokens") or 200_000} for x in (r.json().get("data") or []) if isinstance(x, dict) and x.get("id")]
    except AccountError:
        raise
    except Exception as e:  # noqa: BLE001 - fall back to the built-in list
        log.info("claude model list unavailable (%s); using the built-in list", type(e).__name__)
    return _found(rows or claude_adapter.BUILTIN_MODELS, claude_adapter.CAPS)


async def _codex(endpoint_id: int) -> list:
    from .. import proxy
    ids = None
    try:
        token, account_id = await tokens.fresh_access(endpoint_id)
        headers = {"Authorization": f"Bearer {token}", "originator": "codex_cli_rs", "User-Agent": "codex_cli_rs/" + CLIENT_VERSION}
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        r = await proxy.client.get(f"{codex_auth.CODEX_BASE}/models", params={"client_version": CLIENT_VERSION}, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            rows = data.get("models") if isinstance(data, dict) else data
            rows = rows or (data.get("data") if isinstance(data, dict) else None) or []
            ids = [x.get("slug") or x.get("id") for x in rows if isinstance(x, dict) and (x.get("slug") or x.get("id")) and x.get("visibility", "list") != "hide"]
    except AccountError:
        raise
    except Exception as e:  # noqa: BLE001 - fall back to the built-in list
        log.info("codex model list unavailable (%s); using the built-in list", type(e).__name__)
    builtin = {m["id"]: m for m in codex_adapter.BUILTIN_MODELS}
    return _found([builtin.get(i, {"id": i, "context": 272_000}) for i in ids] if ids else codex_adapter.BUILTIN_MODELS, codex_adapter.CAPS)
