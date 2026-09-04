"""The variant that unlocks the UCP probe's checkout-tested tier.

The gateway probe (PIVOTA-Agent src/services/ucpStoreAuditProbe.js) records
`priced_facts.checkout_status` — the only signal in this lane that separates a
store an agent can actually buy from, from one that merely advertises UCP —
and it reaches that branch ONLY when the claim hands it a variant_gid. That gid
is read off verification_runs.product_key. Until now nothing set it, so the
tier never ran and every route in the system reported `detected`.

These tests hold both halves: that a known merchant's reprobe now carries a
real gid, and that a prospect one never can.
"""

from __future__ import annotations

import asyncio

import pytest

from db.canonical_commerce import canonical_offers, canonical_variants
from db.database import database, engine, metadata
from jobs import scheduled_ucp_reprobe_job as job
from services import canonical_commerce_service as svc


@pytest.fixture(autouse=True)
async def _db():
    metadata.create_all(engine, tables=[canonical_variants, canonical_offers])
    if not database.is_connected:
        await database.connect()
    await database.execute(canonical_offers.delete())
    await database.execute(canonical_variants.delete())
    yield
    await database.execute(canonical_offers.delete())
    await database.execute(canonical_variants.delete())


async def _variant(
    *, cvid, merchant, platform_variant_id, availability, platform="shopify",
    orderable=None,
):
    await database.execute(canonical_variants.insert().values(
        canonical_variant_id=cvid,
        canonical_product_id=f"cp_{cvid}",
        merchant_id=merchant,
        platform=platform,
        platform_product_id="900",
        platform_variant_id=platform_variant_id,
        title="A product",
        standard_variant_data={},
        source_payload_hash="h" * 8,
    ))
    if availability is not None:
        await database.execute(canonical_offers.insert().values(
            canonical_offer_id=f"co_{cvid}",
            canonical_product_id=f"cp_{cvid}",
            canonical_variant_id=cvid,
            merchant_id=merchant,
            currency="USD",
            amount=19.00,
            availability=availability,
            orderable=orderable,
            source_payload_hash="h" * 8,
        ))


# ---------------------------------------------------------------------
# The selector, against real SQL
# ---------------------------------------------------------------------


def test_selects_an_in_stock_shopify_variant_as_a_gid():
    async def scenario():
        await _variant(
            cvid="v1", merchant="m1",
            platform_variant_id="51086327775448", availability="in_stock",
        )
        return await svc.select_probe_variant_gid("m1")

    assert asyncio.run(scenario()) == (
        "gid://shopify/ProductVariant/51086327775448"
    )


def test_picks_the_same_variant_every_time_for_one_merchant():
    """Stability is the safety property, not a detail.

    The tested tier's one side effect is a create_checkout against the
    merchant's live store. A selector that wandered would spray an abandoned
    checkout across a different product on every weekly reprobe.
    """
    async def scenario():
        await _variant(cvid="v9", merchant="m1",
                       platform_variant_id="900", availability="in_stock")
        await _variant(cvid="v2", merchant="m1",
                       platform_variant_id="100", availability="available")
        await _variant(cvid="v5", merchant="m1",
                       platform_variant_id="500", availability="instock")
        return [await svc.select_probe_variant_gid("m1") for _ in range(3)]

    picks = asyncio.run(scenario())
    assert picks == ["gid://shopify/ProductVariant/100"] * 3


def test_skips_a_variant_that_is_not_buyable():
    async def scenario():
        await _variant(cvid="v1", merchant="m1",
                       platform_variant_id="100", availability="out_of_stock")
        await _variant(cvid="v2", merchant="m1",
                       platform_variant_id="200", availability="in_stock")
        return await svc.select_probe_variant_gid("m1")

    assert asyncio.run(scenario()) == "gid://shopify/ProductVariant/200"


def test_skips_a_variant_with_no_offer_row_at_all():
    """No offer row is no availability, and unknown must not buy a checkout."""
    async def scenario():
        await _variant(cvid="v1", merchant="m1",
                       platform_variant_id="100", availability=None)
        return await svc.select_probe_variant_gid("m1")

    assert asyncio.run(scenario()) is None


def test_never_reaches_across_merchants():
    async def scenario():
        await _variant(cvid="v1", merchant="other",
                       platform_variant_id="100", availability="in_stock")
        return await svc.select_probe_variant_gid("m1")

    assert asyncio.run(scenario()) is None


def test_ignores_a_non_shopify_platform():
    async def scenario():
        await _variant(cvid="v1", merchant="m1", platform="woocommerce",
                       platform_variant_id="100", availability="in_stock")
        return await svc.select_probe_variant_gid("m1")

    assert asyncio.run(scenario()) is None


def test_refuses_a_non_numeric_variant_id():
    """A non-numeric id would travel to the merchant's door only to be
    refused there; merchant_ucp_checkout rejects it upstream."""
    async def scenario():
        await _variant(cvid="v1", merchant="m1",
                       platform_variant_id="not-a-number",
                       availability="in_stock")
        return await svc.select_probe_variant_gid("m1")

    assert asyncio.run(scenario()) is None


def test_a_blank_merchant_never_queries():
    assert asyncio.run(svc.select_probe_variant_gid("  ")) is None


def test_uses_the_shared_in_stock_vocabulary():
    """A second copy of this set would drift; the selector must read the one
    services/offer_buyability.py already publishes."""
    from services.offer_buyability import IN_STOCK_AVAILABILITY, _in_stock

    for value in IN_STOCK_AVAILABILITY:
        assert _in_stock(value) is True


# ---------------------------------------------------------------------
# The enqueue, which is what actually delivers it
# ---------------------------------------------------------------------


def _enqueue_harness(monkeypatch, *, merchant_id, gid="gid://shopify/ProductVariant/7"):
    monkeypatch.setenv("STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", "test-key")
    observed = {"selector_calls": []}

    async def fake_due(**_kwargs):
        return [{
            "execution_route_id": "route-1",
            "last_audit_run_id": "audit-1",
            "merchant_id": merchant_id,
        }]

    async def fake_in_flight(**_kwargs):
        return False

    async def fake_enqueue(**kwargs):
        observed["enqueue"] = kwargs
        return "verify-1"

    async def fake_selector(merchant):
        observed["selector_calls"].append(merchant)
        return gid

    import db.audit_evidence as evidence_module
    monkeypatch.setattr(job, "list_due_ucp_routes", fake_due)
    monkeypatch.setattr(
        evidence_module, "has_in_flight_verification_for_route", fake_in_flight,
    )
    monkeypatch.setattr(evidence_module, "enqueue_verification_run", fake_enqueue)
    monkeypatch.setattr(svc, "select_probe_variant_gid", fake_selector)
    return observed


def test_known_merchant_reprobe_carries_the_variant_when_enabled(monkeypatch):
    observed = _enqueue_harness(monkeypatch, merchant_id="merchant_real")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED", "true")

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())

    assert summary["enqueued"] == 1
    assert observed["enqueue"]["product_key"] == "gid://shopify/ProductVariant/7"
    assert observed["selector_calls"] == ["merchant_real"]


def test_a_prospect_domain_can_never_reach_the_checkout_tier(monkeypatch):
    """The abuse fence. Anyone can type any domain into the public marketing
    form; that must never become a create_checkout on a stranger's store."""
    observed = _enqueue_harness(monkeypatch, merchant_id="prospect_deadbeef0000")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED", "true")

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())

    assert summary["enqueued"] == 1
    assert observed["enqueue"]["merchant_id"] is None
    assert observed["enqueue"]["product_key"] is None
    # Not merely absent from the enqueue — never looked up at all.
    assert observed["selector_calls"] == []


def test_checkout_tier_is_default_off(monkeypatch):
    observed = _enqueue_harness(monkeypatch, merchant_id="merchant_real")
    monkeypatch.delenv("STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED", raising=False)

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())

    assert summary["enqueued"] == 1
    assert observed["enqueue"]["product_key"] is None
    assert observed["selector_calls"] == []


def test_a_variant_lookup_failure_still_enqueues_the_reprobe(monkeypatch):
    observed = _enqueue_harness(monkeypatch, merchant_id="merchant_real")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED", "true")

    async def boom(_merchant):
        raise RuntimeError("catalogue down")

    monkeypatch.setattr(svc, "select_probe_variant_gid", boom)

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())

    assert summary["enqueued"] == 1
    assert observed["enqueue"]["product_key"] is None


def test_skips_an_offer_that_says_it_cannot_be_ordered():
    async def scenario():
        await _variant(cvid="v1", merchant="m1", platform_variant_id="100",
                       availability="in_stock", orderable=False)
        await _variant(cvid="v2", merchant="m1", platform_variant_id="200",
                       availability="in_stock", orderable=True)
        return await svc.select_probe_variant_gid("m1")

    assert asyncio.run(scenario()) == "gid://shopify/ProductVariant/200"


def test_an_unknown_orderable_still_qualifies():
    """NULL is unknown, not False — refusing it would empty the lane for every
    ingest path that never writes the column."""
    async def scenario():
        await _variant(cvid="v1", merchant="m1", platform_variant_id="100",
                       availability="in_stock", orderable=None)
        return await svc.select_probe_variant_gid("m1")

    assert asyncio.run(scenario()) == "gid://shopify/ProductVariant/100"
