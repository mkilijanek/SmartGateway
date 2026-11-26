import pytest
from fastapi.testclient import TestClient

from app import db, main, settings_store
from app.main import app


@pytest.fixture()
def client(monkeypatch):
    db.init_db()
    main._ip_hits.clear()
    monkeypatch.setattr(main, "IP_RPM", 0)                       # as if SG_IP_RPM were unset
    yield TestClient(app)
    settings_store.update({"ip_rpm": None})
    main._ip_hits.clear()


def hit(client):
    return client.get("/gw/nope/v1/models").status_code


def test_default_is_off_and_nothing_is_limited(client):
    assert main.ip_rpm() == 0
    assert all(hit(client) != 429 for _ in range(10))


def test_setting_turns_the_limit_on_without_a_restart(client):
    r = client.put("/api/settings", json={"ip_rpm": 3})
    assert r.status_code == 200 and r.json()["ip_rpm"] == 3
    codes = [hit(client) for _ in range(6)]
    assert 429 not in codes[:3] and codes[3:] == [429, 429, 429]


def test_minus_one_returns_to_the_environment_value(client, monkeypatch):
    client.put("/api/settings", json={"ip_rpm": 3})
    r = client.put("/api/settings", json={"ip_rpm": -1})
    assert r.json()["ip_rpm"] is None and main.ip_rpm() == 0
    monkeypatch.setattr(main, "IP_RPM", 2)                         # env value applies again
    assert main.ip_rpm() == 2


def test_zero_overrides_the_environment_and_bad_values_are_refused(client, monkeypatch):
    monkeypatch.setattr(main, "IP_RPM", 2)
    client.put("/api/settings", json={"ip_rpm": 0})
    assert main.ip_rpm() == 0
    assert client.put("/api/settings", json={"ip_rpm": -5}).status_code == 400
    assert client.put("/api/settings", json={"ip_rpm": 10**9}).status_code == 400
