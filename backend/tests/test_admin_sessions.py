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


def test_logout_revokes_the_session_server_side(client):
    c = client
    r = c.post("/api/login", json={"password": "pw-for-test"})
    assert r.status_code == 200
    cookie = r.cookies.get("sg_session")
    stolen = TestClient(app)
    stolen.cookies.set("sg_session", cookie)
    assert stolen.get("/api/gateways").status_code == 200
    assert c.post("/api/logout").status_code == 200
    assert stolen.get("/api/gateways").status_code == 401        # a copy of the cookie is dead too, not just the one that was deleted
    assert not main.valid_session(cookie)


def test_sessions_are_independent_and_old_tokens_are_not_accepted(client):
    a, b = main.make_session(), main.make_session()
    assert a != b and main.valid_session(a) and main.valid_session(b)
    main.revoke_session(a)
    assert not main.valid_session(a) and main.valid_session(b)   # other sessions keep working
    legacy = "9999999999." + main._sign(9999999999, "")           # the old two-part format
    assert not main.valid_session(legacy)
    assert not main.valid_session(b[:-2] + "00")                  # tampered signature
    main.revoke_session(everyone=True)
    assert not main.valid_session(b)


def test_open_websocket_is_closed_when_its_session_is_revoked(client, monkeypatch):
    from app.api import stats
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setattr(stats, "RECHECK_EVERY", 1)
    cookie = client.post("/api/login", json={"password": "pw-for-test"}).cookies.get("sg_session")
    with client.websocket_connect("/api/ws", headers={"Cookie": f"sg_session={cookie}"}) as ws:
        assert ws.receive_json()["type"] == "snapshot"
        main.revoke_session(cookie)
        with pytest.raises(WebSocketDisconnect) as e:
            for _ in range(20):
                ws.receive_json()
        assert e.value.code == 4401


def test_websocket_close_signal_survives_a_full_queue(client, monkeypatch):
    import asyncio
    from app import events
    from app.api import stats
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setattr(stats, "RECHECK_EVERY", 1)
    orig = events.subscribe

    def small():                                  # a subscriber whose backlog is already full
        q = asyncio.Queue(maxsize=2)
        events._subscribers.add(q)
        q.put_nowait({"type": "x"})
        q.put_nowait({"type": "x"})
        return q
    monkeypatch.setattr(stats.events, "subscribe", small)
    cookie = client.post("/api/login", json={"password": "pw-for-test"}).cookies.get("sg_session")
    with client.websocket_connect("/api/ws", headers={"Cookie": f"sg_session={cookie}"}) as ws:
        ws.receive_json()
        main.revoke_session(cookie)
        with pytest.raises(WebSocketDisconnect) as e:
            for _ in range(30):
                ws.receive_json()
        assert e.value.code == 4401


def test_logout_reports_failure_instead_of_pretending(client, monkeypatch):
    monkeypatch.setattr(main, "session", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    cookie_client = client
    cookie_client.cookies.set("sg_session", "1.x.y")
    assert main.revoke_session("1.x.y") is False
    monkeypatch.undo()
