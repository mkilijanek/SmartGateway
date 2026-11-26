import os

DATA_DIR = os.environ.get("SG_DATA_DIR", "./data")
ADMIN_PASSWORD = os.environ.get("SG_ADMIN_PASSWORD", "")
# The management API controls upstream keys and sees all traffic, so it fails closed. Where the admin password comes from:
#   env       SG_ADMIN_PASSWORD (wins over everything, cannot be changed in the UI)
#   ui        a password chosen in Settings, stored as a salted scrypt hash in the data volume
#   generated random, created once on first start, kept in the data volume and printed in the start-up log
#   open      SG_ALLOW_OPEN_ADMIN=1 (trusted dev machines only): no login at all
ALLOW_OPEN_ADMIN = os.environ.get("SG_ALLOW_OPEN_ADMIN", "").lower() in ("1", "true", "yes")
PASSWORD_SOURCE = "env" if ADMIN_PASSWORD else "open"
GENERATED_ADMIN_PASSWORD = False
GENERATED_ADMIN_PASSWORD_IS_NEW = False   # True only on the start that created the password; later starts must not print it again
MIN_PASSWORD = 10


LEGACY_COST = (14, 8, 1)          # log2(N), r, p of the first hash format ("scrypt$salt$hash"), which carried no parameters
MIN_LOG_N, MAX_LOG_N = 14, 16     # 2**14 = the historic cost (16 MB); 2**16 = 64 MB per verification


def configured_log_n() -> int:
    """Cost for new password hashes: Settings > Security (password_hash_log_n), default 14 = unchanged."""
    try:
        from . import settings_store
        n = int(settings_store.get_cached().get("password_hash_log_n") or MIN_LOG_N)
    except Exception:  # noqa: BLE001 - never block a login because the setting cannot be read
        n = MIN_LOG_N
    return max(MIN_LOG_N, min(MAX_LOG_N, n))


def _scrypt(pw: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
    import hashlib
    n = 1 << log_n
    return hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=130 * n * r + (1 << 20))


def _hash_password(pw: str, log_n: int | None = None) -> str:
    """Versioned format: scrypt$<log2 N>$<r>$<p>$salt$hash (the parameters travel with the hash, so the cost can be raised later)."""
    import base64, secrets
    log_n = log_n or configured_log_n()
    salt = secrets.token_bytes(16)
    h = _scrypt(pw, salt, log_n, 8, 1)
    return f"scrypt${log_n}${8}${1}$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def _parse_hash(stored: str):
    """(log_n, r, p, salt_b64, hash_b64) for both the versioned and the legacy three-part format."""
    parts = stored.split("$")
    if len(parts) == 3:
        return (*LEGACY_COST, parts[1], parts[2])
    _, ln, r, p, salt, h = parts
    return int(ln), int(r), int(p), salt, h


import threading
_SCRYPT_SLOTS = threading.BoundedSemaphore(int(os.environ.get("SG_MAX_PASSWORD_CHECKS", "4")))   # each check needs ~16 MiB and a CPU core


def _check_hash(pw: str, stored: str) -> bool:
    import base64, hmac
    try:
        ln, r, p, salt, h = _parse_hash(stored)
        if not (MIN_LOG_N <= ln <= MAX_LOG_N) or r != 8 or p != 1:    # a stored hash is trusted, but not with unbounded memory
            return False
        with _SCRYPT_SLOTS:   # at most a few verifications at once, however many requests are trying passwords
            got = _scrypt(pw, base64.b64decode(salt), ln, r, p)
        return hmac.compare_digest(got, base64.b64decode(h))
    except Exception:  # noqa: BLE001
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return _parse_hash(stored)[0] < configured_log_n()
    except Exception:  # noqa: BLE001
        return False


def verify_password(candidate: str) -> bool:
    import secrets
    if not ADMIN_PASSWORD:
        return True
    if PASSWORD_SOURCE == "ui":
        if not _check_hash(candidate, ADMIN_PASSWORD):
            return False
        if needs_rehash(ADMIN_PASSWORD):    # the cost was raised (or the hash is the legacy format): upgrade it now that the password is known
            try:
                set_admin_password(candidate)
            except Exception:  # noqa: BLE001 - the login itself succeeded
                pass
        return True
    return secrets.compare_digest(candidate.encode(), ADMIN_PASSWORD.encode())


def ensure_admin_password() -> None:
    global ADMIN_PASSWORD, GENERATED_ADMIN_PASSWORD, GENERATED_ADMIN_PASSWORD_IS_NEW, PASSWORD_SOURCE
    if ADMIN_PASSWORD or ALLOW_OPEN_ADMIN:
        return
    import secrets
    os.makedirs(DATA_DIR, exist_ok=True)
    hpath = os.path.join(DATA_DIR, "admin_password.hash")
    if os.path.exists(hpath):
        with open(hpath) as f:
            ADMIN_PASSWORD, PASSWORD_SOURCE = f.read().strip(), "ui"
        return
    path = os.path.join(DATA_DIR, "admin_password")
    GENERATED_ADMIN_PASSWORD_IS_NEW = not os.path.exists(path)
    if GENERATED_ADMIN_PASSWORD_IS_NEW:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(18))
    with open(path) as f:
        ADMIN_PASSWORD = f.read().strip()
    GENERATED_ADMIN_PASSWORD, PASSWORD_SOURCE = True, "generated"


def set_admin_password(new: str) -> None:
    """Persist a password chosen in the UI (hash only) and drop the generated plaintext copy."""
    global ADMIN_PASSWORD, GENERATED_ADMIN_PASSWORD, PASSWORD_SOURCE
    hashed = _hash_password(new)
    hpath = os.path.join(DATA_DIR, "admin_password.hash")
    fd = os.open(hpath + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(hashed)
    os.replace(hpath + ".tmp", hpath)
    try:
        os.remove(os.path.join(DATA_DIR, "admin_password"))
    except OSError:
        pass
    ADMIN_PASSWORD, PASSWORD_SOURCE, GENERATED_ADMIN_PASSWORD = hashed, "ui", False


STATIC_DIR = os.environ.get("SG_STATIC_DIR", "./static")
DEFAULT_SLOTS = int(os.environ.get("SG_DEFAULT_SLOTS", "4"))
HOSTED_SLOTS = int(os.environ.get("SG_HOSTED_SLOTS", "8"))  # default concurrency per model on hosted catalogs (limits are not published)
VERSION = "2.1"
HEALTH_INTERVAL = float(os.environ.get("SG_HEALTH_INTERVAL", "30"))
LOG_RETENTION = int(os.environ.get("SG_LOG_RETENTION", "20000"))

CAPABILITIES = ["chat", "reasoning", "vision", "audio", "video", "ocr", "embedding", "tools", "tts", "image_gen"]
FALLBACK_GRACE = float(os.environ.get("SG_FALLBACK_GRACE", "3"))
STRATEGIES = ["least_busy", "weighted_round_robin", "priority_failover", "fastest", "random", "tiered"]
