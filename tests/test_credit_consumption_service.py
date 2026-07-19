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
    # deepseek verify @ 0.25 sample. At flat_multiple=1.2, priced on PER-PROVIDER
    # measured tokens (grounded ChatGPT ingests ~15k input tokens/probe via
    # web_search_preview, not the flat 2000), this is 17 credits — up from 10 when
    # every provider was mis-priced at 2000 input tokens and the ChatGPT lane
    # under-recovered COGS. See the audit-billing-cogs fix (#1506).
    audit_credits, _ = _audit_metering(
        sku_count=1,
        prompts_per_sku=1,
        providers=["gemini", "chatgpt"],
        verify_providers=["deepseek"],
        verify_sample={"positive_fraction": 0.25, "max_per_sku": None},
    )
    assert audit_credits == 17


def test_per_provider_representative_tokens_price_grounded_lanes_realistically():
    """#1506: grounded ChatGPT/Claude are priced on their real measured token
    usage (ChatGPT ~15k input via web_search_preview), not the flat 2000-token
    representative_probe that under-recovered COGS. Gemini stays grounding-
    dominated; explicit tokens still override (back-compat)."""
    from services.provider_credit_rates import credits_for_probe, provider_probe_cost_usd

    # ChatGPT grounded: 15000 in x $5/1M + 260 out x $30/1M + $0.015 grounding.
    assert round(float(provider_probe_cost_usd("chatgpt", grounded=True)), 4) == 0.0978
    assert credits_for_probe("chatgpt", grounded=True) == 11.7   # was 5.4 at flat 2000
    assert credits_for_probe("claude", grounded=True) == 7.1
    # Gemini unchanged — the $0.035 grounding fee dominates, token delta is noise.
    assert credits_for_probe("gemini", grounded=True) == 4.4
    # Explicit tokens override the per-provider default (the OLD flat-2000 price).
    old_flat = provider_probe_cost_usd("chatgpt", grounded=True, input_tokens=2000, output_tokens=500)
    assert round(float(old_flat), 4) == 0.04


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


@pytest.mark.asyncio
async def test_meter_free_tier_is_preview_only(monkeypatch):
    async def not_paid(*_a, **_k):
        return False
    monkeypatch.setattr(ccs, "merchant_is_paid_tier", not_paid)

    out = await ccs.meter_agent_workflow(
        "m", "agent_demand_test", provider="gemini", units=2, idempotency_key="k",
    )
    assert out["billing_mode"] == "preview_only"
    assert out["reason"] == "not_paid_tier"


@pytest.mark.asyncio
async def test_meter_paid_priceable_provider_meters(monkeypatch):
    captured = {}

    async def paid(*_a, **_k):
        return True

    async def fake_consume(merchant_id, operation_type, idempotency_key, **kw):
        captured.update(
            merchant_id=merchant_id, operation_type=operation_type,
            idempotency_key=idempotency_key, **kw,
        )
        return {"credits": kw.get("credits"), "replay": False}

    monkeypatch.setattr(ccs, "merchant_is_paid_tier", paid)
    monkeypatch.setattr(ccs, "consume", fake_consume)

    out = await ccs.meter_agent_workflow(
        "m", "agent_demand_test", provider="gemini", units=2, idempotency_key="k",
    )
    assert out["billing_mode"] == "metered"
    assert out["credits"] > 0
    assert captured["operation_type"] == "agent_demand_test"
    assert captured["idempotency_key"] == "k"


@pytest.mark.asyncio
async def test_meter_non_priceable_provider_is_preview_only(monkeypatch):
    async def paid(*_a, **_k):
        return True

    async def fail_consume(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("internal/merchant_platform must not debit")

    monkeypatch.setattr(ccs, "merchant_is_paid_tier", paid)
    monkeypatch.setattr(ccs, "consume", fail_consume)

    out = await ccs.meter_agent_workflow(
        "m", "agent_sku_match", provider="internal", units=10, idempotency_key="k",
    )
    assert out["billing_mode"] == "preview_only"
    assert out["reason"] == "provider_not_priceable"


@pytest.mark.asyncio
async def test_meter_zero_units_is_preview_only(monkeypatch):
    async def paid(*_a, **_k):
        return True

    async def fail_consume(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("zero-cost run must not debit")

    monkeypatch.setattr(ccs, "merchant_is_paid_tier", paid)
    monkeypatch.setattr(ccs, "consume", fail_consume)

    out = await ccs.meter_agent_workflow(
        "m", "agent_demand_test", provider="gemini", units=0, idempotency_key="k",
    )
    assert out["billing_mode"] == "preview_only"
