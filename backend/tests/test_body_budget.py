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



def test_total_inflight_body_budget(client, monkeypatch):
    from app import bodylimit
    monkeypatch.setattr(bodylimit, "GATEWAY_LIMIT", 10_000)
    monkeypatch.setattr(bodylimit, "INFLIGHT_BUDGET", 1_000)
    body = {"model": "x", "messages": [{"role": "user", "content": "a" * 1500}]}
    r = client.post("/gw/anything/v1/chat/completions", json=body)           # one request bigger than the whole budget
    assert r.status_code == 503 and r.headers["retry-after"] == "5" and r.json()["error"]["type"] == "overloaded_error"
    r = client.post("/api/login", content=iter([b"a" * 600, b"b" * 600]), headers={"content-type": "application/json"})
    assert r.status_code == 503                                              # chunked: counted as it arrives
    assert bodylimit._inflight == 0                                          # everything was given back
    small = client.post("/gw/anything/v1/chat/completions", json={"model": "x"})
    assert small.status_code != 503 and bodylimit._inflight == 0


def test_inflight_budget_is_shared_between_concurrent_requests(monkeypatch):
    import asyncio
    from app import bodylimit
    monkeypatch.setattr(bodylimit, "INFLIGHT_BUDGET", 1_000)
    monkeypatch.setattr(bodylimit, "GATEWAY_LIMIT", 10_000)
    gate, answers = asyncio.Event(), []

    async def app(scope, receive, send):
        await receive()
        await gate.wait()                                   # hold the request open (its bytes stay reserved)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def call(i):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"x" * 600, "more_body": False}

        async def send(m):
            sent.append(m)
        scope = {"type": "http", "path": "/gw/a/v1/x", "headers": []}
        await bodylimit.BodyLimit(app)(scope, receive, send)
        answers.append(sent[0]["status"])

    async def main_():
        t1 = asyncio.create_task(call(1))
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(call(2))                   # 600 + 600 > 1000 -> refused while the first is still in flight
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(t1, t2)
        t3 = asyncio.create_task(call(3))                   # budget returned: accepted again
        await t3
    asyncio.run(main_())
    assert sorted(answers) == [200, 200, 503] and bodylimit._inflight == 0
