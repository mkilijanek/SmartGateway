import pytest
from fastapi.testclient import TestClient

from app import config, db, main
from app.main import app


@pytest.fixture()
def client(monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw-for-test")
    main._fails.clear()
    yield TestClient(app)
    main._fails.clear()



def test_locked_client_never_reaches_the_password_check(client, monkeypatch):
    for _ in range(5):
        client.get("/api/gateways", headers={"Authorization": "Bearer nope"})
    calls = []
    real = config.verify_password
    monkeypatch.setattr(config, "verify_password", lambda p: calls.append(p) or real(p))
    r = client.get("/api/gateways", headers={"Authorization": "Bearer pw-for-test"})   # even the right password is not tried while locked
    assert r.status_code == 429 and calls == []


def test_scrypt_checks_run_off_the_event_loop_and_are_bounded(monkeypatch):
    import threading, time as _t
    stored = config._hash_password("a-long-enough-password")
    monkeypatch.setattr(config, "_SCRYPT_SLOTS", threading.BoundedSemaphore(2))
    peak = {"now": 0, "max": 0}
    lock = threading.Lock()
    import hashlib
    real = hashlib.scrypt

    def spy(*a, **k):
        with lock:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
        _t.sleep(0.05)
        try:
            return real(*a, **k)
        finally:
            with lock:
                peak["now"] -= 1

    monkeypatch.setattr(hashlib, "scrypt", spy)
    ts = [threading.Thread(target=config._check_hash, args=("a-long-enough-password", stored)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak["max"] <= 2
