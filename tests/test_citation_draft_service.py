"""Unit tests for the metered citation draft service.

Pure prompt construction + the paid-gate/generate/meter/persist flow with the
LLM, metering, and DB calls all mocked. No key, no balance, no DB.
(`asyncio_mode = auto` runs the async tests.)
"""
from __future__ import annotations

import pytest

import services.citation_draft_service as cds


# --------------------------------------------------------------------------
# Pure prompt construction
# --------------------------------------------------------------------------

def test_build_messages_reddit_reply_is_authentic_and_grounded() -> None:
    system, user = cds.build_messages("reddit_reply", {
        "brand": "BB Lab",
        "question": "is low molecular collagen worth it before bed?",
        "subreddit": "SkincareAddiction",
        "product": {"title": "Good Night Collagen", "molecular_weight": "low (500 Da)",
                    "format": "stick", "claims": ["sleep support"]},
    })
    # Authenticity-as-efficacy: disclosure + grounding + anti-ad framing present.
    assert "disclose" in system.lower()
    assert "never invent" in system.lower()
    assert "removed by moderators" in system.lower()
    assert "<brand>" not in system           # brand placeholder substituted
    assert "BB Lab" in system
    # User payload carries the question, community, and product facts.
    assert "is low molecular collagen worth it" in user
    assert "r/SkincareAddiction" in user
    assert "Good Night Collagen" in user
    assert "molecular_weight" in user


def test_build_messages_evidence_block_and_unsupported() -> None:
    _system, user = cds.build_messages("owned_evidence_page", {
        "brand": "Ownist", "question": "does glutathione brighten skin?",
        "product": {"title": "Triple Shine"},
        "evidence": ["PMC: oral glutathione RCT, 12 weeks", "PubMed: tyrosinase inhibition review"],
    })
    assert "CLINICAL/SCIENTIFIC EVIDENCE" in user
    assert "oral glutathione RCT" in user
    with pytest.raises(ValueError):
        cds.build_messages("nonsense_action", {})


def test_facts_block_skips_empty_and_joins_lists() -> None:
    block = cds._facts_block({"a": "x", "b": None, "c": [], "d": ["p", "q"]})
    assert "- a: x" in block and "- d: p, q" in block
    assert "b:" not in block and "c:" not in block
    assert cds._facts_block(None).startswith("(no structured product facts")


# --------------------------------------------------------------------------
# generate_draft flow
# --------------------------------------------------------------------------

def _patch(monkeypatch, *, paid, gen, meter_called, record_called,
           meter_result=None, gen_raises=False, meter_raises=False, set_credits=None):
    async def fake_paid(merchant_id, **kw):
        return paid

    async def fake_generate(*, system, user, provider, **kw):
        gen["called"] = True
        if gen_raises:
            raise RuntimeError("deepseek 500")
        return gen["value"]

    async def fake_meter(merchant_id, op, *, provider, units, idempotency_key, **kw):
        meter_called["called"] = True
        meter_called["idem"] = idempotency_key
        meter_called["op"] = op
        if meter_raises:
            raise RuntimeError("debit failed")
        return meter_result or {"billing_mode": "metered", "credits": 1}

    async def fake_record(**kw):
        record_called["called"] = True
        record_called["kwargs"] = kw
        return "cact_test123"

    async def fake_set_credits(*, action_id, credits):
        if set_credits is not None:
            set_credits["called"] = True
            set_credits["action_id"] = action_id
            set_credits["credits"] = credits

    monkeypatch.setattr(cds.ccs, "merchant_is_paid_tier", fake_paid)
    monkeypatch.setattr(cds, "_generate", fake_generate)
    monkeypatch.setattr(cds.ccs, "meter_agent_workflow", fake_meter)
    monkeypatch.setattr(cds.cos, "record_merchant_action", fake_record)
    monkeypatch.setattr(cds.cos, "set_action_draft_credits", fake_set_credits)


async def test_free_tier_is_gated_before_generation(monkeypatch) -> None:
    gen = {"called": False, "value": ("draft", 10, 20)}
    meter_called = {"called": False}
    record_called = {"called": False}
    _patch(monkeypatch, paid=False, gen=gen, meter_called=meter_called, record_called=record_called)

    out = await cds.generate_draft(
        merchant_id="m1", target_id="t1", action_type="reddit_reply",
        context={"brand": "BB Lab", "question": "q"})

    assert out["billing_mode"] == "preview_only"
    assert out["reason"] == "not_paid_tier"
    assert out["draft_content"] is None and out["action_id"] is None
    assert gen["called"] is False          # no COGS spent
    assert meter_called["called"] is False  # nothing charged


async def test_paid_tier_generates_meters_and_persists(monkeypatch) -> None:
    gen = {"called": False, "value": ("Full disclosure, I work with BB Lab. ...", 50, 120)}
    meter_called = {"called": False}
    record_called = {"called": False}
    set_credits = {"called": False}
    _patch(monkeypatch, paid=True, gen=gen, meter_called=meter_called,
           record_called=record_called, set_credits=set_credits)

    out = await cds.generate_draft(
        merchant_id="m1", target_id="t1", action_type="reddit_reply", sku_key="sku1",
        context={"brand": "BB Lab", "question": "is it worth it?"})

    assert out["billing_mode"] == "metered" and out["credits"] == 1
    assert out["draft_content"].startswith("Full disclosure")
    assert out["action_id"] == "cact_test123"
    # Metered under the right operation_type + idempotent key.
    assert meter_called["op"] == cds.DRAFT_OPERATION_TYPE
    assert meter_called["idem"] == "citation_draft:t1:reddit_reply"
    # Persisted FIRST with draft_credits=None (receipt), then credits set post-meter.
    assert record_called["called"] is True
    assert record_called["kwargs"]["draft_credits"] is None
    assert record_called["kwargs"]["idempotency_key"] == "draft:t1:reddit_reply"
    assert set_credits["called"] is True and set_credits["credits"] == 1


async def test_empty_generation_is_not_charged(monkeypatch) -> None:
    gen = {"called": False, "value": ("   ", 10, 0)}  # 200 with whitespace body
    meter_called = {"called": False}
    record_called = {"called": False}
    _patch(monkeypatch, paid=True, gen=gen, meter_called=meter_called, record_called=record_called)

    out = await cds.generate_draft(
        merchant_id="m1", target_id="t1", action_type="reddit_reply",
        context={"brand": "BB Lab", "question": "q"})

    assert out["billing_mode"] == "preview_only" and out["reason"] == "empty_generation"
    assert out["draft_content"] is None and out["credits"] == 0
    assert meter_called["called"] is False   # never charge for empty output
    assert record_called["called"] is False


async def test_metering_failure_keeps_draft_uncharged(monkeypatch) -> None:
    gen = {"called": False, "value": ("a useful grounded reply", 40, 90)}
    meter_called = {"called": False}
    record_called = {"called": False}
    set_credits = {"called": False}
    _patch(monkeypatch, paid=True, gen=gen, meter_called=meter_called,
           record_called=record_called, set_credits=set_credits, meter_raises=True)

    out = await cds.generate_draft(
        merchant_id="m1", target_id="t1", action_type="owned_faq",
        context={"brand": "BB Lab", "question": "q"})

    # Draft persisted (merchant keeps it), but NOT charged.
    assert record_called["called"] is True          # persisted before meter
    assert out["action_id"] == "cact_test123"
    assert out["billing_mode"] == "preview_only" and out["credits"] == 0
    assert out["reason"] == "metering_failed"
    assert set_credits["called"] is False           # no credits to record


async def test_custom_idempotency_key_flows_to_both_namespaces(monkeypatch) -> None:
    gen = {"called": False, "value": ("reply", 5, 5)}
    meter_called = {"called": False}
    record_called = {"called": False}
    _patch(monkeypatch, paid=True, gen=gen, meter_called=meter_called, record_called=record_called)

    await cds.generate_draft(
        merchant_id="m1", target_id="t1", action_type="reddit_reply",
        context={"brand": "BB Lab", "question": "q"}, idempotency_key="regen-2")

    assert meter_called["idem"] == "citation_draft:regen-2"
    assert record_called["kwargs"]["idempotency_key"] == "draft:regen-2"


async def test_generation_failure_does_not_meter(monkeypatch) -> None:
    gen = {"called": False, "value": None}
    meter_called = {"called": False}
    record_called = {"called": False}
    _patch(monkeypatch, paid=True, gen=gen, meter_called=meter_called,
           record_called=record_called, gen_raises=True)

    out = await cds.generate_draft(
        merchant_id="m1", target_id="t1", action_type="reddit_reply",
        context={"brand": "BB Lab", "question": "q"})

    assert out["billing_mode"] == "preview_only"
    assert out["reason"] == "generation_failed"
    assert meter_called["called"] is False   # never charge for a failed draft
    assert record_called["called"] is False


async def test_no_api_key_returns_preview_not_charged(monkeypatch) -> None:
    gen = {"called": False, "value": None}   # _generate returns None on no-key
    meter_called = {"called": False}
    record_called = {"called": False}
    _patch(monkeypatch, paid=True, gen=gen, meter_called=meter_called, record_called=record_called)

    out = await cds.generate_draft(
        merchant_id="m1", target_id="t1", action_type="owned_faq",
        context={"brand": "BB Lab", "question": "q"})

    assert out["billing_mode"] == "preview_only"
    assert out["reason"] == "no_api_key"
    assert meter_called["called"] is False
