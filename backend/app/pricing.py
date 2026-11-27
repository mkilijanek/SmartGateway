"""What a model costs. Prices are USD per 1M tokens: the admin's manual price wins, else the hosted catalog's own `cost` block (OpenCode),
which can also carry time-of-day multipliers (peak-hour surcharges)."""
from datetime import datetime, timezone
from typing import Optional

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def effective(m) -> Optional[dict]:
    """{"in", "out", "cache_read", "rules", "source"} or None when the model has no price (a local model, or a subscription without one)."""
    if getattr(m, "price_manual", False) and (m.price_in is not None or m.price_out is not None):
        pin = float(m.price_in or 0)
        return {"in": pin, "out": float(m.price_out or 0), "cache_read": float(m.price_cache_read if m.price_cache_read is not None else pin), "rules": [], "source": "manual"}
    c = getattr(m, "catalog_cost", None)
    text = (c or {}).get("text") if isinstance(c, dict) else None
    if not isinstance(text, dict):
        return None
    try:
        i, o = text.get("input") or {}, text.get("output") or {}
        pin, pout = float(i.get("price") or 0), float(o.get("price") or 0)
        cr = (i.get("cache") or {}).get("read")
        return {"in": pin, "out": pout, "cache_read": float(cr) if cr is not None else pin, "rules": list(c.get("multipliers") or []), "source": "catalog"}
    except (TypeError, ValueError):
        return None


def _hhmm(s) -> Optional[int]:
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _hours_ok(cond: dict, minute: int) -> Optional[bool]:
    """`utc_hours: {gte, lt}` against the minute of the day; None when the condition is of a kind we do not understand."""
    if not isinstance(cond, dict) or not set(cond) <= {"gte", "gt", "lt", "lte"}:
        return None
    for op, v in cond.items():
        t = _hhmm(v)
        if t is None:
            return None
        if not {"gte": minute >= t, "gt": minute > t, "lt": minute < t, "lte": minute <= t}[op]:
            return False
    return True


def _match(node, when: datetime) -> Optional[bool]:
    """Evaluate a catalog `when` block. True / False, or None if it uses something we cannot evaluate (the rule is then ignored)."""
    if not isinstance(node, dict):
        return None
    results = []
    for k, v in node.items():
        if k == "utc_days":
            days = (v or {}).get("in") if isinstance(v, dict) else None
            if not isinstance(days, list):
                return None
            results.append(DAYS[when.weekday()] in [str(d).lower() for d in days])
        elif k == "utc_hours":
            r = _hours_ok(v, when.hour * 60 + when.minute)
            if r is None:
                return None
            results.append(r)
        elif k in ("all", "any"):
            sub = [_match(x, when) for x in (v if isinstance(v, list) else [])]
            if not sub or any(x is None for x in sub):
                return None
            results.append(all(sub) if k == "all" else any(sub))
        else:
            return None
    return all(results) if results else None


def factor(rules: list, ts: float) -> float:
    """Product of the multipliers whose condition holds at this moment (UTC); rules we cannot evaluate count as 1."""
    when, f = datetime.fromtimestamp(ts, timezone.utc), 1.0
    for r in rules or []:
        try:
            if isinstance(r, dict) and _match(r.get("when"), when):
                f *= float(r.get("factor") or 1)
        except (TypeError, ValueError):
            continue
    return f


def cost(price: dict, prompt: int, completion: int, cached: int, ts: float) -> float:
    """USD for these tokens at this moment. Cached tokens are part of `prompt`, billed at the cache-read price."""
    cached = max(0, min(cached, prompt))
    usd = ((prompt - cached) * price["in"] + cached * price["cache_read"] + completion * price["out"]) / 1_000_000
    return usd * factor(price.get("rules"), ts)
