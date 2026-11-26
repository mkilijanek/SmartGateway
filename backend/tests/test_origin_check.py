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



def test_cross_origin_state_changes_are_refused(client):
    ok = {"Authorization": "Bearer pw-for-test"}
    assert client.post("/api/login", json={"password": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/logout", headers={"Origin": "https://evil.example", **ok}).status_code == 403
    assert client.post("/api/logout", headers={"Origin": "null", **ok}).status_code == 403
    assert client.post("/api/logout", headers={"Origin": "http://testserver", **ok}).status_code == 200   # same host
    assert client.post("/api/logout", headers=ok).status_code == 200                                      # no Origin: script / curl
    assert client.get("/api/gateways", headers={"Origin": "https://evil.example", **ok}).status_code == 200   # reads are not state changes


def test_origin_follows_forwarded_host_and_allowlist(monkeypatch):
    assert main.origin_ok({"origin": "https://sg.example.com", "host": "10.0.0.5:8080", "x-forwarded-host": "sg.example.com"})
    assert not main.origin_ok({"origin": "https://sg.example.com.evil.io", "host": "sg.example.com"})
    assert not main.origin_ok({"origin": "https://other.example", "host": "sg.example.com"})
    monkeypatch.setattr(main, "ALLOWED_ORIGINS", ["https://other.example"])
    assert main.origin_ok({"origin": "https://other.example/", "host": "sg.example.com"})


def test_websocket_refuses_a_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect
    ok = {"Authorization": "Bearer pw-for-test"}
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/api/ws", headers={"Origin": "https://evil.example", **ok}):
            pass
    assert e.value.code == 4403
    with client.websocket_connect("/api/ws", headers={"Origin": "http://testserver", **ok}) as ws:
        assert ws.receive_json()["type"] == "snapshot"
