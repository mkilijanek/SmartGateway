import os

import pytest
from fastapi.testclient import TestClient

from app import config, db, main


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """A brand-new install: empty data dir, no password in the environment."""
    db.init_db()
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_OPEN_ADMIN", False)
    monkeypatch.setattr(config, "PASSWORD_SOURCE", "open")
    monkeypatch.setattr(config, "SETUP_WIZARD", True)
    monkeypatch.setattr(config, "SETUP_PENDING", False)
    monkeypatch.delenv("SG_ADMIN_USER", raising=False)
    main._fails.clear()
    yield tmp_path
    main._fails.clear()
    config.SETUP_PENDING = False


def start():
    config.ensure_admin_password()
    return TestClient(main.app)


def test_fresh_install_enters_setup_with_a_private_code(fresh):
    c = start()
    assert config.SETUP_PENDING and not config.GENERATED_ADMIN_PASSWORD and not (fresh / "admin_password").exists()
    code = (fresh / "setup_code").read_text()
    assert len(code) == 14 and oct(os.stat(fresh / "setup_code").st_mode & 0o777) == "0o600"
    s = c.get("/api/session").json()
    assert s["setup_required"] and s["auth_required"] and not s["authenticated"]
    config.ensure_admin_password()                          # a restart before finishing keeps the same code
    assert (fresh / "setup_code").read_text() == code


def test_api_is_closed_until_setup_is_done(fresh):
    c = start()
    for path in ("/api/gateways", "/api/endpoints", "/api/settings"):
        assert c.get(path).status_code == 403
    assert c.post("/api/login", json={"password": "anything"}).status_code == 403          # an empty password must never mean "open"
    assert c.get("/api/gateways", headers={"Authorization": "Bearer x"}).status_code == 403
    assert c.get("/healthz").status_code == 200


def test_wizard_creates_the_account(fresh):
    c = start()
    code = (fresh / "setup_code").read_text()
    body = {"code": code, "username": "boss", "password": "a-long-password-1"}
    assert c.post("/api/setup", json={**body, "code": "WRONG-WRONG-WR"}).status_code == 403
    assert c.post("/api/setup", json={**body, "password": "short"}).status_code == 400
    assert c.post("/api/setup", json={**body, "username": "x"}).status_code == 400
    assert c.post("/api/setup", json={**body, "username": "bad name!"}).status_code == 400
    r = c.post("/api/setup", json={**body, "code": code.lower()})      # the code is not case sensitive
    assert r.status_code == 200 and "sg_session" in r.headers.get("set-cookie", "")
    assert not config.SETUP_PENDING and not (fresh / "setup_code").exists()
    assert (fresh / "admin_user").read_text() == "boss" and "a-long-password-1" not in (fresh / "admin_password.hash").read_text()
    assert c.get("/api/gateways").status_code == 200                    # the new session cookie works
    s = c.get("/api/session").json()
    assert s["authenticated"] and not s["setup_required"] and s["password_source"] == "ui"
    c2 = TestClient(main.app)
    assert c2.get("/api/gateways").status_code == 401
    assert c2.post("/api/login", json={"username": "admin", "password": "a-long-password-1"}).status_code == 401   # the chosen user name counts
    assert c2.post("/api/login", json={"username": "boss", "password": "wrong-password-1"}).status_code == 401
    assert c2.post("/api/login", json={"username": "boss", "password": "a-long-password-1"}).status_code == 200
    assert c2.get("/api/gateways", headers={"Authorization": "Bearer a-long-password-1"}).status_code == 200
    # setup can never be run again, with or without the old code
    assert c2.post("/api/setup", json={**body, "password": "another-long-pw-2"}).status_code == 409


def test_account_survives_a_restart(fresh, monkeypatch):
    c = start()
    c.post("/api/setup", json={"code": (fresh / "setup_code").read_text(), "username": "boss", "password": "a-long-password-1"})
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")                   # simulate a restart
    config.ensure_admin_password()
    assert not config.SETUP_PENDING and config.PASSWORD_SOURCE == "ui" and config.verify_password("a-long-password-1")


def test_wrong_codes_are_rate_limited(fresh):
    c = start()
    codes = [c.post("/api/setup", json={"code": f"BAD{i}", "username": "boss", "password": "a-long-password-1"}).status_code for i in range(12)]
    assert codes[0] == 403 and codes[-1] == 429


def test_existing_installs_never_see_the_wizard(fresh, monkeypatch):
    # 1. a password from the environment
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "from-env-password")
    config.ensure_admin_password()
    assert not config.SETUP_PENDING and not (fresh / "setup_code").exists()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    # 2. a password chosen in Settings earlier
    (fresh / "admin_password.hash").write_text(config._hash_password("old-ui-password"))
    config.ensure_admin_password()
    assert not config.SETUP_PENDING and config.PASSWORD_SOURCE == "ui"
    (fresh / "admin_password.hash").unlink()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    # 3. a generated password from an earlier start
    (fresh / "admin_password").write_text("generated-earlier-1234567890")
    config.ensure_admin_password()
    assert not config.SETUP_PENDING and config.GENERATED_ADMIN_PASSWORD
    (fresh / "admin_password").unlink()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "GENERATED_ADMIN_PASSWORD", False)
    # 4. an older install that already has a database but no credentials: keeps the generated-password behaviour
    (fresh / "smartgateway.db").write_bytes(b"")
    config.ensure_admin_password()
    assert not config.SETUP_PENDING and config.GENERATED_ADMIN_PASSWORD
    (fresh / "admin_password").unlink()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "GENERATED_ADMIN_PASSWORD", False)
    # 5. the wizard switched off
    (fresh / "smartgateway.db").unlink()
    monkeypatch.setattr(config, "SETUP_WIZARD", False)
    config.ensure_admin_password()
    assert not config.SETUP_PENDING and config.GENERATED_ADMIN_PASSWORD


def test_env_user_name_still_works_without_the_wizard(fresh, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw-for-test-123")
    monkeypatch.setattr(config, "PASSWORD_SOURCE", "env")
    monkeypatch.setenv("SG_ADMIN_USER", "ops")
    c = TestClient(main.app)
    assert c.post("/api/login", json={"username": "ops", "password": "pw-for-test-123"}).status_code == 200
    assert c.post("/api/login", json={"username": "admin", "password": "pw-for-test-123"}).status_code == 401
