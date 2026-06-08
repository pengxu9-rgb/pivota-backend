"""Layer 2 LLM review gate: pass keeps, flag/error withholds (deterministic-by-
fallback), default-off/no-key skips. The LLM is always mocked (no key in CI)."""
import asyncio
import json

import pytest

import services.audit_review_gate as gate
from services.audit_review_gate import (
    review_merchant_surface,
    apply_audit_review_gate,
    ReviewVerdict,
    VERDICT_PASS,
    VERDICT_FLAG,
    VERDICT_ERROR,
    VERDICT_SKIPPED,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _enable(monkeypatch, *, key="k"):
    monkeypatch.setenv("AUDIT_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setattr(gate, "configured_key_for_provider", lambda p: key)


def _mock_synth(monkeypatch, text):
    async def _fake(**kwargs):
        return {"text": text, "usage": {}, "provider": "deepseek", "model": "m"}
    monkeypatch.setattr(gate, "synthesize", _fake)


def _payload():
    return {
        "audited_url": "https://bblab.shop/x",
        "brand_report": {"merchant_name": "BB LAB", "merchant_domain": "bblab.shop",
                         "aggregate": {"buyer_path_verdict": {"top_controllers": ["reddit.com"]}}},
        "sku_intelligence": {
            "hero_sku": {"title": "Good Night Collagen"},
            "headline": "You lost `best collagen`, but nobody owns `halal collagen sticks before bed`.",
            "top_open_lanes": [], "substitution_alert": {"present": False}, "intent_ladder": {},
            "next_best_action": {"headline": "do this",
                                 "strategic_brief": {"core_decision": "x", "grounding_notes": {}}},
        },
    }


# --- single-surface verdicts ---------------------------------------------
def test_disabled_is_skipped(monkeypatch):
    monkeypatch.delenv("AUDIT_REVIEW_GATE_ENABLED", raising=False)
    v = _run(review_merchant_surface(surface="money_shot", rendered_output="x",
                                     identity={}, claims="x", evidence={}))
    assert v.verdict == VERDICT_SKIPPED
    assert v.should_withhold() is False


def test_enabled_no_key_is_skipped(monkeypatch):
    monkeypatch.setenv("AUDIT_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setattr(gate, "configured_key_for_provider", lambda p: None)
    v = _run(review_merchant_surface(surface="money_shot", rendered_output="x",
                                     identity={}, claims="x", evidence={}))
    assert v.verdict == VERDICT_SKIPPED


def test_pass_keeps_surface(monkeypatch):
    _enable(monkeypatch)
    _mock_synth(monkeypatch, json.dumps({"verdict": "pass", "severity": "none", "findings": []}))
    v = _run(review_merchant_surface(surface="money_shot", rendered_output="x",
                                     identity={}, claims="x", evidence={}))
    assert v.verdict == VERDICT_PASS
    assert v.should_withhold() is False


def test_flag_withholds(monkeypatch):
    _enable(monkeypatch)
    _mock_synth(monkeypatch, json.dumps({"verdict": "flag", "severity": "high",
                                         "findings": [{"claim": "c", "issue": "ungrounded"}]}))
    v = _run(review_merchant_surface(surface="strategic_brief", rendered_output="x",
                                     identity={}, claims="x", evidence={}))
    assert v.verdict == VERDICT_FLAG
    assert v.should_withhold() is True
    assert v.findings


def test_llm_error_withholds(monkeypatch):
    _enable(monkeypatch)
    from services.llm_synthesis import LLMSynthesisError

    async def _boom(**kwargs):
        raise LLMSynthesisError("upstream down")
    monkeypatch.setattr(gate, "synthesize", _boom)
    v = _run(review_merchant_surface(surface="money_shot", rendered_output="x",
                                     identity={}, claims="x", evidence={}))
    assert v.verdict == VERDICT_ERROR
    assert v.should_withhold() is True


def test_unparseable_withholds(monkeypatch):
    _enable(monkeypatch)
    _mock_synth(monkeypatch, "not json at all")
    v = _run(review_merchant_surface(surface="money_shot", rendered_output="x",
                                     identity={}, claims="x", evidence={}))
    assert v.verdict == VERDICT_ERROR


def test_gate_never_returns_prose(monkeypatch):
    """A gate, not an editor: the verdict object carries no rewritten text."""
    _enable(monkeypatch)
    _mock_synth(monkeypatch, json.dumps({"verdict": "flag", "findings": [],
                                         "rewrite": "here is better copy"}))
    v = _run(review_merchant_surface(surface="money_shot", rendered_output="orig",
                                     identity={}, claims="orig", evidence={}))
    assert not hasattr(v, "rewrite")
    assert "rewrite" not in (v.note or "")


# --- payload-level apply --------------------------------------------------
def test_apply_disabled_is_noop(monkeypatch):
    monkeypatch.delenv("AUDIT_REVIEW_GATE_ENABLED", raising=False)
    payload = _payload()
    before = json.dumps(payload, sort_keys=True)
    out = _run(apply_audit_review_gate(payload, run_id="r"))
    assert out == {}
    assert json.dumps(payload, sort_keys=True) == before


def test_apply_flag_withholds_both_surfaces(monkeypatch):
    _enable(monkeypatch)
    _mock_synth(monkeypatch, json.dumps({"verdict": "flag", "severity": "high", "findings": []}))
    payload = _payload()
    out = _run(apply_audit_review_gate(payload, run_id="r"))
    assert out == {"money_shot": VERDICT_FLAG, "strategic_brief": VERDICT_FLAG}
    assert payload["sku_intelligence"]["headline"] is None
    assert payload["sku_intelligence"]["next_best_action"]["strategic_brief"] is None
    # the deterministic next_best_action itself is untouched
    assert payload["sku_intelligence"]["next_best_action"]["headline"] == "do this"


def test_apply_pass_keeps_surfaces(monkeypatch):
    _enable(monkeypatch)
    _mock_synth(monkeypatch, json.dumps({"verdict": "pass", "findings": []}))
    payload = _payload()
    out = _run(apply_audit_review_gate(payload, run_id="r"))
    assert out == {"money_shot": VERDICT_PASS, "strategic_brief": VERDICT_PASS}
    assert payload["sku_intelligence"]["headline"] is not None
    assert payload["sku_intelligence"]["next_best_action"]["strategic_brief"] is not None


def test_apply_never_raises_on_garbage(monkeypatch):
    _enable(monkeypatch)
    _mock_synth(monkeypatch, json.dumps({"verdict": "pass"}))
    for bad in (None, {}, {"sku_intelligence": 5}, {"sku_intelligence": {}}):
        _run(apply_audit_review_gate(bad, run_id="r"))
