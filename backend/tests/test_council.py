import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import council, db, proxy, retention
from app.main import app
from app.models import CouncilRun, Endpoint, Model, RequestLog
from app.router import health


# ------------------------------------------------------------------ pure helpers
def test_parse_ranking_variants():
    v = ["A", "B", "C"]
    assert council.parse_ranking("good stuff\nFINAL RANKING:\n1. Response C\n2. Response A\n3. Response B", v) == ["C", "A", "B"]
    assert council.parse_ranking("Response A is nice.\nfinal ranking:\n1) **Response B**\n2) Response B\n3) Response Z", v) == ["B"]  # dupes, unknown letters dropped
    assert council.parse_ranking("1. Response B\n2. Response A\n3. Response C", v) == ["B", "A", "C"]      # no marker but a complete list
    assert council.parse_ranking("Response A is better than Response B.", v) is None                      # prose only
    assert council.parse_ranking("", v) is None


def test_aggregate_orders_by_mean_rank_then_first_places():
    answers = [{"idx": 0, "label": "x"}, {"idx": 1, "label": "y"}, {"idx": 2, "label": "z"}]
    reviews = [{"order_idx": [1, 2], "n_ranked": 2}, {"order_idx": [1, 0], "n_ranked": 2}, {"order_idx": [2, 0], "n_ranked": 2}]
    rows = council.aggregate(answers, reviews)
    assert [r["label"] for r in rows] == ["y", "z", "x"] and rows[0]["first_places"] == 2 and rows[2]["avg_rank"] == 1.0
    assert council.aggregate(answers, [])[0]["avg_rank"] is None


def test_normalize_validation():
    role = lambda i: {"gateway_id": i, "model": "m"}
    ok = council.normalize({"councillors": [role(1), role(2)], "chairman": role(3)})
    assert ok["review_enabled"] and ok["min_answers"] == 2 and ok["store_runs"] is False and ok["reviewers"] == "councillors"
    with pytest.raises(ValueError):
        council.normalize({"councillors": [role(1)], "chairman": role(3)})
    with pytest.raises(ValueError):
        council.normalize({"councillors": [role(i) for i in range(9)], "chairman": role(3)})
    with pytest.raises(ValueError):
        council.normalize({"councillors": [role(1), role(2)]})
    with pytest.raises(ValueError):
        council.normalize({"councillors": [role(1), role(2)], "chairman": role(3), "review_prompt": "no placeholders"})


# ------------------------------------------------------------------ end to end
@pytest.fixture(scope="module")
def client():
    db.init_db()
    return TestClient(app)


class Fake:
    def __init__(self):
        self.calls: list[dict] = []
        self.fail: set = set()
        self.rank_text = "Review text.\n\nFINAL RANKING:\n1. Response A\n2. Response B"
        self.chair_ok = True


@pytest.fixture
def fake(monkeypatch):
    f = Fake()

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        model, prompt = body["model"], body["messages"][-1]["content"]
        kind = "chair" if "chairman of a council" in prompt else "review" if "FINAL RANKING" in prompt else "answer"
        f.calls.append({"model": model, "kind": kind, "prompt": prompt, "stream": bool(body.get("stream"))})
        if model in f.fail or (kind == "chair" and not f.chair_ok):
            return httpx.Response(500, text="boom")
        text = {"answer": f"answer-from-{model}", "review": f.rank_text, "chair": f"final-by-{model}"}[kind]
        if body.get("stream"):
            parts = [{"id": "c", "object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {"content": text[:6]}}]},
                     {"id": "c", "object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {"content": text[6:]}, "finish_reason": "stop"}]},
                     {"id": "c", "object": "chat.completion.chunk", "model": model, "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}]
            sse = "".join(f"data: {json.dumps(p)}\n\n" for p in parts) + "data: [DONE]\n\n"
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "model": model,
                                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})

    monkeypatch.setattr(proxy, "client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return f


def inner_gateway(client, upstream_id):
    with Session(db.engine) as s:
        ep = Endpoint(name="council-ep", base_url="http://fake/v1", status="online")
        s.add(ep); s.commit(); s.refresh(ep)
        m = Model(endpoint_id=ep.id, upstream_id=upstream_id, capabilities=["chat"], max_slots=8, context_length=32000)
        s.add(m); s.commit(); s.refresh(m)
        ep_id, m_id = ep.id, m.id
    health.status[ep_id] = "online"
    r = client.post("/api/gateways", json={"name": f"inner-{upstream_id}-{time.time_ns()}", "models": [{"public_name": "m", "members": [{"model_id": m_id}]}]})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def team(client):
    ids = {n: inner_gateway(client, n) for n in ("up-a", "up-b", "up-c", "up-chair")}
    yield ids
    for g in list(ids.values()):
        client.delete(f"/api/gateways/{g['id']}?force=true")


def role(g):
    return {"gateway_id": g["id"], "model": "m"}


def make_council(client, team, **cfg):
    base = {"councillors": [role(team[n]) for n in ("up-a", "up-b", "up-c")], "chairman": role(team["up-chair"])}
    body = {"name": f"council-{time.time_ns()}", "kind": "council", "council": {**base, **cfg}, "models": [{"public_name": "senate", "members": []}]}
    r = client.post("/api/gateways", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def ask(client, gw, content="What is 2+2?", headers=None, **kw):
    return client.post(f"/api/admin-gw/{gw['slug']}/v1/chat/completions", headers=headers or {},
                       json={"model": "senate", "messages": [{"role": "user", "content": content}], **kw})


def test_full_flow_details_usage_and_anonymisation(client, fake, team):
    gw = make_council(client, team)
    assert gw["kind"] == "council" and gw["models"][0]["public_name"] == "senate" and gw["models"][0]["members"] == []
    r = ask(client, gw, headers={"X-SG-Council-Details": "1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["model"] == "senate" and d["choices"][0]["message"]["content"] == "final-by-up-chair"
    kinds = [c["kind"] for c in fake.calls]
    assert kinds.count("answer") == 3 and kinds.count("review") == 3 and kinds.count("chair") == 1
    assert d["usage"]["prompt_tokens"] == 7 * 5 and d["usage"]["completion_tokens"] == 7 * 3          # every call is summed
    sg = d["sg_council"]
    assert [a["label"].split("/")[0].startswith("inner-up-") for a in sg["stage1"]] == [True] * 3 and len(sg["reviews"]) == 3
    assert all(len(rv["ranking"]) == 2 and rv["shown"] == 2 for rv in sg["reviews"])
    # reviewers see only the other two answers, anonymised: no model names, no own answer
    for c in (c for c in fake.calls if c["kind"] == "review"):
        assert "inner-" not in c["prompt"] and "Response A" in c["prompt"] and "Response B" in c["prompt"] and "Response C" not in c["prompt"]
        assert c["prompt"].count("answer-from-") == 2 and f"answer-from-{c['model']}" not in c["prompt"]
    chair = next(c for c in fake.calls if c["kind"] == "chair")
    assert all(f"answer-from-up-{x}" in chair["prompt"] for x in "abc")                               # chairman sees every answer
    log = client.get(f"/api/logs?gateway_id={gw['id']}&limit=1").json()[0]
    assert log["requirements"] == "council" and log["attempts"] == 7 and log["ok"] and log["council_run_id"] is None
    client.delete(f"/api/gateways/{gw['id']}")


def test_details_hidden_unless_requested(client, fake, team):
    gw = make_council(client, team)
    assert "sg_council" not in ask(client, gw).json()
    client.delete(f"/api/gateways/{gw['id']}")
    gw = make_council(client, team, details_default=True)
    assert "sg_council" in ask(client, gw).json()
    client.delete(f"/api/gateways/{gw['id']}")


def test_review_can_be_disabled_and_separate_reviewers(client, fake, team):
    gw = make_council(client, team, review_enabled=False)
    assert ask(client, gw).status_code == 200 and not [c for c in fake.calls if c["kind"] == "review"]
    client.delete(f"/api/gateways/{gw['id']}")
    fake.calls.clear()
    gw = make_council(client, team, reviewers=[role(team["up-chair"])])
    ask(client, gw)
    reviews = [c for c in fake.calls if c["kind"] == "review"]
    assert len(reviews) == 1 and reviews[0]["prompt"].count("answer-from-") == 3                       # a separate reviewer sees all three
    client.delete(f"/api/gateways/{gw['id']}")


def test_failed_councillor_is_tolerated_and_too_few_is_502(client, fake, team):
    gw = make_council(client, team)
    fake.fail = {"up-b"}
    r = ask(client, gw, headers={"X-SG-Council-Details": "1"})
    assert r.status_code == 200
    s1 = r.json()["sg_council"]["stage1"]
    assert sum(a["ok"] for a in s1) == 2 and sum(1 for a in s1 if a["error"]) == 1
    assert len([c for c in fake.calls if c["kind"] == "review"]) == 2                                  # only successful councillors review
    fake.fail = {"up-a", "up-b"}
    r = ask(client, gw)
    assert r.status_code == 502 and r.json()["error"]["type"] == "council_failed" and len(r.json()["error"]["details"]["errors"]) == 2
    assert client.get(f"/api/logs?gateway_id={gw['id']}&limit=1").json()[0]["ok"] is False
    client.delete(f"/api/gateways/{gw['id']}")


def test_chairman_failure_falls_back_to_best_ranked(client, fake, team):
    gw = make_council(client, team)
    fake.chair_ok = False
    r = ask(client, gw)
    assert r.status_code == 200 and r.headers["X-SG-Council"] == "chairman-fallback"
    assert r.json()["choices"][0]["message"]["content"].startswith("answer-from-")
    client.delete(f"/api/gateways/{gw['id']}")


def sse(r):
    out = []
    for line in r.text.splitlines():
        if line.startswith("data:") and "[DONE]" not in line:
            out.append(json.loads(line[5:]))
    return out


def test_streaming_without_progress_forwards_the_chairman(client, fake, team):
    gw = make_council(client, team)
    r = ask(client, gw, stream=True, headers={"X-SG-Council-Details": "1"})
    assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]
    ev = sse(r)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in ev if c.get("choices") and c["choices"][0].get("delta"))
    assert text == "final-by-up-chair" and not any((c.get("choices") or [{}])[0].get("delta", {}).get("reasoning_content") for c in ev if c.get("choices"))
    assert all(c.get("model") == "senate" for c in ev)
    assert next(c for c in fake.calls if c["kind"] == "chair")["stream"] is True
    usage = [c for c in ev if c.get("usage")]
    assert len(usage) == 1 and usage[0]["usage"]["prompt_tokens"] == 6 * 5 + 7
    assert ev[-1]["sg_council"]["chairman"]["ok"] and len(ev[-1]["sg_council"]["stage1"]) == 3
    client.delete(f"/api/gateways/{gw['id']}")


def test_streaming_with_progress_and_failure(client, fake, team):
    gw = make_council(client, team, stream_progress=True)
    ev = sse(ask(client, gw, stream=True))
    prog = "".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in ev if c.get("choices"))
    assert "Stage 1" in prog and "Stage 2" in prog and "final-by-up-chair" in "".join(c["choices"][0]["delta"].get("content", "") for c in ev if c.get("choices"))
    assert not any("sg_council" in c for c in ev)
    fake.fail = {"up-a", "up-b"}
    r = ask(client, gw, stream=True)
    assert r.status_code == 200 and "council_failed" in r.text and r.text.strip().endswith("[DONE]")  # the stream has started: error event
    client.delete(f"/api/gateways/{gw['id']}")


def test_store_runs_off_and_on_and_retention(client, fake, team):
    with Session(db.engine) as s:
        before = len(s.exec(select(CouncilRun)).all())
    gw = make_council(client, team)
    ask(client, gw)
    with Session(db.engine) as s:
        assert len(s.exec(select(CouncilRun)).all()) == before                                          # nothing stored by default
    client.delete(f"/api/gateways/{gw['id']}")
    gw = make_council(client, team, store_runs=True)
    r = ask(client, gw, "remember this question")
    run_id = int(r.headers["X-SG-Council-Run"])
    run = client.get(f"/api/council-runs/{run_id}").json()
    assert run["status"] == "done" and run["final"] == "final-by-up-chair" and "remember this question" in run["question"]
    assert len(run["details"]["stage1"]) == 3 and client.get(f"/api/council-runs?gateway_id={gw['id']}").json()[0]["id"] == run_id
    assert client.get(f"/api/logs?gateway_id={gw['id']}&limit=1").json()[0]["council_run_id"] == run_id
    with Session(db.engine) as s:
        s.get(CouncilRun, run_id).ts = time.time() - 40 * 86400; s.commit()
    assert retention.prune_once({"council_retention_days": 30}).get("councilrun", 0) >= 1
    assert client.get(f"/api/council-runs/{run_id}").status_code == 404
    client.delete(f"/api/gateways/{gw['id']}")


def test_config_validation_references_and_delete_protection(client, fake, team):
    base = {"councillors": [role(team["up-a"]), role(team["up-b"])], "chairman": role(team["up-chair"])}
    mk = lambda **c: client.post("/api/gateways", json={"name": f"v-{time.time_ns()}", "kind": "council", "council": {**base, **c}, "models": [{"public_name": "s", "members": []}]})
    assert mk(councillors=[role(team["up-a"])]).status_code == 400                                      # needs at least two
    assert mk(chairman={"gateway_id": 99999999, "model": "m"}).status_code == 400                       # unknown gateway
    assert mk(chairman={"gateway_id": team["up-chair"]["id"], "model": "nope"}).status_code == 400      # unknown model
    r = client.post("/api/gateways", json={"name": "x", "kind": "council", "council": base, "models": []})
    assert r.status_code == 400                                                                         # exactly one public model
    gw = mk().json()
    r = client.delete(f"/api/gateways/{team['up-a']['id']}")
    assert r.status_code == 409 and gw["name"] in r.json()["detail"]["used_by"]
    # a council cannot include itself, directly or through another council
    put = lambda g, cfg: client.put(f"/api/gateways/{g['id']}", json={"name": g["name"], "slug": g["slug"], "kind": "council", "council": cfg, "models": [{"public_name": "s", "members": []}]})
    assert put(gw, {**base, "councillors": [{"gateway_id": gw["id"], "model": "s"}, role(team["up-b"])]}).status_code == 400
    outer = client.post("/api/gateways", json={"name": f"outer-{time.time_ns()}", "kind": "council", "council": {**base, "councillors": [{"gateway_id": gw["id"], "model": "s"}, role(team["up-b"])]},
                                                "models": [{"public_name": "s", "members": []}]})
    assert outer.status_code == 200
    assert put(gw, {**base, "councillors": [{"gateway_id": outer.json()["id"], "model": "s"}, role(team["up-b"])]}).status_code == 400
    assert client.put(f"/api/gateways/{gw['id']}", json={"name": "x", "kind": "router", "models": []}).status_code == 400   # kind cannot change
    client.delete(f"/api/gateways/{outer.json()['id']}")
    client.delete(f"/api/gateways/{gw['id']}")


def test_unsupported_routes_and_nesting(client, fake, team):
    gw = make_council(client, team)
    assert client.post(f"/api/admin-gw/{gw['slug']}/v1/chat/completions", json={"model": "senate", "tools": [{}], "messages": [{"role": "user", "content": "x"}]}).status_code == 400
    assert client.post(f"/api/admin-gw/{gw['slug']}/v1/embeddings", json={"model": "senate", "input": "x"}).status_code == 400
    assert client.get(f"/api/admin-gw/{gw['slug']}/v1/models").json()["data"][0]["id"] == "senate"
    # a council can use another council as a councillor
    outer = client.post("/api/gateways", json={"name": f"outer-{time.time_ns()}", "kind": "council",
                                                "council": {"councillors": [{"gateway_id": gw["id"], "model": "senate"}, role(team["up-b"])], "chairman": role(team["up-chair"])},
                                                "models": [{"public_name": "senate", "members": []}]}).json()
    r = ask(client, outer)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "final-by-up-chair"
    client.delete(f"/api/gateways/{outer['id']}")
    client.delete(f"/api/gateways/{gw['id']}")


# ------------------------------------------------------------------ endpoint-model roles
def raw_model(upstream_id, enabled=True):
    with Session(db.engine) as s:
        ep = Endpoint(name="raw-ep", base_url="http://fake/v1", status="online")
        s.add(ep); s.commit(); s.refresh(ep)
        m = Model(endpoint_id=ep.id, upstream_id=upstream_id, capabilities=["chat"], max_slots=8, context_length=32000, enabled=enabled)
        s.add(m); s.commit(); s.refresh(m)
        health.status[ep.id] = "online"
        return m.id


def test_roles_can_be_endpoint_models_or_gateways_mixed(client, fake, team):
    ma, mc = raw_model("raw-a"), raw_model("raw-chair")
    body = {"name": f"mixed-{time.time_ns()}", "kind": "council", "models": [{"public_name": "senate", "members": []}],
            "council": {"councillors": [{"model_id": ma}, role(team["up-b"]), role(team["up-c"])], "chairman": {"model_id": mc}}}
    r = client.post("/api/gateways", json=body)
    assert r.status_code == 200, r.text
    gw = r.json()
    assert gw["council"]["councillors"][0] == {"model_id": ma} and gw["council"]["chairman"] == {"model_id": mc}
    # the hidden gateway behind endpoint-model roles is never listed (unless asked) and cannot be called or edited
    assert "__council__" not in [g["slug"] for g in client.get("/api/gateways").json()]
    hidden = next(g for g in client.get("/api/gateways?all=true").json() if g["slug"] == "__council__")
    assert sorted(m["public_name"] for m in hidden["models"]) == sorted([f"model-{ma}", f"model-{mc}"])
    assert client.post("/api/admin-gw/__council__/v1/chat/completions", json={"model": f"model-{ma}", "messages": [{"role": "user", "content": "x"}]}).status_code == 404
    assert client.put(f"/api/gateways/{hidden['id']}", json={"name": "x", "models": []}).status_code == 400
    r = ask(client, gw, headers={"X-SG-Council-Details": "1"})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "final-by-raw-chair"
    assert {c["model"] for c in fake.calls if c["kind"] == "answer"} == {"raw-a", "up-b", "up-c"}   # each role reached its own upstream model
    labels = [a["label"] for a in r.json()["sg_council"]["stage1"]]
    assert labels[0] == "raw-ep/raw-a" and r.json()["sg_council"]["chairman"]["label"] == "raw-ep/raw-chair"
    assert [c for c in fake.calls if c["kind"] == "review"] and all("raw-ep" not in c["prompt"] for c in fake.calls if c["kind"] == "review")
    # streaming chairman through a model role
    ev = sse(ask(client, gw, stream=True))
    assert "final-by-raw-chair" in "".join(c["choices"][0]["delta"].get("content", "") for c in ev if c.get("choices"))
    # editing the council drops models nobody uses any more; deleting it removes the hidden models
    body["council"]["councillors"][0] = role(team["up-a"])
    assert client.put(f"/api/gateways/{gw['id']}", json={**body, "slug": gw["slug"]}).status_code == 200
    hidden = next(g for g in client.get("/api/gateways?all=true").json() if g["slug"] == "__council__")
    assert [m["public_name"] for m in hidden["models"]] == [f"model-{mc}"]
    client.delete(f"/api/gateways/{gw['id']}")
    assert not [g for g in client.get("/api/gateways?all=true").json() if g["slug"] == "__council__"]   # nothing uses it: the hidden gateway is gone


def test_model_roles_are_validated(client, fake, team):
    mk = lambda **c: client.post("/api/gateways", json={"name": f"v-{time.time_ns()}", "kind": "council", "models": [{"public_name": "s", "members": []}],
                                                         "council": {"councillors": [role(team["up-a"]), role(team["up-b"])], "chairman": role(team["up-chair"]), **c}})
    assert mk(chairman={"model_id": 99999999}).status_code == 400
    r = mk(chairman={"model_id": raw_model("off", enabled=False)})
    assert r.status_code == 400 and "disabled" in r.json()["detail"]


def test_failure_details_only_for_the_admin_route(client, fake, team):
    """Upstream error text per councillor is shown on the admin route (verbose) but never on the public gateway URL."""
    gw = make_council(client, team)
    fake.fail = {"up-a", "up-b"}
    body = {"model": "senate", "messages": [{"role": "user", "content": "q"}]}
    admin = client.post(f"/api/admin-gw/{gw['slug']}/v1/chat/completions", json=body)
    public = client.post(f"/gw/{gw['slug']}/v1/chat/completions", json=body)
    assert admin.status_code == public.status_code == 502
    assert len(admin.json()["error"]["details"]["errors"]) == 2
    assert "details" not in public.json()["error"] and "boom" not in public.text and "HTTP 500" not in public.text
    client.delete(f"/api/gateways/{gw['id']}")
