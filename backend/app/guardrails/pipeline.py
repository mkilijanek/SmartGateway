"""Guardrail pipeline: input checks (PII, injection, content filters) and output checks (filters, JSON validation).

Configuration lives in GatewayModel.guardrails (None = everything off). Only counts and rule names are ever reported
about findings - never the matched content.
"""
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import filters, injection, output, pii

PII_MODES = ("off", "log", "redact", "block")
INJ_MODES = ("off", "log", "block")
SENS = ("low", "medium", "high")
ORDER = ["pass", "validated", "flagged", "redacted", "retried", "failed", "blocked"]  # worst last


def worst(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a or not b:
        return a or b
    return a if ORDER.index(a) >= ORDER.index(b) else b


def normalize(raw: Optional[dict]) -> Optional[dict]:
    """Validated config with defaults, or None when nothing is switched on. Raises ValueError on bad input."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError("guardrails must be an object")
    p = raw.get("pii") or {}
    i = raw.get("injection") or {}
    f = raw.get("filters") or {}
    o = raw.get("output") or {}
    cfg = {
        "pii": {"mode": p.get("mode", "off"), "types": list(p.get("types") or pii.KINDS)},
        "injection": {"mode": i.get("mode", "off"), "sensitivity": i.get("sensitivity", "medium")},
        "filters": {"mode": f.get("mode", "off"), "filter_ids": list(f.get("filter_ids") or []), "group_ids": list(f.get("group_ids") or [])},
        "output": {"mode": o.get("mode", "off"), "json_schema": o.get("json_schema") or None,
                   "use_request_schema": bool(o.get("use_request_schema", True)),
                   "max_retries": o.get("max_retries", 2), "feedback": bool(o.get("feedback", True)),
                   "on_fail": o.get("on_fail", "error")},
    }
    if cfg["pii"]["mode"] not in PII_MODES:
        raise ValueError(f"pii mode must be one of {PII_MODES}")
    bad = [t for t in cfg["pii"]["types"] if t not in pii.KINDS]
    if bad:
        raise ValueError(f"unknown PII types {bad}; known: {pii.KINDS}")
    if cfg["injection"]["mode"] not in INJ_MODES:
        raise ValueError(f"injection mode must be one of {INJ_MODES}")
    if cfg["injection"]["sensitivity"] not in SENS:
        raise ValueError(f"injection sensitivity must be one of {SENS}")
    if cfg["filters"]["mode"] not in ("off", "on"):
        raise ValueError("filters mode must be off or on")
    for k in ("filter_ids", "group_ids"):
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in cfg["filters"][k]):
            raise ValueError(f"filters {k} must be a list of integers")
    if cfg["output"]["mode"] not in ("off", "on"):
        raise ValueError("output mode must be off or on")
    if cfg["output"]["on_fail"] not in ("error", "passthrough"):
        raise ValueError("output on_fail must be error or passthrough")
    mr = cfg["output"]["max_retries"]
    if not isinstance(mr, int) or isinstance(mr, bool) or not 0 <= mr <= 5:
        raise ValueError("output max_retries must be an integer from 0 to 5")
    sch = cfg["output"]["json_schema"]
    if sch is not None:
        if not isinstance(sch, dict):
            raise ValueError("json_schema must be an object")
        try:
            output.check_schema(sch)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"invalid JSON schema: {str(e).splitlines()[0][:200]}")
    if all(cfg[k]["mode"] == "off" for k in ("pii", "injection", "filters", "output")):
        return None
    return cfg


def safe_normalize(raw: Optional[dict]) -> Optional[dict]:
    """For the request path: a broken stored config must not take the model down."""
    try:
        return normalize(raw)
    except ValueError:
        return None


def _stage_rules(rules, *stages) -> list:
    return [r for r in (rules or []) if r.get("stage", "both") in stages]


def buffered(cfg: Optional[dict], rules=()) -> bool:
    """Output checks need the complete answer, so the upstream call is made without streaming. `rules` = resolved library filters."""
    if not cfg:
        return False
    if cfg["output"]["mode"] == "on":
        return True
    return cfg["filters"]["mode"] == "on" and bool(_stage_rules(rules, "output", "both"))


def fingerprint(cfg: Optional[dict], rules=()) -> Optional[str]:
    """Part of the response-cache key: a changed output policy must not be served from entries made under the old one."""
    if not cfg or not buffered(cfg, rules):
        return None
    f = [{k: r.get(k) for k in ("pattern", "stage", "action", "validator")} for r in _stage_rules(rules, "output", "both")] if cfg["filters"]["mode"] == "on" else []
    sub = {"o": cfg["output"], "f": sorted(f, key=lambda r: json.dumps(r, sort_keys=True))}
    return hashlib.sha1(json.dumps(sub, sort_keys=True).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ input
@dataclass
class InputResult:
    body: dict
    action: Optional[str] = None          # None (nothing configured) | pass | flagged | redacted | blocked
    findings: dict = field(default_factory=dict)
    reason: str = ""


def _transform(route: str, body: dict, fn: Callable) -> dict:
    """Copy of the body with fn(role, text) applied to every text the model will read."""
    new = dict(body)
    if route == "chat/completions":
        msgs = []
        for m in body.get("messages") or []:
            if not isinstance(m, dict):
                msgs.append(m)
                continue
            m = dict(m)
            role = m.get("role", "user")
            c = m.get("content")
            if isinstance(c, str):
                m["content"] = fn(role, c)
            elif isinstance(c, list):
                parts = []
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part = {**part, "text": fn(role, part["text"])}
                    parts.append(part)
                m["content"] = parts
            if isinstance(m.get("tool_calls"), list):
                calls = []
                for tc in m["tool_calls"]:
                    f = tc.get("function") if isinstance(tc, dict) else None
                    if isinstance(f, dict) and isinstance(f.get("arguments"), str):
                        tc = {**tc, "function": {**f, "arguments": fn("tool_args", f["arguments"])}}
                    calls.append(tc)
                m["tool_calls"] = calls
            msgs.append(m)
        new["messages"] = msgs
    elif route in ("completions", "embeddings"):
        key = "prompt" if route == "completions" else "input"
        v = body.get(key)
        if isinstance(v, str):
            new[key] = fn("user", v)
        elif isinstance(v, list):
            new[key] = [fn("user", x) if isinstance(x, str) else x for x in v]
    return new


def apply_input(route: str, body: dict, cfg: Optional[dict], rules=()) -> InputResult:
    if not cfg or route not in ("chat/completions", "completions", "embeddings"):
        return InputResult(body)
    pcfg, icfg, fcfg = cfg["pii"], cfg["injection"], cfg["filters"]
    rules = _stage_rules(rules, "input", "both") if fcfg["mode"] == "on" else []
    if pcfg["mode"] == "off" and icfg["mode"] == "off" and not rules:
        return InputResult(body)
    red = pii.Redactor()
    counts: dict = {}
    state = {"blocked": [], "changed": False}

    def bump(k: str, n: int = 1):
        counts[k] = counts.get(k, 0) + n

    def fn(role: str, text: str) -> str:
        if not text:
            return text
        orig = text
        if icfg["mode"] != "off" and role in ("user", "tool"):
            hits = injection.scan(text)
            if hits and injection.flagged(hits, icfg["sensitivity"]):
                for name, _ in hits:
                    bump("injection:" + name)
                if icfg["mode"] == "block":
                    state["blocked"].append("prompt injection")
        for rule in rules:
            try:
                found_ms = filters.find(rule, text)
            except filters.FilterTimeout:   # cannot tell whether the text is clean or redact it: fail closed
                name = rule.get("name") or rule["pattern"][:30]
                bump("filter_timeout:" + name)
                state["blocked"].append(f"content filter '{name}' (pattern too slow on this text)")
                continue
            n = len(found_ms)
            if n:
                name = rule.get("name") or rule["pattern"][:30]
                bump("filter:" + name, n)
                if rule.get("action", "block") == "block":
                    state["blocked"].append(f"content filter '{name}'")
                else:
                    text = filters.redact(rule, text, matches=found_ms)
        if pcfg["mode"] != "off":
            found = pii.scan(text, pcfg["types"])
            for f in found:
                bump("pii:" + f.kind)
            if found:
                if pcfg["mode"] == "block":
                    state["blocked"].append("personal data (" + ", ".join(sorted({f.kind for f in found})) + ")")
                elif pcfg["mode"] == "redact":
                    text = red.apply(text, found)
        if text != orig:
            state["changed"] = True
        return text

    new_body = _transform(route, body, fn)
    if state["blocked"]:
        why = "; ".join(dict.fromkeys(state["blocked"]))
        return InputResult(body, "blocked", counts, f"Request blocked by guardrails: {why}")
    if state["changed"]:
        return InputResult(new_body, "redacted", counts)
    return InputResult(body, "flagged" if counts else "pass", counts)


# ------------------------------------------------------------------ output
@dataclass
class OutputResult:
    data: dict
    ok: bool = True
    blocked: bool = False
    action: str = "validated"
    findings: dict = field(default_factory=dict)
    error: str = ""


def request_schema(body: dict) -> tuple:
    """(wants_json, schema) from an OpenAI-style response_format in the request."""
    rf = body.get("response_format")
    if not isinstance(rf, dict):
        return False, None
    if rf.get("type") == "json_schema":
        sch = (rf.get("json_schema") or {}).get("schema")
        return True, sch if isinstance(sch, dict) else None
    return rf.get("type") == "json_object", None


def check_output(data: dict, cfg: dict, body: dict, rules=()) -> OutputResult:
    """Output filters first (block / redact), then JSON validation. `data` is a chat.completion; never mutated."""
    fcfg, ocfg = cfg["filters"], cfg["output"]
    rules = _stage_rules(rules, "output", "both") if fcfg["mode"] == "on" else []
    res = OutputResult(data)
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
        return res
    data = copy.deepcopy(data)
    res.data = data
    schema, need_json = None, False
    if ocfg["mode"] == "on":
        need_json = bool(ocfg["json_schema"])
        schema = ocfg["json_schema"]
        if not need_json and ocfg["use_request_schema"]:
            need_json, schema = request_schema(body)
    for ch in data["choices"]:
        msg = (ch or {}).get("message") or {}
        text = msg.get("content")
        if not isinstance(text, str):
            if need_json and not msg.get("tool_calls"):
                res.ok, res.error, res.action = False, "the reply is empty", "failed"
            continue
        for rule in rules:
            try:
                found_ms = filters.find(rule, text)
            except filters.FilterTimeout:
                name = rule.get("name") or rule["pattern"][:30]
                res.findings["filter_timeout:" + name] = res.findings.get("filter_timeout:" + name, 0) + 1
                res.blocked, res.ok, res.action = True, False, "blocked"
                res.error = f"content filter '{name}' (pattern too slow on this text)"
                continue
            n = len(found_ms)
            if not n:
                continue
            name = rule.get("name") or rule["pattern"][:30]
            res.findings["filter:" + name] = res.findings.get("filter:" + name, 0) + n
            if rule.get("action", "block") == "block":
                res.blocked, res.ok, res.action = True, False, "blocked"
                res.error = f"content filter '{name}'"
            else:
                text = filters.redact(rule, text, matches=found_ms)
                msg["content"] = text
                if res.action == "validated":
                    res.action = "redacted"
        if need_json and not res.blocked:
            ok, why = output.validate_json(text, schema)
            if not ok:
                res.ok, res.error, res.action = False, why, "failed"
    return res
