import os

from app import config


def test_without_password_one_is_generated_and_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_OPEN_ADMIN", False)
    config.ensure_admin_password()
    first = config.ADMIN_PASSWORD
    assert len(first) >= 20 and config.GENERATED_ADMIN_PASSWORD and config.GENERATED_ADMIN_PASSWORD_IS_NEW
    assert oct(os.stat(tmp_path / "admin_password").st_mode & 0o777) == "0o600"
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    config.ensure_admin_password()
    assert config.ADMIN_PASSWORD == first            # stable across restarts
    assert not config.GENERATED_ADMIN_PASSWORD_IS_NEW   # ... and not announced again (the start-up log only prints it for a new one)


def test_explicit_password_and_open_opt_in_are_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "mine")
    config.ensure_admin_password()
    assert config.ADMIN_PASSWORD == "mine" and not (tmp_path / "admin_password").exists()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_OPEN_ADMIN", True)
    config.ensure_admin_password()
    assert config.ADMIN_PASSWORD == ""


def test_password_change_via_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import db, main
    db.init_db()
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_OPEN_ADMIN", False)
    monkeypatch.setattr(config, "PASSWORD_SOURCE", "open")
    config.ensure_admin_password()
    old = config.ADMIN_PASSWORD
    main._fails.clear()
    c = TestClient(main.app)
    h = {"Authorization": f"Bearer {old}"}
    assert c.get("/api/session", headers=h).json()["password_source"] == "generated"
    url = "/api/admin/password"
    assert c.post(url, json={"current": "x", "new": "a-long-new-password"}).status_code == 401      # needs admin auth
    assert c.post(url, headers=h, json={"current": "wrong", "new": "a-long-new-password"}).status_code == 403
    assert c.post(url, headers=h, json={"current": old, "new": "short"}).status_code == 400
    assert c.post(url, headers=h, json={"current": old, "new": old}).status_code == 400
    old_session = main.make_session()
    r = c.post(url, headers=h, json={"current": old, "new": "a-long-new-password"})
    assert r.status_code == 200 and "sg_session" in r.headers.get("set-cookie", "")
    assert not main.valid_session(old_session)                                                       # other sessions are signed out
    c = TestClient(main.app)                                                                         # fresh client: no cookie
    assert c.get("/api/gateways", headers=h).status_code == 401                                      # old password is dead
    assert c.get("/api/gateways", headers={"Authorization": "Bearer a-long-new-password"}).status_code == 200
    assert c.post("/api/login", json={"password": "a-long-new-password"}).status_code == 200
    assert not (tmp_path / "admin_password").exists() and "a-long-new-password" not in (tmp_path / "admin_password.hash").read_text()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")          # simulate a restart: the chosen password survives
    config.ensure_admin_password()
    assert config.PASSWORD_SOURCE == "ui" and config.verify_password("a-long-new-password")
    monkeypatch.setattr(config, "PASSWORD_SOURCE", "env")
    assert c.post(url, headers={"Authorization": "Bearer a-long-new-password"}, json={"current": "x", "new": "another-long-one"}).status_code in (400, 401)
    main._fails.clear()
