"""Encryption of upstream API keys at rest (Fernet), plus helpers for hashing gateway keys."""
import base64
import hashlib
import logging
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import Text, TypeDecorator

from . import config

log = logging.getLogger("smartgateway.crypto")
PREFIX = "enc:"
_fernet: Fernet | None = None


def _load_secret() -> bytes:
    env = os.environ.get("SG_SECRET_KEY", "")
    if env:
        return hashlib.sha256(env.encode()).digest()
    path = os.path.join(config.DATA_DIR, "secret.key")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).digest()


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(base64.urlsafe_b64encode(_load_secret()))
    return _fernet


def encrypt(value: str) -> str:
    return PREFIX + fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value.startswith(PREFIX):
        return value  # legacy plaintext
    try:
        return fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise ValueError("Cannot decrypt stored secret: wrong SG_SECRET_KEY / secret.key")


class SecretUnreadable(ValueError):
    """A stored secret exists but cannot be decrypted; it must never be treated as "no secret"."""


class UnreadableSecret(str):
    """Placeholder for a stored secret that cannot be decrypted. Truthy on purpose: code that tests `if api_key:` must not take it for "no key"."""


UNREADABLE = UnreadableSecret("<unreadable>")


def is_unreadable(value) -> bool:
    return isinstance(value, UnreadableSecret)


class EncryptedString(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return None
        return value if value.startswith(PREFIX) else encrypt(value)

    def process_result_value(self, value, dialect):
        if not value:
            return None
        try:
            return decrypt(value)
        except ValueError:
            # DB restored without its secret.key: don't take the whole app down. The value is marked unreadable (not None), so the
            # endpoint is taken out of rotation instead of being called without its credentials; the key must be re-entered.
            log.warning("Stored secret cannot be decrypted (secret.key / SG_SECRET_KEY changed?) - marked unreadable, re-enter it")
            return UNREADABLE


def encrypt_existing(engine) -> int:
    """Encrypt any plaintext upstream keys left over from v1."""
    from sqlalchemy import text

    n = 0
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, api_key FROM endpoint WHERE api_key IS NOT NULL AND api_key != '' AND api_key NOT LIKE 'enc:%'")).all()
        for id_, key in rows:
            conn.execute(text("UPDATE endpoint SET api_key = :k WHERE id = :i"), {"k": encrypt(key), "i": id_})
            n += 1
    return n


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
