"""LLM council gateway: several models answer, review each other anonymously, and a chairman writes the final answer.

Every role points at the public model of an existing gateway, and each call goes through proxy.dispatch, so that gateway's routing,
failover, slots, caching, guardrails and request log all apply."""
import asyncio
import contextvars
import json
import random
import re
import string
import time
import uuid
from typing import Optional

from fastapi.responses import JSONResponse, StreamingResponse
from sqlmodel import select

from .db import session
from .runtime_ctx import verbose
from .models import CouncilRun, Endpoint, Gateway, GatewayModel, Member, Model, RequestLog

MAX_DEPTH = 3
COUNCIL_SLUG = "__council__"   # hidden gateway that holds one public model (with one upstream member) per endpoint model used as a council role
MAX_COUNCILLORS = 8
_chain: contextvars.ContextVar[tuple] = contextvars.ContextVar("council_chain", default=())
PASS_PARAMS = ("temperature", "top_p", "max_tokens", "max_completion_tokens", "seed", "stop")

REVIEW_PROMPT = """You are reviewing anonymous answers from several assistants to the conversation below.

Conversation:
{question}

{responses}

First evaluate each response in a short paragraph: what is accurate, what is wrong or missing, and how insightful it is. Then give your final ranking, best first, in exactly this format and nothing after it:

FINAL RANKING:
1. Response X
2. Response Y
"""
CHAIRMAN_PROMPT = """You are the chairman of a council of AI assistants. Several assistants answered the conversation below and reviewed each other's answers. Use their answers and the peer review to write the single best final answer for the user. Answer the user directly: do not mention the council, the ranking or the other assistants.

Conversation:
{question}

Answers:
{responses}

{ranking}
"""
PROMPT_KEYS = {"review_prompt": ("{question}", "{responses}"), "chairman_prompt": ("{question}", "{responses}")}


# ---------------------------------------------------------------- config
def normalize(cfg: Optional[dict]) -> dict:
    """Validate and complete a council config. Raises ValueError with a readable message."""
    c = dict(cfg or {})
    def role(r):
        if r.get("model_id") not in (None, ""):
            return {"model_id": int(r["model_id"])}
        return {"gateway_id": int(r["gateway_id"]), "model": str(r["model"]).strip()}
    try:
        councillors = [role(r) for r in (c.get("councillors") or [])]
        chairman = role(c["chairman"]) if c.get("chairman") else None
        rv = c.get("reviewers", "councillors")
        reviewers = "councillors" if rv in (None, "", "councillors") else [role(r) for r in rv]
    except (KeyError, TypeError, ValueError):
        raise ValueError("every role needs a gateway and a model")
    if not (2 <= len(councillors) <= MAX_COUNCILLORS):
        raise ValueError(f"a council needs between 2 and {MAX_COUNCILLORS} councillors")
    if chairman is None:
        raise ValueError("choose a chairman")
    if reviewers != "councillors" and len(reviewers) > MAX_COUNCILLORS:
        raise ValueError(f"at most {MAX_COUNCILLORS} reviewers")
    out = {
        "councillors": councillors, "chairman": chairman, "reviewers": reviewers,
        "review_enabled": bool(c.get("review_enabled", True)),
        "min_answers": max(1, min(int(c.get("min_answers") or 2), len(councillors))),
        "stage_timeout_s": max(5, min(int(c.get("stage_timeout_s") or 120), 900)),
        "stage_max_tokens": int(c["stage_max_tokens"]) if c.get("stage_max_tokens") else None,
        "stream_progress": bool(c.get("stream_progress", False)),
        "details_default": bool(c.get("details_default", False)),
        "store_runs": bool(c.get("store_runs", False)),
    }
    for k, need in PROMPT_KEYS.items():
        v = (c.get(k) or "").strip()
        if v and not all(n in v for n in need):
            raise ValueError(f"{k.replace('_', ' ')} must contain {' and '.join(need)}")
        out[k] = v or None
    return out


def roles_of(cfg: Optional[dict]) -> list[dict]:
    if not cfg:
        return []
    return list(cfg.get("councillors", [])) + ([cfg["chairman"]] if cfg.get("chairman") else []) + (cfg["reviewers"] if isinstance(cfg.get("reviewers"), list) else [])


def references(cfg: Optional[dict]) -> set[int]:
    """Gateways a council uses (roles that point at an endpoint model are not gateways)."""
    return {r["gateway_id"] for r in roles_of(cfg) if "gateway_id" in r}


def model_ids(cfg: Optional[dict]) -> set[int]:
    return {r["model_id"] for r in roles_of(cfg) if "model_id" in r}


def check_refs(s, cfg: dict, self_id: Optional[int]) -> None:
    """Every referenced gateway and public model exists, and the council does not (indirectly) include itself."""
    from .models import GatewayModel
    for r in roles_of(cfg):
        if "model_id" in r:
            m = s.get(Model, r["model_id"])
            if not m:
                raise ValueError(f"model {r['model_id']} does not exist")
            if not m.enabled:
                raise ValueError(f"model '{m.upstream_id}' is disabled: enable it on the Endpoints page first")
            continue
        g = s.get(Gateway, r["gateway_id"])
        if not g:
            raise ValueError(f"gateway {r['gateway_id']} does not exist")
        if self_id is not None and g.id == self_id:
            raise ValueError("a council cannot include itself")
        if not s.exec(select(GatewayModel).where(GatewayModel.gateway_id == g.id, GatewayModel.public_name == r["model"])).first():
            raise ValueError(f"gateway '{g.name}' has no model '{r['model']}'")
    if self_id is None:
        return
    seen, stack = set(), list(references(cfg))
    while stack:  # walk councils that are nested inside this one
        gid = stack.pop()
        if gid == self_id:
            raise ValueError("this would create a loop of councils")
        if gid in seen:
            continue
        seen.add(gid)
        g = s.get(Gateway, gid)
        if g and g.kind == "council":
            stack.extend(references(g.council))


def used_by(s, gateway_id: int) -> list[str]:
    return [g.name for g in s.exec(select(Gateway).where(Gateway.kind == "council")).all() if gateway_id in references(g.council)]


# ---------------------------------------------------------------- text helpers
def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content or "")


def render_conversation(messages: list, limit: int = 12000) -> str:
    msgs = [m for m in messages if isinstance(m, dict)]
    if len([m for m in msgs if m.get("role") != "system"]) == 1:
        u = next(m for m in msgs if m.get("role") != "system")
        sys_ = [f"System: {_text(m.get('content'))}" for m in msgs if m.get("role") == "system"]
        txt = "\n".join(sys_ + [_text(u.get("content"))])
    else:
        txt = "\n\n".join(f"{str(m.get('role', 'user')).capitalize()}: {_text(m.get('content'))}" for m in msgs)
    return txt[-limit:]


def letters(n: int) -> list[str]:
    return list(string.ascii_uppercase[:n])


def parse_ranking(text: str, valid: list[str]) -> Optional[list[str]]:
    """Letters in ranked order from a 'FINAL RANKING:' block (tolerant). None when nothing usable was found."""
    t = text or ""
    idx = t.upper().rfind("FINAL RANKING")
    section = t[idx:] if idx >= 0 else t
    found: list[str] = []
    for m in re.finditer(r"(?im)^\s*(?:\d+[.)]|[-*])?\s*\**\s*Response\s+([A-Z])\b", section):
        L = m.group(1)
        if L in valid and L not in found:
            found.append(L)
    if idx < 0 and len(found) < len(valid):  # no marker: only trust a complete list
        return None
    return found or None


def aggregate(answers: list[dict], reviews: list[dict]) -> list[dict]:
    """Mean normalised rank (0 = best) of each answer across the reviewers that ranked it; first places break ties."""
    pos: dict[int, list[float]] = {}
    firsts: dict[int, int] = {}
    for r in reviews:
        order = r.get("order_idx") or []
        n = r.get("n_ranked") or len(order)
        if n < 2:
            continue
        for rank, i in enumerate(order):
            pos.setdefault(i, []).append(rank / (n - 1))
            if rank == 0:
                firsts[i] = firsts.get(i, 0) + 1
    rows = []
    for a in answers:
        p = pos.get(a["idx"])
        rows.append({"idx": a["idx"], "label": a["label"], "avg_rank": round(sum(p) / len(p), 3) if p else None,
                     "first_places": firsts.get(a["idx"], 0), "votes": len(p or [])})
    rows.sort(key=lambda r: (r["avg_rank"] is None, r["avg_rank"] if r["avg_rank"] is not None else 0, -r["first_places"]))
    return rows


# ---------------------------------------------------------------- calls
def _gateway(gid: int) -> Optional[Gateway]:
    with session() as s:
        g = s.get(Gateway, gid)
        if g:
            s.expunge(g)
        return g


def model_name(model_id: int) -> str:
    return f"model-{model_id}"


def resolve(role: dict) -> tuple[Optional[Gateway], str]:
    """(gateway, public model) a role is called through. An endpoint-model role goes through the hidden council gateway."""
    if "model_id" in role:
        with session() as s:
            g = s.exec(select(Gateway).where(Gateway.slug == COUNCIL_SLUG)).first()
            if g:
                s.expunge(g)
        return g, model_name(role["model_id"])
    return _gateway(role["gateway_id"]), role["model"]


def sync_models() -> None:
    """Make the hidden gateway hold exactly one public model (one upstream member) for every endpoint model used by any council."""
    with session() as s:
        wanted: set[int] = set()
        for g in s.exec(select(Gateway).where(Gateway.kind == "council")).all():
            wanted |= model_ids(g.council)
        gw = s.exec(select(Gateway).where(Gateway.slug == COUNCIL_SLUG)).first()
        if not wanted:   # nothing uses it any more (or never did): remove the hidden gateway altogether
            if gw is not None:
                for gm in s.exec(select(GatewayModel).where(GatewayModel.gateway_id == gw.id)).all():
                    for mem in s.exec(select(Member).where(Member.gateway_model_id == gm.id)).all():
                        s.delete(mem)
                    s.delete(gm)
                s.delete(gw)
                s.commit()
            return
        if gw is None:
            gw = Gateway(slug=COUNCIL_SLUG, name="Council models", strategy="least_busy", queue_timeout=60.0, max_retries=2)
            s.add(gw)
            s.flush()
        have = {gm.public_name: gm for gm in s.exec(select(GatewayModel).where(GatewayModel.gateway_id == gw.id)).all()}
        want = {model_name(i): i for i in wanted if s.get(Model, i)}
        for name, gm in have.items():
            if name not in want:
                for mem in s.exec(select(Member).where(Member.gateway_model_id == gm.id)).all():
                    s.delete(mem)
                s.delete(gm)
        for name, mid in want.items():
            gm = have.get(name)
            if gm is None:
                gm = GatewayModel(gateway_id=gw.id, public_name=name)
                s.add(gm)
                s.flush()
            mems = s.exec(select(Member).where(Member.gateway_model_id == gm.id)).all()
            if len(mems) == 1 and mems[0].model_id == mid:
                continue
            for mem in mems:
                s.delete(mem)
            s.flush()
            s.add(Member(gateway_model_id=gm.id, model_id=mid))
        s.commit()


def _body(model: str, messages: list, client_body: dict, cfg: dict, stream: bool = False) -> dict:
    b = {k: client_body[k] for k in PASS_PARAMS if client_body.get(k) is not None}
    cap = cfg.get("stage_max_tokens")
    if cap:
        for k in ("max_tokens", "max_completion_tokens"):
            if k in b:
                b[k] = min(int(b[k]), cap)
        if "max_tokens" not in b and "max_completion_tokens" not in b:
            b["max_tokens"] = cap
    b.update(model=model, messages=messages, stream=stream)
    return b


def _usage(data: dict) -> dict:
    u = (data or {}).get("usage") or {}
    return {"prompt_tokens": int(u.get("prompt_tokens") or 0), "completion_tokens": int(u.get("completion_tokens") or 0)}


async def call(role: dict, messages: list, client_body: dict, cfg: dict, headers_in) -> dict:
    from . import proxy
    t0 = time.time()
    res = {"ok": False, "text": "", "error": None, "ms": 0, "usage": {"prompt_tokens": 0, "completion_tokens": 0}, "reasoning": ""}
    gw, pm = resolve(role)
    if gw is None:
        res["error"] = "gateway no longer exists"
        return res
    try:
        r = await asyncio.wait_for(proxy.dispatch(gw, None, "chat/completions", _body(pm, messages, client_body, cfg), None, headers_in),
                                   cfg["stage_timeout_s"])
        data = json.loads(bytes(r.body))
        if r.status_code >= 400:
            res["error"] = str((data.get("error") or {}).get("message") or data)[:300]
        else:
            msg = ((data.get("choices") or [{}])[0]).get("message") or {}
            res.update(ok=True, text=_text(msg.get("content")), usage=_usage(data), reasoning=msg.get("reasoning_content") or "")
            if not res["text"].strip():
                res.update(ok=False, error="empty answer")
    except asyncio.TimeoutError:
        res["error"] = f"timed out after {cfg['stage_timeout_s']} s"
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    res["ms"] = int((time.time() - t0) * 1000)
    return res


def role_label(role: dict, names: dict) -> str:
    if "model_id" in role:
        return names.get(("m", role["model_id"]), f"model {role['model_id']}")
    return f"{names.get(role['gateway_id'], role['gateway_id'])}/{role['model']}"


def _names() -> dict:
    """gateway id -> name, and ('m', model id) -> 'endpoint/upstream id'."""
    with session() as s:
        names: dict = {g.id: g.name for g in s.exec(select(Gateway)).all()}
        eps = {e.id: e.name for e in s.exec(select(Endpoint)).all()}
        for m in s.exec(select(Model)).all():
            names[("m", m.id)] = f"{eps.get(m.endpoint_id, '?')}/{m.upstream_id}"
    return names


def _label(role: dict, names: dict, seen: dict) -> str:
    base = role_label(role, names)
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base} #{seen[base]}"


def _sum(*usages) -> dict:
    return {"prompt_tokens": sum(u["prompt_tokens"] for u in usages), "completion_tokens": sum(u["completion_tokens"] for u in usages)}


async def stages(cfg: dict, messages: list, client_body: dict, headers_in, progress=None) -> dict:
    """Stage 1 (answers) and stage 2 (anonymous peer review). Returns the state the chairman works from."""
    emit = progress or (lambda _t: None)
    names = _names()
    seen: dict = {}
    roles = [{**r, "idx": i, "label": _label(r, names, seen)} for i, r in enumerate(cfg["councillors"])]
    question = render_conversation(messages)

    emit(f"Stage 1: asking {len(roles)} models…\n")
    done = 0

    async def ask(r):
        nonlocal done
        out = await call(r, messages, client_body, cfg, headers_in)
        done += 1
        emit(f"Stage 1: {done}/{len(roles)} answers in\n")
        return {**r, **out}

    answers = list(await asyncio.gather(*(ask(r) for r in roles)))
    good = [a for a in answers if a["ok"]]
    state = {"question": question, "answers": answers, "good": good, "reviews": [], "ranking": []}
    if len(good) < cfg["min_answers"]:
        return state

    if cfg["review_enabled"] and len(good) >= 2:
        if cfg["reviewers"] == "councillors":
            reviewers = [{**{k: a[k] for k in ("gateway_id", "model", "model_id") if k in a}, "idx": a["idx"], "label": a["label"]} for a in good]
        else:
            reviewers = [{"idx": None, "label": _label(r, names, seen), **r} for r in cfg["reviewers"]]
        tpl = cfg.get("review_prompt") or REVIEW_PROMPT
        emit(f"Stage 2: {len(reviewers)} reviewers ranking the answers…\n")
        rdone = 0

        async def review(rv):
            nonlocal rdone
            others = [a for a in good if a["idx"] != rv["idx"]]   # a councillor never reviews its own answer
            random.shuffle(others)                                 # independent order per reviewer
            ls = letters(len(others))
            body = "\n\n".join(f"Response {L}:\n{a['text'][:12000]}" for L, a in zip(ls, others))
            prompt = tpl.replace("{question}", question).replace("{responses}", body)
            out = await call(rv, [{"role": "user", "content": prompt}], client_body, cfg, headers_in)
            rec = {"reviewer": rv["label"], "ok": out["ok"], "text": out["text"], "error": out["error"], "ms": out["ms"], "usage": out["usage"],
                   "ranking": [], "order_idx": [], "n_ranked": len(others), "unparsed": False, "shown": len(others)}
            if out["ok"] and len(others) >= 2:
                order = parse_ranking(out["text"], ls)
                if order:
                    rec["order_idx"] = [others[ls.index(L)]["idx"] for L in order]
                    rec["ranking"] = [others[ls.index(L)]["label"] for L in order]
                else:
                    rec["unparsed"] = True
            rdone += 1
            emit(f"Stage 2: {rdone}/{len(reviewers)} reviews in\n")
            return rec

        state["reviews"] = list(await asyncio.gather(*(review(rv) for rv in reviewers)))
        state["ranking"] = aggregate(good, state["reviews"])
    else:
        state["ranking"] = [{"idx": a["idx"], "label": a["label"], "avg_rank": None, "first_places": 0, "votes": 0} for a in good]
    return state


def chairman_messages(cfg: dict, st: dict) -> list:
    answers = "\n\n".join(f"{a['label']}:\n{a['text'][:12000]}" for a in st["good"])
    parts = []
    ranked = [r for r in st["ranking"] if r["avg_rank"] is not None]
    if ranked:
        parts.append("Peer review ranking, best first: " + ", ".join(f"{r['label']} (score {r['avg_rank']:.2f}, lower is better)" for r in ranked))
    crit = [f"Review by {r['reviewer']}:\n{r['text'][:1500]}" for r in st["reviews"] if r["ok"] and r["text"]]
    if crit:
        parts.append("Reviews:\n" + "\n\n".join(crit))
    prompt = (cfg.get("chairman_prompt") or CHAIRMAN_PROMPT).replace("{question}", st["question"]).replace("{responses}", answers).replace("{ranking}", "\n\n".join(parts))
    return [{"role": "user", "content": prompt}]


def details(cfg: dict, st: dict, chair: dict, run_id: Optional[int]) -> dict:
    strip = lambda a: {"label": a["label"], "ok": a["ok"], "text": a["text"], "error": a["error"], "ms": a["ms"], "usage": a["usage"]}
    return {
        "run_id": run_id,
        "stage1": [strip(a) for a in st["answers"]],
        "reviews": [{k: r[k] for k in ("reviewer", "ok", "text", "error", "ms", "usage", "ranking", "unparsed", "shown")} for r in st["reviews"]],
        "ranking": [{k: r[k] for k in ("label", "avg_rank", "first_places", "votes")} for r in st["ranking"]],
        "chairman": chair,
    }


# ---------------------------------------------------------------- entry point
def _err(status: int, message: str, code: str, extra: Optional[dict] = None):
    """`extra` carries upstream error text, so it is only included for the signed-in admin (or SG_VERBOSE_ERRORS), like on the plain gateway."""
    return JSONResponse({"error": {"message": message, "type": code, **({"details": extra} if extra and verbose.get() else {})}}, status_code=status)


def _chunk(rid: str, model: str, delta: dict, finish=None, **extra) -> str:
    return "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}, separators=(",", ":")) + "\n\n"


async def run(gw: Gateway, gm, key, route: str, body: dict, headers_in, token):
    from . import proxy
    cfg = gw.council or {}
    if route != "chat/completions":
        return _err(400, "Council gateways only support chat completions", "invalid_request_error")
    if body.get("tools") or body.get("response_format"):
        return _err(400, "Council gateways do not support tools or response_format", "invalid_request_error")
    chain = _chain.get()
    if gw.id in chain or len(chain) >= MAX_DEPTH:
        return _err(400, "Councils are nested too deeply", "invalid_request_error")
    try:
        cfg = normalize(cfg)
    except ValueError as e:
        return _err(500, f"Council is misconfigured: {e}" if verbose.get() else "This council gateway is misconfigured.", "council_misconfigured")

    t0 = time.time()
    messages = list(body.get("messages") or [])
    want_details = cfg["details_default"] or headers_in.get("x-sg-council-details", "") == "1"
    is_stream = bool(body.get("stream"))
    rid = "chatcmpl-council-" + uuid.uuid4().hex[:12]
    run_id = None
    if cfg["store_runs"]:
        with session() as s:
            row = CouncilRun(gateway_id=gw.id, key_id=key.id if key else None, question=render_conversation(messages, 20000))
            s.add(row); s.commit(); s.refresh(row)
            run_id = row.id
    state: dict = {}
    calls = {"n": 0}

    async def finish(st: Optional[dict], chair: dict, final: str, usage: dict, status: int, error: Optional[str], stream: bool):
        total = int((time.time() - t0) * 1000)
        d = details(cfg, st, chair, run_id) if st else {}
        if run_id:
            def _w():
                with session() as s:
                    r = s.get(CouncilRun, run_id)
                    if r:
                        r.status, r.final, r.total_ms, r.details = ("done" if status == 200 else "failed"), final, total, d
                        s.add(r); s.commit()
            await asyncio.to_thread(_w)
        n_calls = (len(st["answers"]) + len(st["reviews"]) + 1) if st else 0
        await proxy.log_request(RequestLog(gateway_id=gw.id, key_id=key.id if key else None, public_model=gm.public_name, route=route,
                                           requirements="council", status=status, ok=status == 200, stream=stream, total_ms=total,
                                           prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"], attempts=n_calls,
                                           error=error, finish_reason="stop" if status == 200 else None,
                                           outcome="complete" if status == 200 else None, council_run_id=run_id))
        return d

    def failure_msg(st):
        return "; ".join(f"{a['label']}: {a['error']}" for a in st["answers"] if not a["ok"]) or "no answers"

    token_list = _chain.set(chain + (gw.id,))
    try:
        # ------------------------------------------------------------ streaming with progress: stages run inside the generator
        if is_stream and cfg["stream_progress"]:
            q: asyncio.Queue = asyncio.Queue()
            task = asyncio.create_task(stages(cfg, messages, body, headers_in, progress=q.put_nowait))

            async def gen():
                first = True
                _chain.set(chain + (gw.id,))
                try:
                    while not task.done() or not q.empty():
                        try:
                            msg = await asyncio.wait_for(q.get(), 10)
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"
                            continue
                        yield _chunk(rid, gm.public_name, {**({"role": "assistant"} if first else {}), "reasoning_content": msg})
                        first = False
                    st = await task
                    if len(st["good"]) < cfg["min_answers"]:
                        msg = f"Council failed: too few answers ({len(st['good'])}/{cfg['min_answers']}). {failure_msg(st)}"
                        await finish(st, {}, "", _sum(*[a["usage"] for a in st["answers"]]), 502, msg, True)
                        yield "data: " + json.dumps({"error": {"message": msg, "type": "council_failed"}}) + "\n\ndata: [DONE]\n\n"
                        return
                    async for piece in _chairman_stream(rid, cfg, gm, st, body, headers_in, run_id, want_details, finish, first):
                        yield piece
                finally:
                    if not task.done():
                        task.cancel()
                    token.release()
            sr = StreamingResponse(gen(), media_type="text/event-stream", headers={"X-SG-Council-Run": str(run_id or "")})
            sr._sg_stream = True
            return sr

        # ------------------------------------------------------------ everything else: stages 1 and 2 finish before any reply
        state = await stages(cfg, messages, body, headers_in)
        if len(state["good"]) < cfg["min_answers"]:
            msg = f"Council failed: only {len(state['good'])} of {len(state['answers'])} models answered (need {cfg['min_answers']})"
            await finish(state, {}, "", _sum(*[a["usage"] for a in state["answers"]]), 502, failure_msg(state), is_stream)
            return _err(502, msg, "council_failed", {"errors": [{"model": a["label"], "error": a["error"]} for a in state["answers"] if not a["ok"]]})

        if is_stream:
            async def gen2():
                _chain.set(chain + (gw.id,))
                try:
                    async for piece in _chairman_stream(rid, cfg, gm, state, body, headers_in, run_id, want_details, finish, True):
                        yield piece
                finally:
                    token.release()
            sr = StreamingResponse(gen2(), media_type="text/event-stream", headers={"X-SG-Council-Run": str(run_id or "")})
            sr._sg_stream = True
            return sr

        chair = await call(cfg["chairman"], chairman_messages(cfg, state), body, cfg, headers_in)
        fallback = not chair["ok"]
        final = chair["text"] if chair["ok"] else state["good"][_top(state)]["text"]
        usage = _sum(chair["usage"], *[a["usage"] for a in state["answers"]], *[r["usage"] for r in state["reviews"]])
        chair_info = {"label": role_label(cfg["chairman"], _names()),
                      "ok": chair["ok"], "ms": chair["ms"], "error": chair["error"], "fallback": fallback, "usage": chair["usage"]}
        d = await finish(state, chair_info, final, usage, 200, None, False)
        out = {"id": rid, "object": "chat.completion", "created": int(time.time()), "model": gm.public_name,
               "choices": [{"index": 0, "message": {"role": "assistant", "content": final}, "finish_reason": "stop"}],
               "usage": {**usage, "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"]}}
        if want_details:
            out["sg_council"] = d or details(cfg, state, chair_info, run_id)
        h = {"X-SG-Council-Run": str(run_id)} if run_id else {}
        if fallback:
            h["X-SG-Council"] = "chairman-fallback"
        return JSONResponse(out, headers=h)
    finally:
        _chain.reset(token_list)


def _top(st: dict) -> int:
    """Index in st['good'] of the best-ranked answer (first answer when nothing was ranked)."""
    ranked = [r for r in st["ranking"] if r["avg_rank"] is not None]
    best = ranked[0]["idx"] if ranked else st["good"][0]["idx"]
    return next(i for i, a in enumerate(st["good"]) if a["idx"] == best)


async def _chairman_stream(rid, cfg, gm, st, body, headers_in, run_id, want_details, finish, first):
    """Forward the chairman's own stream, then the optional details chunk. Falls back to the top answer if the chairman fails."""
    from . import proxy
    prior = _sum(*[a["usage"] for a in st["answers"]], *[r["usage"] for r in st["reviews"]])
    gw_c, pm_c = resolve(cfg["chairman"])
    label = role_label(cfg["chairman"], _names())
    b = _body(pm_c, chairman_messages(cfg, st), body, cfg, stream=True)
    b["stream_options"] = {"include_usage": True}
    final: list[str] = []
    cusage = {"prompt_tokens": 0, "completion_tokens": 0}
    started = time.time()
    err_msg = None
    it = None
    try:
        r = await asyncio.wait_for(proxy.dispatch(gw_c, None, "chat/completions", b, None, headers_in), cfg["stage_timeout_s"]) if gw_c else None
        if r is None or not hasattr(r, "body_iterator"):
            err_msg = "chairman unavailable" if r is None else str((json.loads(bytes(r.body)).get("error") or {}).get("message") or "chairman failed")[:200]
        else:
            it = r.body_iterator
            async for raw in it:
                text = raw.decode() if isinstance(raw, bytes) else raw
                for line in text.split("\n"):
                    if not line.startswith("data:") or "[DONE]" in line:
                        continue
                    try:
                        obj = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    for ch in obj.get("choices") or []:
                        c = (ch.get("delta") or {}).get("content")
                        if c:
                            final.append(c)
                    if obj.get("usage"):
                        cusage = _usage(obj)
                        continue  # the summed usage is sent once at the end
                    obj["id"], obj["model"] = rid, gm.public_name
                    if first:
                        for ch in obj.get("choices") or []:
                            ch.setdefault("delta", {}).setdefault("role", "assistant")
                        first = False
                    yield "data: " + json.dumps(obj, separators=(",", ":")) + "\n\n"
    except Exception as e:  # noqa: BLE001
        err_msg = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        if it is not None and hasattr(it, "aclose"):
            try:
                await it.aclose()
            except Exception:  # noqa: BLE001
                pass
    fallback = not final
    if fallback:  # chairman failed before saying anything: answer with the best-ranked response instead
        text = st["good"][_top(st)]["text"]
        yield _chunk(rid, gm.public_name, {"role": "assistant", "content": text})
        final.append(text)
    total = _sum(prior, cusage)
    chair = {"label": label, "ok": not fallback, "ms": int((time.time() - started) * 1000), "error": err_msg, "fallback": fallback, "usage": cusage}
    d = await finish(st, chair, "".join(final), total, 200, None, True)
    yield _chunk(rid, gm.public_name, {}, "stop", usage={**total, "total_tokens": total["prompt_tokens"] + total["completion_tokens"]})
    if want_details:
        yield "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "model": gm.public_name, "choices": [], "sg_council": d}, default=str) + "\n\n"
    yield "data: [DONE]\n\n"
