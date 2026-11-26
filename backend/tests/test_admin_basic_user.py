import base64

import pytest
from fastapi.testclient import TestClient

from app import config, db, main
from app.main import app


@pytest.fixture()
def client(monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw-for-test")
    monkeypatch.delenv("SG_ADMIN_USER", raising=False)
    main._fails.clear()
    yield TestClient(app)
    main._fails.clear()


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_basic_auth_checks_the_user_name_like_the_login_form(client):
    assert client.get("/api/gateways", headers=basic("admin", "pw-for-test")).status_code == 200
    assert client.get("/api/gateways", headers=basic("mallory", "pw-for-test")).status_code == 401
    assert client.get("/api/gateways", headers=basic("", "pw-for-test")).status_code == 401
    assert client.post("/api/login", json={"username": "mallory", "password": "pw-for-test"}).status_code == 401   # same rule as the form


def test_basic_auth_follows_sg_admin_user_and_allows_colons_in_the_password(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw:with:colons")
    monkeypatch.setenv("SG_ADMIN_USER", "ops")
    assert client.get("/api/gateways", headers=basic("ops", "pw:with:colons")).status_code == 200
    assert client.get("/api/gateways", headers=basic("admin", "pw:with:colons")).status_code == 401


def test_bearer_is_still_the_password_alone(client):
    assert client.get("/api/gateways", headers={"Authorization": "Bearer pw-for-test"}).status_code == 200


def test_malformed_basic_header_is_refused(client):
    assert client.get("/api/gateways", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert client.get("/api/gateways", headers={"Authorization": "Basic " + base64.b64encode(b"nocolon").decode()}).status_code == 401
