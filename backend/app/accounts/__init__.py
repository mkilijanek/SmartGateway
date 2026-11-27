"""Subscription accounts (OAuth) as upstreams: one account = one Endpoint with provider "codex-oauth" (ChatGPT) or "claude-oauth" (Claude).

The rest of the gateway keeps speaking OpenAI chat/completions; this package signs in, keeps the tokens fresh and translates to and from the
vendor's own API at the last moment (see upstream.py)."""

OAUTH_PROVIDERS = {"codex-oauth", "claude-oauth"}
LABELS = {"codex-oauth": "ChatGPT / Codex", "claude-oauth": "Claude"}
KIND = {"codex-oauth": "codex", "claude-oauth": "claude"}


class AccountError(Exception):
    """An account cannot be used right now (needs a new sign-in, unsupported route, vendor refused). The message is safe to log."""

    def __init__(self, message: str, needs_login: bool = False):
        super().__init__(message)
        self.needs_login = needs_login


def is_oauth(provider) -> bool:
    return provider in OAUTH_PROVIDERS
