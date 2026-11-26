"""User-defined content filter rules (regex)."""
import os
import time
from functools import lru_cache

import regex as re   # same syntax as the stdlib module, plus a match timeout: a user-written pattern with catastrophic backtracking can't hang a request

from . import checks


MATCH_TIMEOUT = float(os.environ.get("SG_FILTER_REGEX_TIMEOUT_MS", "200")) / 1000   # per rule and text
# adversarial inputs for backtracking patterns; a rule that is slow on these is rejected when it is saved
PROBES = ("a" * 4000 + "!", "ab" * 3000 + "!", " " * 4000 + "x", "1" * 4000 + "x", "a@" * 2000 + "!")
PROBE_TIMEOUT = 0.05


class FilterTimeout(Exception):
    """The pattern took too long on this text; the caller must not treat the text as clean."""


@lru_cache(maxsize=256)
def compile_rule(pattern: str):
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


def validate_rule(rule: dict) -> None:
    pat = rule.get("pattern")
    if not isinstance(pat, str) or not pat.strip():
        raise ValueError("filter rule needs a pattern")
    if len(pat) > 8000:
        raise ValueError("filter pattern is too long (max 8000 characters)")
    try:
        rx = compile_rule(pat)
    except re.error as e:
        raise ValueError(f"invalid filter regex '{pat}': {e}")
    if rx.match(""):
        raise ValueError(f"filter regex '{pat}' matches empty text")
    for probe in PROBES:
        try:
            list(rx.finditer(probe, timeout=PROBE_TIMEOUT))
        except TimeoutError:
            raise ValueError(f"filter regex '{pat}' is too slow (catastrophic backtracking); simplify it, e.g. avoid nested repetition like (a+)+")
    if rule.get("validator") and rule["validator"] not in checks.VALIDATORS:
        raise ValueError(f"unknown validation '{rule['validator']}'; known: {sorted(checks.VALIDATORS)}")
    if rule.get("stage", "both") not in ("input", "output", "both"):
        raise ValueError("filter stage must be input, output or both")
    if rule.get("action", "block") not in ("block", "redact"):
        raise ValueError("filter action must be block or redact")


def find(rule: dict, text: str) -> list:
    """Matches of the rule in text. With a validator, only structurally valid numbers count (e.g. a PESEL with a correct checksum).
    A pattern may name a group (?P<n>...) for the number itself, so that a label in front of it is not validated."""
    check = checks.get(rule.get("validator"))
    out, deadline = [], time.monotonic() + MATCH_TIMEOUT
    try:
        for m in compile_rule(rule["pattern"]).finditer(text, timeout=MATCH_TIMEOUT):
            if time.monotonic() > deadline:
                raise TimeoutError
            if check and not check(m.groupdict().get("n") or m.group(0)):
                continue
            out.append(m)
    except TimeoutError:
        raise FilterTimeout(rule.get("name") or rule["pattern"][:30]) from None
    return out


def redact(rule: dict, text: str, repl: str = "[FILTERED]", matches: list | None = None) -> str:
    out, pos = [], 0
    for m in (find(rule, text) if matches is None else matches):
        out.append(text[pos:m.start()])
        out.append(repl)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)
