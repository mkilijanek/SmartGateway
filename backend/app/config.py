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
MIN_PASSWORD = 10


def _hash_password(pw: str) -> str:
    import base64, hashlib, secrets
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


import threading
_SCRYPT_SLOTS = threading.BoundedSemaphore(int(os.environ.get("SG_MAX_PASSWORD_CHECKS", "4")))   # each check needs ~16 MiB and a CPU core


def _check_hash(pw: str, stored: str) -> bool:
    import base64, hashlib, hmac
    try:
        _, salt, h = stored.split("$")
        with _SCRYPT_SLOTS:   # at most a few verifications at once, however many requests are trying passwords
            got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got, base64.b64decode(h))
    except Exception:  # noqa: BLE001
        return False


def verify_password(candidate: str) -> bool:
    import secrets
    if not ADMIN_PASSWORD:
        return True
    if PASSWORD_SOURCE == "ui":
        return _check_hash(candidate, ADMIN_PASSWORD)
    return secrets.compare_digest(candidate.encode(), ADMIN_PASSWORD.encode())


def ensure_admin_password() -> None:
    global ADMIN_PASSWORD, GENERATED_ADMIN_PASSWORD, PASSWORD_SOURCE
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
    if not os.path.exists(path):
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
