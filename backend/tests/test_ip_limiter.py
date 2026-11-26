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



def test_ip_limiter_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(main, "IP_RPM", 3)
    monkeypatch.setattr(main, "MAX_TRACKED_IPS", 50)
    main._ip_hits.clear()
    for _ in range(10_000):                                   # one address hammering: rejected requests are not stored
        main._ip_limited("1.1.1.1")
    assert len(main._ip_hits["1.1.1.1"]) == 3
    for i in range(500):                                      # many addresses: the table stays at the cap, newest kept
        main._ip_limited(f"10.0.{i // 250}.{i % 250}")
    assert len(main._ip_hits) <= 50 and "10.0.1.249" in main._ip_hits
    main._ip_hits.clear()


def test_ip_limiter_window_slides(monkeypatch):
    monkeypatch.setattr(main, "IP_RPM", 2)
    main._ip_hits.clear()
    t = [1000.0]
    monkeypatch.setattr(main.time, "time", lambda: t[0])
    assert [main._ip_limited("a") for _ in range(3)] == [False, False, True]
    t[0] += 61
    assert main._ip_limited("a") is False                     # the old hits aged out
    main._ip_hits.clear()
