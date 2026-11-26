import time

import pytest

from app.guardrails import filters, pipeline, presets

EVIL = "(a+)+$"            # classic catastrophic backtracking
TEXT = "a" * 4000 + "!"


def test_slow_pattern_is_rejected_when_saved():
    with pytest.raises(ValueError, match="too slow"):
        filters.validate_rule({"pattern": EVIL, "stage": "input", "action": "block"})


def test_every_builtin_preset_still_validates():
    assert presets.FILTERS
    for p in presets.FILTERS:
        filters.validate_rule({"pattern": p["pattern"], "stage": p.get("stage", "input"), "action": p.get("action", "redact"), "validator": p.get("validator")})


def test_find_gives_up_quickly_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(filters, "MATCH_TIMEOUT", 0.02)
    t0 = time.time()
    with pytest.raises(filters.FilterTimeout):
        filters.find({"name": "evil", "pattern": EVIL}, TEXT)
    assert time.time() - t0 < 2


def test_normal_patterns_and_validators_behave_as_before():
    rule = {"pattern": r"(?P<n>\d{11})", "validator": "pesel"}
    assert filters.find({"pattern": "swordfish"}, "the Swordfish") and not filters.find({"pattern": "swordfish"}, "nothing")
    assert filters.redact({"pattern": "secret \\d+"}, "x secret 42 y") == "x [FILTERED] y"
    assert filters.find(rule, "no digits here") == []


def test_input_with_a_timing_out_rule_is_blocked_not_passed(monkeypatch):
    monkeypatch.setattr(filters, "MATCH_TIMEOUT", 0.02)
    rules = [{"name": "evil", "pattern": EVIL, "stage": "input", "action": "redact"}]   # even a redact rule cannot be applied -> block
    cfg = pipeline.normalize({"filters": {"mode": "on", "filter_ids": [1]}})
    r = pipeline.apply_input("chat/completions", {"messages": [{"role": "user", "content": TEXT}]}, cfg, rules)
    assert r.action == "blocked" and "evil" in r.reason and TEXT not in r.reason


def test_output_with_a_timing_out_rule_is_blocked(monkeypatch):
    monkeypatch.setattr(filters, "MATCH_TIMEOUT", 0.02)
    rules = [{"name": "evil", "pattern": EVIL, "stage": "output", "action": "block"}]
    cfg = pipeline.normalize({"filters": {"mode": "on", "filter_ids": [1]}})
    res = pipeline.check_output({"choices": [{"message": {"content": TEXT}}]}, cfg, {}, rules)
    assert res.blocked and not res.ok
