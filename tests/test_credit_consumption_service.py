"""Tests for the canonical credit-consumption service."""

from __future__ import annotations

from decimal import Decimal

import pytest

import services.credit_consumption_service as ccs


def test_category_mapping():
    assert ccs.category_for("audit") == "audit"
    assert ccs.category_for("prompt") == "prompt"
    assert ccs.category_for("agent_sku_match") == "execution"
    assert ccs.category_for("agent_demand_test") == "execution"
    with pytest.raises(ValueError):
        ccs.category_for("not_a_real_operation")


def test_estimate_probe_credits_matches_audit_pricing():
    """estimate_probe_credits is the same per-probe model the audit cost path uses."""
    from routes.audit_runs_routes import _audit_metering

    # The probe set the audit path builds for 1 SKU / 1 prompt, gemini+chatgpt,
    # deepseek verify @ 0.25 sample == the live-verified 12-credit scenario.
    audit_credits, _ = _audit_metering(
        sku_count=1,
        prompts_per_sku=1,
        providers=["gemini", "chatgpt"],
        verify_providers=["deepseek"],
        verify_sample={"positive_fraction": 0.25, "max_per_sku": None},
    )
    assert audit_credits == 12


def test_estimate_zero_and_negative_probes_are_ignored():
    credits, cogs = ccs.estimate_probe_credits(
        [("gemini", 0, True), ("chatgpt", -3, True)]
    )
    assert credits == 0
    assert cogs == Decimal("0")


@pytest.mark.asyncio
async def test_consume_with_explicit_credits_passes_key_through(monkeypatch):
    captured = {}

    async def fake_debit(merchant_id, category, amount, *, idempotency_key, usd_cogs=0, conn=None):
        captured.update(
            merchant_id=merchant_id, category=category, amount=amount,
            idempotency_key=idempotency_key, usd_cogs=usd_cogs,
        )
        return {"credits": amount, "replay": False}

    monkeypatch.setattr(ccs._mcb, "debit", fake_debit)

    out = await ccs.consume(
        "merch_x", "agent_sku_match", "run-1", credits=7, usd_cogs=Decimal("0.05"),
    )
    assert out["credits"] == 7
    assert out["category"] == "execution"
    # key is passed through unchanged (callers migrating from debit() keep replay).
    assert captured["idempotency_key"] == "run-1"
    assert captured["category"] == "execution"
    assert captured["amount"] == 7


@pytest.mark.asyncio
async def test_consume_with_probes_estimates_cost(monkeypatch):
    async def fake_debit(merchant_id, category, amount, *, idempotency_key, usd_cogs=0, conn=None):
        return {"credits": amount, "replay": False}

    monkeypatch.setattr(ccs._mcb, "debit", fake_debit)

    expected_credits, _ = ccs.estimate_probe_credits([("gemini", 2, True)])
    out = await ccs.consume("merch_x", "agent_demand_test", "run-2", probes=[("gemini", 2, True)])
    assert out["credits"] == expected_credits
    assert out["category"] == "execution"


@pytest.mark.asyncio
async def test_consume_zero_credits_is_noop(monkeypatch):
    async def fail_debit(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("debit should not run for zero credits")

    monkeypatch.setattr(ccs._mcb, "debit", fail_debit)
    out = await ccs.consume("merch_x", "agent_sku_match", "run-3", credits=0)
    assert out == {"credits": 0, "category": "execution", "skipped": True}


@pytest.mark.asyncio
async def test_refund_credits_back(monkeypatch):
    captured = {}

    async def fake_credit(merchant_id, category, amount, *, source_event_id, usd_cogs=0, conn=None):
        captured.update(category=category, amount=amount, source_event_id=source_event_id)
        return {"credits": amount, "replay": False}

    monkeypatch.setattr(ccs._mcb, "credit", fake_credit)

    out = await ccs.refund("merch_x", "agent_sku_match", 7, "run-1")
    assert out["credits"] == 7
    assert captured["category"] == "execution"
    assert captured["source_event_id"] == "run-1"
