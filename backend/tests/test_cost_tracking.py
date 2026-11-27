"""Cost tracking: prices (catalog + manual, peak-hour multipliers), the cost summary, plan limits of subscriptions, cached-token recording."""
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app import db, discovery, pricing, proxy
from app.accounts import limits as plan_limits, tokens
from app.main import app
from app.models import Endpoint, Model, OAuthAccount, RequestLog
from app.router import health

PEAK = [{"when": {"utc_days": {"in": ["mon", "tue", "wed", "thu", "fri"]},
                  "any": [{"all": [{"utc_hours": {"gte": "01:00"}}, {"utc_hours": {"lt": "04:00"}}]}, {"all": [{"utc_hours": {"gte": "06:00"}}, {"utc_hours": {"lt": "10:00"}}]}]},
         "factor": 2}]
CATALOG = {"text": {"input": {"price": 1.0, "cache": {"read": 0.1}}, "output": {"price": 4.0}}, "multipliers": PEAK}


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


MON = (2026, 11, 23)     # a Monday
SAT = (2026, 11, 28)


class M:   # a stand-in for a Model row
    def __init__(self, **kw):
        self.catalog_cost, self.price_in, self.price_out, self.price_cache_read, self.price_manual = None, None, None, None, False
        self.__dict__.update(kw)


def test_catalog_price_cached_tokens_and_peak_multiplier():
    p = pricing.effective(M(catalog_cost=CATALOG))
    assert (p["in"], p["out"], p["cache_read"], p["source"]) == (1.0, 4.0, 0.1, "catalog")
    # 1M prompt of which 400k cached, 100k completion = 600k*1 + 400k*0.1 + 100k*4 = 1.04 USD
    off_peak = pricing.cost(p, 1_000_000, 100_000, 400_000, ts(*MON, 5))
    assert off_peak == pytest.approx(1.04)
    assert pricing.cost(p, 1_000_000, 100_000, 400_000, ts(*MON, 2)) == pytest.approx(2.08)      # Monday 02:00 UTC is in the 01-04 peak window
    assert pricing.cost(p, 1_000_000, 100_000, 400_000, ts(*MON, 7, 30)) == pytest.approx(2.08)  # ... and 06-10
    assert pricing.cost(p, 1_000_000, 100_000, 400_000, ts(*MON, 10)) == pytest.approx(1.04)     # window ends at 10:00
    assert pricing.cost(p, 1_000_000, 100_000, 400_000, ts(*SAT, 2)) == pytest.approx(1.04)      # no surcharge at the weekend
    assert pricing.cost(p, 100, 0, 5000, ts(*MON, 5)) == pytest.approx(100 * 0.1 / 1e6)          # cached never exceeds the prompt


def test_unknown_multiplier_rules_are_ignored_and_manual_price_wins():
    odd = {"text": {"input": {"price": 2}, "output": {"price": 2}}, "multipliers": [{"when": {"region": "eu"}, "factor": 9}, {"when": "nonsense", "factor": 9}]}
    assert pricing.cost(pricing.effective(M(catalog_cost=odd)), 1_000_000, 0, 0, ts(*MON, 2)) == pytest.approx(2.0)
    both = M(catalog_cost=CATALOG, price_in=5, price_out=6, price_manual=True)
    p = pricing.effective(both)
    assert (p["in"], p["out"], p["cache_read"], p["source"], p["rules"]) == (5.0, 6.0, 5.0, "manual", [])     # cache price defaults to the input price
    assert pricing.effective(M()) is None and pricing.effective(M(catalog_cost={"image": {}})) is None
    assert pricing.effective(M(catalog_cost={"text": {"input": {"price": 0}, "output": {"price": 0}}}))["in"] == 0   # free is a price


@pytest.fixture(scope="module")
def client():
    db.init_db()
    yield TestClient(app)
    with Session(db.engine) as s:
        s.exec(delete(RequestLog))
        s.commit()


def mk_ep(name, provider=None, catalog=None, manual=None, models=("m",)):
    with Session(db.engine) as s:
        ep = Endpoint(name=f"{name}-{time.time_ns()}", base_url="http://x/v1", provider=provider, status="online")
        s.add(ep); s.commit(); s.refresh(ep)
        ids = []
        for u in models:
            m = Model(endpoint_id=ep.id, upstream_id=u, capabilities=["chat"], enabled=True, catalog_cost=catalog,
                      price_in=manual[0] if manual else None, price_out=manual[1] if manual else None, price_manual=bool(manual))
            s.add(m); s.commit(); s.refresh(m); ids.append(m.id)
        return ep.id, ep.name, ids


def log(ep_id, model, when, p, c, cached=0, ok=True, gw=1, from_cache=False):
    with Session(db.engine) as s:
        s.add(RequestLog(ts=when, gateway_id=gw, public_model="x", endpoint_id=ep_id, upstream_model=model, route="chat/completions", status=200 if ok else 502,
                         ok=ok, prompt_tokens=p, completion_tokens=c, cached_tokens=cached, cached=from_cache))
        s.commit()


def rows(client, days=7):
    d = client.get(f"/api/cost/summary?days={days}").json()
    return d, {r["endpoint"]: r for r in d["by_model"]}


def test_summary_prices_each_kind_of_traffic(client):
    now = time.time()
    go, go_n, _ = mk_ep("go", "opencode-go", CATALOG)
    zen, zen_n, _ = mk_ep("zen", "opencode-zen", {"text": {"input": {"price": 3}, "output": {"price": 15}}})
    free, free_n, _ = mk_ep("free", "opencode-go", {"text": {"input": {"price": 0}, "output": {"price": 0}}})
    api, api_n, _ = mk_ep("deepseek", None, manual=(0.5, 2.0))
    local, local_n, _ = mk_ep("local", None)
    codex, codex_n, _ = mk_ep("codex", "codex-oauth")
    off_peak = ts(*MON, 5)
    base, _ = rows(client, 30)
    log(go, "m", off_peak, 1_000_000, 100_000, 400_000)                    # 1.04 value on a subscription
    log(go, "m", ts(*MON, 2), 1_000_000, 0, 0)                             # peak: x2
    log(zen, "m", now - 3600, 2_000_000, 1_000_000)                        # 6 + 15 = 21 pay-as-you-go
    log(free, "m", now - 3600, 5_000_000, 5_000_000)
    log(api, "m", now - 3600, 4_000_000, 1_000_000)                        # 2 + 2 = 4 pay-as-you-go (manual price)
    log(local, "m", now - 3600, 700, 300)
    log(codex, "m", now - 3600, 10, 5)
    log(api, "m", now - 3600, 9_000_000, 9_000_000, ok=False)              # failed requests cost nothing
    log(api, "m", now - 3600, 9_000_000, 9_000_000, from_cache=True)       # neither do answers from the gateway's own cache
    d, r = rows(client, 30)
    assert r[go_n]["billing"] == "subscription" and r[go_n]["cost"] == pytest.approx(1.04 + 2.0) and r[go_n]["requests"] == 2 and r[go_n]["cached_tokens"] == 400_000
    assert r[go_n]["price"] == {"in": 1.0, "out": 4.0, "cache_read": 0.1, "source": "catalog", "has_rules": True}
    assert r[zen_n]["billing"] == "payg" and r[zen_n]["cost"] == pytest.approx(21.0)
    assert r[free_n]["billing"] == "free" and r[free_n]["cost"] == 0
    assert r[api_n]["billing"] == "payg" and r[api_n]["cost"] == pytest.approx(4.0) and r[api_n]["requests"] == 1 and r[api_n]["price"]["source"] == "manual"
    assert r[local_n]["billing"] == "unpriced" and r[local_n]["cost"] is None and r[codex_n]["billing"] == "unpriced"
    t, b = d["totals"], base["totals"]
    assert t["payg_cost"] - b["payg_cost"] == pytest.approx(25.0)
    assert t["subscription_value"] - b["subscription_value"] == pytest.approx(3.04)
    assert t["unpriced_requests"] - b["unpriced_requests"] == 2 and t["unpriced_tokens"] - b["unpriced_tokens"] == 1015
    assert t["gateway_cache_hits"] - b["gateway_cache_hits"] == 1
    assert 0 < len(d["daily"]) and d["daily"][-1]["day"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today = d["daily"][-1]
    assert today["payg_cost"] >= 25.0 and zen_n in today["by_endpoint"] and today["by_endpoint"][zen_n] == pytest.approx(21.0)
    long = client.get("/api/cost/summary?days=200").json()                      # beyond 120 days only days with traffic are listed
    assert 0 < len(long["daily"]) < 200 and all(x["requests"] > 0 for x in long["daily"])
    assert client.get("/api/cost/summary?days=0").status_code == 422 and client.get("/api/cost/summary?days=9999").status_code == 422
    # a short range leaves out the old (Monday) traffic
    _, short = rows(client, 1)
    assert go_n not in short and zen_n in short


def test_price_table_manual_override_and_reset(client):
    ep, name, (mid,) = mk_ep("prices", "opencode-go", CATALOG)
    body = [e for e in client.get("/api/cost/prices").json() if e["endpoint_id"] == ep][0]
    m = body["models"][0]
    assert body["hosted_catalog"] and m["has_catalog_price"] and m["price"]["source"] == "catalog" and m["price"]["in"] == 1.0
    assert client.put(f"/api/cost/prices/{mid}", json={"price_in": -1, "price_out": 1}).status_code == 422
    assert client.put(f"/api/cost/prices/{mid}", json={"price_in": 7, "price_out": 8}).status_code == 200
    m = [e for e in client.get("/api/cost/prices").json() if e["endpoint_id"] == ep][0]["models"][0]
    assert m["price"] == {"in": 7.0, "out": 8.0, "cache_read": 7.0, "source": "manual", "has_rules": False}
    log(ep, "m", time.time() - 60, 1_000_000, 1_000_000)
    assert rows(client, 1)[1][name]["cost"] == pytest.approx(15.0)
    assert client.delete(f"/api/cost/prices/{mid}").status_code == 200
    m = [e for e in client.get("/api/cost/prices").json() if e["endpoint_id"] == ep][0]["models"][0]
    assert m["price"]["source"] == "catalog" and m["price"]["in"] == 1.0
    assert client.put("/api/cost/prices/99999999", json={"price_in": 1, "price_out": 1}).status_code == 404


def test_refresh_reads_the_catalog_prices(client, monkeypatch):
    ep, name, (mid,) = mk_ep("zen-refresh", "opencode-zen", None)
    seen = {}

    async def fake(base, key, probe=True, provider=None):
        dm = discovery.DiscoveredModel(upstream_id="m")
        dm.capabilities, dm.cost = {"chat"}, {"text": {"input": {"price": 9}, "output": {"price": 18}}}
        seen["provider"] = provider
        return [dm]
    monkeypatch.setattr(discovery, "discover", fake)
    r = client.post("/api/cost/prices/refresh").json()
    assert r["refreshed"] >= 1 and seen["provider"] == "opencode-zen"
    m = [e for e in client.get("/api/cost/prices").json() if e["endpoint_id"] == ep][0]["models"][0]
    assert m["price"]["in"] == 9.0 and m["price"]["out"] == 18.0 and m["has_catalog_price"]


class Vendors:
    def __init__(self):
        self.calls: list = []
        self.fail: set = set()

    def handler(self, request: httpx.Request):
        host, path = request.url.host, request.url.path
        self.calls.append((host, path, request.headers.get("authorization", "")[:12]))
        if host in self.fail:
            return httpx.Response(500, json={"error": "boom sk-secret-token"})
        reset = int(time.time()) + 3600
        if host == "opencode.ai" and path.endswith("/usage"):
            return httpx.Response(200, json={"usage": {"rolling": {"status": "ok", "percent": 12.5, "resetsAt": "2026-11-27T10:00:00Z"},
                                                       "weekly": {"status": "ok", "percent": 55, "resetsAt": "2026-11-30T00:00:00Z"}, "monthly": {"percent": 101.5, "resetsAt": None}}})
        if host == "chatgpt.com" and path.endswith("/wham/usage"):
            return httpx.Response(200, json={"plan_type": "plus", "rate_limit": {"allowed": True, "primary_window": {"used_percent": 30, "limit_window_seconds": 18000, "reset_at": reset},
                                                                                   "secondary_window": {"used_percent": 80, "limit_window_seconds": 604800, "reset_after_seconds": 7200}},
                                             "credits": {"has_credits": True, "unlimited": False, "balance": "12.5"}})
        if host == "api.anthropic.com" and path.endswith("/oauth/usage"):
            return httpx.Response(200, json={"five_hour": {"utilization": 42.0, "resets_at": "2026-11-27T12:00:00+00:00"}, "seven_day": {"utilization": 9, "resets_at": None},
                                             "seven_day_opus": None, "seven_day_sonnet": {"utilization": 20, "resets_at": "2026-11-30T00:00:00Z"}, "extra_usage": {"is_enabled": False}})
        return httpx.Response(404, json={})


@pytest.fixture
def vendors(monkeypatch):
    v = Vendors()
    monkeypatch.setattr(proxy, "client", httpx.AsyncClient(transport=httpx.MockTransport(v.handler)))
    plan_limits._cache.clear()
    yield v
    plan_limits._cache.clear()


def account_ep(name, provider, kind):
    ep, n, _ = mk_ep(name, provider)
    tokens.save_tokens(ep, {"access_token": "access-secret-token", "refresh_token": "refresh-secret-token", "expires_in": 3600}, kind)
    return ep, n


def test_limits_of_opencode_codex_and_claude(client, vendors):
    go, go_n, _ = mk_ep("go-limits", "opencode-go", CATALOG)
    with Session(db.engine) as s:
        e = s.get(Endpoint, go); e.api_key = "oc-secret-key"; s.add(e); s.commit()
    cx, cx_n = account_ep("codex-limits", "codex-oauth", "codex")
    cl, cl_n = account_ep("claude-limits", "claude-oauth", "claude")
    zen, zen_n, _ = mk_ep("zen-limits", "opencode-zen")
    local, _, _ = mk_ep("local-limits")
    log(go, "m", time.time() - 600, 1000, 500)
    r = {x["name"]: x for x in client.get("/api/cost/limits").json()}
    assert local not in [x["endpoint_id"] for x in r.values()]                 # a plain endpoint has no plan limits
    w = {x["key"]: x for x in r[go_n]["windows"]}
    assert w["five_hour"]["percent"] == 12.5 and w["five_hour"]["resets_at"].startswith("2026-11-27T10:00") and w["seven_day"]["percent"] == 55
    assert w["monthly"]["percent"] == 100.0 and w["monthly"]["resets_at"] is None                     # clamped to 100
    assert w["five_hour"]["via_gateway"] == {"requests": 1, "tokens": 1500} and r[go_n]["billing"] == "subscription"
    c = {x["key"]: x for x in r[cx_n]["windows"]}
    assert c["five_hour"]["percent"] == 30 and c["seven_day"]["percent"] == 80 and c["seven_day"]["resets_at"] and r[cx_n]["extra"]["plan"] == "plus"
    assert r[cx_n]["extra"]["credits"] == {"has_credits": True, "unlimited": False, "balance": "12.5"}
    a = {x["key"]: x for x in r[cl_n]["windows"]}
    assert [x["key"] for x in r[cl_n]["windows"]] == ["five_hour", "seven_day", "seven_day_sonnet"] and a["five_hour"]["percent"] == 42
    assert r[zen_n]["windows"] == [] and "Pay-as-you-go" in r[zen_n]["note"] and r[zen_n]["billing"] == "payg"
    assert ("chatgpt.com", "/backend-api/wham/usage", "Bearer acces") in vendors.calls                 # the account's token is used ...
    body = json.dumps(r)
    assert "secret" not in body                                                                       # ... and never shown


def test_a_failing_vendor_is_reported_without_breaking_the_rest_and_calls_are_cached(client, vendors):
    cx, cx_n = account_ep("codex-fail", "codex-oauth", "codex")
    cl, cl_n = account_ep("claude-ok", "claude-oauth", "claude")
    vendors.fail = {"chatgpt.com"}
    r = {x["name"]: x for x in client.get("/api/cost/limits").json()}
    assert r[cx_n]["error"] == "ChatGPT answered HTTP 500" and r[cx_n]["windows"] == [] and "boom" not in json.dumps(r) and "sk-secret" not in json.dumps(r)
    assert r[cl_n]["error"] is None and r[cl_n]["windows"]
    n = len(vendors.calls)
    client.get("/api/cost/limits")
    assert len(vendors.calls) == n                                                                  # a minute of caching: no new vendor calls
    vendors.fail = set()
    client.get("/api/cost/limits?refresh=true")                                                      # refresh is rate limited too (10 s)
    assert len(vendors.calls) == n
    plan_limits._cache.clear()
    r = {x["name"]: x for x in client.get("/api/cost/limits?refresh=true").json()}
    assert r[cx_n]["error"] is None and r[cx_n]["windows"]


def test_an_account_that_needs_sign_in_says_so(client, vendors):
    cx, cx_n = account_ep("codex-dead", "codex-oauth", "codex")
    with Session(db.engine) as s:
        a = s.get(OAuthAccount, cx); a.needs_login = True; s.add(a); s.commit()
    r = {x["name"]: x for x in client.get("/api/cost/limits").json()}
    assert "sign in" in r[cx_n]["error"].lower() and r[cx_n]["windows"] == []


class Upstream:
    def __init__(self):
        self.usage = {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020, "prompt_tokens_details": {"cached_tokens": 640}}

    def handler(self, request: httpx.Request):
        body = json.loads(request.content or b"{}")
        if body.get("stream"):
            chunks = [{"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
                      {"id": "c", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": self.usage}]
            return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}], "usage": self.usage})


def test_cached_prompt_tokens_are_recorded_for_plain_and_streamed_requests(client, monkeypatch):
    up = Upstream()
    monkeypatch.setattr(proxy, "client", httpx.AsyncClient(transport=httpx.MockTransport(up.handler)))
    ep, name, (mid,) = mk_ep("cached", None, manual=(1.0, 2.0))
    health.status[ep] = "online"
    g = client.post("/api/gateways", json={"name": f"c-{time.time_ns()}", "models": [{"public_name": "cm", "members": [{"model_id": mid}]}]}).json()
    url = f"/api/admin-gw/{g['slug']}/v1/chat/completions"
    assert client.post(url, json={"model": "cm", "messages": [{"role": "user", "content": "x"}]}).status_code == 200
    assert client.post(url, json={"model": "cm", "stream": True, "messages": [{"role": "user", "content": "y"}]}).status_code == 200
    time.sleep(0.3)
    with Session(db.engine) as s:
        got = [(r.prompt_tokens, r.completion_tokens, r.cached_tokens) for r in s.exec(select(RequestLog).where(RequestLog.endpoint_id == ep)).all()]
    assert got == [(1000, 20, 640), (1000, 20, 640)]
    # 2 requests x (360 fresh x 1.0 + 640 cached x 1.0 (no cache price: input price) + 20 x 2.0) / 1e6
    assert rows(client, 1)[1][name]["cost"] == pytest.approx(2 * (1000 * 1.0 + 20 * 2.0) / 1e6)


# ------------------------------------------------------------------ the background check and the cache-only views
import asyncio
import logging


@pytest.fixture
def watched(vendors):
    with Session(db.engine) as s:   # only this test's accounts are checked: drop plan endpoints that earlier tests left behind
        for e in s.exec(select(Endpoint).where(Endpoint.provider.in_(list(plan_limits.PLANNED)))).all():
            a = s.get(OAuthAccount, e.id)
            if a:
                s.delete(a)
            s.delete(e)
        s.commit()
    plan_limits._cache.clear()
    plan_limits._failures.clear()
    plan_limits._not_before.clear()
    yield vendors
    plan_limits._failures.clear()
    plan_limits._not_before.clear()


def usage_calls(v):
    return len([c for c in v.calls if c[1].endswith("/usage")])


def accounts_view(client):
    return {a["name"]: a for a in client.get("/api/limits").json()["accounts"]}


def test_background_check_fills_the_cache_and_the_views_never_call_a_vendor(client, watched, caplog):
    cl, cl_n = account_ep("claude-bg", "claude-oauth", "claude")
    cx, cx_n = account_ep("codex-bg", "codex-oauth", "codex")
    go, go_n, _ = mk_ep("go-bg", "opencode-go", CATALOG)
    with Session(db.engine) as s:
        e = s.get(Endpoint, go); e.api_key = "oc-secret-key"; s.add(e); s.commit()
    assert accounts_view(client)[cl_n]["windows"] == [] and accounts_view(client)[cl_n]["fetched_at"] is None      # nothing known before the first check
    with caplog.at_level(logging.INFO, logger="smartgateway.limits"):
        assert asyncio.run(plan_limits.poll_all()) >= 3
    n = usage_calls(watched)
    a = accounts_view(client)
    assert [w["key"] for w in a[cl_n]["windows"]] == ["five_hour", "seven_day", "seven_day_sonnet"] and a[cl_n]["windows"][0]["percent"] == 42
    assert {w["key"]: w["percent"] for w in a[cx_n]["windows"]} == {"five_hour": 30, "seven_day": 80} and a[cx_n]["plan"] == "plus"
    assert [w["key"] for w in a[go_n]["windows"]] == ["five_hour", "seven_day", "monthly"] and a[cl_n]["fetched_at"] and a[cl_n]["stale"] is False
    for _ in range(3):                                                         # dashboards poll these: no vendor traffic
        client.get("/api/limits"); client.get("/api/endpoints")
    assert usage_calls(watched) == n
    ep = {e["id"]: e for e in client.get("/api/endpoints").json()}
    assert ep[cl]["limits"]["windows"][0]["percent"] == 42 and ep[cl]["limits"]["windows"][0]["resets_at"].startswith("2026-11-27T12:00")   # on the endpoint card
    assert ep[cx]["limits"]["windows"] and ep[go]["limits"]["windows"] and "limits" not in mk_ep_out(client, "plain")
    assert "42.0%" in caplog.text and "secret" not in caplog.text and "oc-secret" not in caplog.text            # the log carries percentages only
    assert "secret" not in json.dumps(client.get("/api/limits").json()) and "secret" not in json.dumps(client.get("/api/endpoints").json())


def mk_ep_out(client, name):
    ep, n, _ = mk_ep(name)
    return [e for e in client.get("/api/endpoints").json() if e["id"] == ep][0]


def test_a_failed_check_keeps_the_last_good_numbers_flagged_as_stale_and_backs_off(client, watched):
    cl, cl_n = account_ep("claude-stale", "claude-oauth", "claude")
    asyncio.run(plan_limits.poll_all())
    good = accounts_view(client)[cl_n]
    assert good["error"] is None and good["windows"][0]["percent"] == 42
    watched.fail = {"api.anthropic.com"}
    asyncio.run(plan_limits.poll_all(respect_backoff=False))
    bad = accounts_view(client)[cl_n]
    assert bad["stale"] is True and bad["error"] == "Anthropic answered HTTP 500" and bad["windows"] == good["windows"] and bad["last_ok"] == good["fetched_at"]
    assert "boom" not in json.dumps(bad) and "sk-secret" not in json.dumps(bad)
    n = usage_calls(watched)
    asyncio.run(plan_limits.poll_all())                                        # a failing account is left alone for a while
    assert usage_calls(watched) == n and plan_limits._failures[cl] == 1 and plan_limits._not_before[cl] > time.time() + plan_limits.INTERVAL
    plan_limits._not_before.clear()
    asyncio.run(plan_limits.poll_all())                                        # ... then asked again, and a second failure waits longer
    assert usage_calls(watched) == n + 1 and plan_limits._failures[cl] == 2
    watched.fail = set()
    plan_limits._not_before.clear()
    asyncio.run(plan_limits.poll_all())                                        # recovery clears the flags
    ok = accounts_view(client)[cl_n]
    assert ok["error"] is None and ok["stale"] is False and cl not in plan_limits._failures


def test_refresh_now_is_rate_limited_and_an_account_without_login_is_not_asked(client, watched):
    cl, cl_n = account_ep("claude-refresh", "claude-oauth", "claude")
    dead, dead_n = account_ep("codex-dead2", "codex-oauth", "codex")
    with Session(db.engine) as s:
        a = s.get(OAuthAccount, dead); a.needs_login = True; s.add(a); s.commit()
    r = client.post("/api/limits/refresh").json()
    rows = {a["name"]: a for a in r["accounts"]}
    assert rows[cl_n]["windows"] and "sign in" in rows[dead_n]["error"].lower() and rows[dead_n]["windows"] == []
    n = usage_calls(watched)
    client.post("/api/limits/refresh"); client.post("/api/limits/refresh")
    assert usage_calls(watched) == n                                            # within 10 s nothing is asked again
    assert r["interval"] >= 60 and r["polling"] is True


def test_checks_use_the_claude_code_user_agent_only_when_the_identity_switch_is_on(client, watched, monkeypatch):
    seen = []
    orig = watched.handler

    def spy(request):
        if request.url.host == "api.anthropic.com":
            seen.append(request.headers.get("user-agent", ""))
        return orig(request)
    monkeypatch.setattr(proxy, "client", httpx.AsyncClient(transport=httpx.MockTransport(spy)))
    cl, cl_n = account_ep("claude-ua", "claude-oauth", "claude")
    monkeypatch.delenv("SG_CLAUDE_CODE_IDENTITY", raising=False)
    asyncio.run(plan_limits.limits_for(cl, force=True))
    monkeypatch.setenv("SG_CLAUDE_CODE_IDENTITY", "1")
    asyncio.run(plan_limits.limits_for(cl, force=True))
    assert not seen[0].startswith("claude-code") and seen[1].startswith("claude-code")
