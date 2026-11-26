import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

from app import config, db, main, settings_store
from app.main import app


def legacy_hash(pw: str) -> str:
    salt = b"0123456789abcdef"
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


@pytest.fixture()
def ui_password(tmp_path, monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PASSWORD_SOURCE", "ui")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", legacy_hash("correct-horse-battery"))
    main._fails.clear()
    yield
    settings_store.update({"password_hash_log_n": 14})
    main._fails.clear()


def test_new_hashes_are_versioned_and_default_cost_is_unchanged():
    h = config._hash_password("secret-pass-123")
    assert h.startswith("scrypt$14$8$1$") and len(h.split("$")) == 6
    assert config._check_hash("secret-pass-123", h) and not config._check_hash("wrong", h)


def test_legacy_three_part_hashes_still_verify(ui_password):
    assert config.verify_password("correct-horse-battery") and not config.verify_password("nope")


def test_hash_parameters_are_bounded():
    h = config._hash_password("secret-pass-123")
    assert not config._check_hash("secret-pass-123", h.replace("scrypt$14$", "scrypt$30$", 1))     # absurd cost is refused, not computed
    assert not config._check_hash("secret-pass-123", "scrypt$14$16$1$" + h.split("$", 4)[4])
    assert not config._check_hash("x", "garbage")


def test_raising_the_cost_upgrades_the_hash_at_the_next_login(ui_password):
    old = config.ADMIN_PASSWORD
    assert not config.needs_rehash(old)                                  # default cost 14 = the legacy cost: nothing to do
    settings_store.update({"password_hash_log_n": 15})
    assert config.needs_rehash(old)
    assert config.verify_password("wrong-password") is False and config.ADMIN_PASSWORD == old   # a failed login changes nothing
    assert config.verify_password("correct-horse-battery") is True
    assert config.ADMIN_PASSWORD != old and config.ADMIN_PASSWORD.startswith("scrypt$15$8$1$")
    assert config.verify_password("correct-horse-battery") is True and not config.needs_rehash(config.ADMIN_PASSWORD)


def test_env_password_is_never_rehashed(monkeypatch):
    monkeypatch.setattr(config, "PASSWORD_SOURCE", "env")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "from-the-environment")
    assert config.verify_password("from-the-environment")


def test_setting_is_validated_and_reported(ui_password):
    c = TestClient(app, headers={"Authorization": "Bearer correct-horse-battery"})    # the admin API is closed in this fixture
    assert c.put("/api/settings", json={"password_hash_log_n": 13}).status_code == 400
    assert c.put("/api/settings", json={"password_hash_log_n": 17}).status_code == 400
    r = c.put("/api/settings", json={"password_hash_log_n": 15})
    assert r.status_code == 200 and r.json()["password_hash_log_n"] == 15
