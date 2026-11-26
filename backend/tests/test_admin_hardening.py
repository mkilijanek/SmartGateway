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


def test_wrong_bearer_passwords_are_rate_limited(client):
    for _ in range(5):
        assert client.get("/api/gateways", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/gateways", headers={"Authorization": "Bearer nope"}).status_code == 429
    assert client.post("/api/login", json={"password": "pw-for-test"}).status_code == 429   # lock is shared with the login form


def test_requests_without_credentials_do_not_lock_anyone_out(client):
    for _ in range(10):
        assert client.get("/api/gateways").status_code == 401
    assert client.get("/api/gateways", headers={"Authorization": "Bearer pw-for-test"}).status_code == 200


def test_session_dies_when_password_changes(client, monkeypatch):
    token = main.make_session()
    assert main.valid_session(token)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "another-password")
    assert not main.valid_session(token)


def test_security_headers(client):
    r = client.get("/healthz")
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "x-frame-options" not in client.get("/gw/nope/v1/models").headers   # API surface can be embedded by tools


# ---- public-surface hardening
def test_session_hides_password_source_from_anonymous(client):
    assert "password_source" not in client.get("/api/session").json()
    assert client.get("/api/session", headers={"Authorization": "Bearer pw-for-test"}).json()["password_source"] in ("env", "open", "generated", "ui")


def test_body_size_limit_returns_413(client, monkeypatch):
    from app import bodylimit
    monkeypatch.setattr(bodylimit, "GATEWAY_LIMIT", 1000)
    monkeypatch.setattr(bodylimit, "ADMIN_LIMIT", 1000)
    big = {"model": "x", "messages": [{"role": "user", "content": "a" * 5000}]}
    r = client.post("/gw/anything/v1/chat/completions", json=big)
    assert r.status_code == 413 and r.json()["error"]["code"] == 413
    r = client.post("/api/login", json={"password": "p" * 5000})
    assert r.status_code == 413
    # chunked bodies without a Content-Length are counted too
    r = client.post("/api/login", content=iter([b"a" * 600, b"b" * 600]), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert client.post("/gw/anything/v1/chat/completions", json={"model": "x"}).status_code != 413   # small bodies still pass


def test_per_ip_rate_limit_on_gateway_urls(client, monkeypatch):
    monkeypatch.setattr(main, "IP_RPM", 3)
    main._ip_hits.clear()
    codes = [client.get("/gw/nope/v1/models").status_code for _ in range(5)]
    assert codes[:3] == [404, 404, 404] and codes[3:] == [429, 429]
    assert client.get("/healthz").status_code == 200            # the admin UI / health check are not limited
    main._ip_hits.clear()


def test_upstream_headers_cannot_reach_clients():
    from app import proxy
    out = proxy.up_headers({"Content-Type": "application/json", "Set-Cookie": "a=b", "Access-Control-Allow-Origin": "*", "Server": "x",
                            "CF-Ray": "1", "Retry-After": "5", "X-RateLimit-Remaining": "3", "Content-Length": "9", "Via": "1.1 x"})
    assert set(out) == {"Content-Type", "Retry-After", "X-RateLimit-Remaining"}


def test_upstream_errors_are_generic_for_the_public_but_detailed_for_admin():
    from app import proxy
    from app.runtime_ctx import verbose
    secret = "gpu-box-7: ConnectError: [Errno 111] Connection refused 10.0.0.7:8000"
    assert "10.0.0.7" not in proxy.public_error(secret, 2) and "gpu-box" not in proxy.public_error(secret, 2)
    tok = verbose.set(True)
    try:
        assert "10.0.0.7" in proxy.public_error(secret, 2)
    finally:
        verbose.reset(tok)


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
