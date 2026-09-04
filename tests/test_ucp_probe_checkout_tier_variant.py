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
from db import merchant_official_domains as domains
from db.database import database, engine, metadata
from jobs import scheduled_ucp_reprobe_job as job
from services import canonical_commerce_service as svc


@pytest.fixture(autouse=True)
async def _db():
    metadata.create_all(engine, tables=[canonical_variants, canonical_offers])
    if not database.is_connected:
        await database.connect()
    await domains.ensure_merchant_official_domains_table()
    await database.execute(canonical_offers.delete())
    await database.execute(canonical_variants.delete())
    await database.execute(domains.merchant_official_domains.delete())
    yield
    await database.execute(domains.merchant_official_domains.delete())
    await database.execute(canonical_offers.delete())
    await database.execute(canonical_variants.delete())


async def _variant(
    *, cvid, merchant, platform_variant_id, availability, platform="shopify",
    orderable=None, platform_product_id=None,
):
    await database.execute(canonical_variants.insert().values(
        canonical_variant_id=cvid,
        canonical_product_id=f"cp_{cvid}",
        merchant_id=merchant,
        platform=platform,
        # Defaults to the REVERSE of the variant id so the composite index
        # (merchant_id, platform, platform_product_id, platform_variant_id)
        # cannot hand the query rows in variant order for free. Without this the
        # ORDER BY is untested: deleting it entirely still passes.
        platform_product_id=(
            platform_product_id
            if platform_product_id is not None
            else str(platform_variant_id)[::-1]
        ),
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
        await _variant(cvid="v9", merchant="m1", platform_product_id="aaa",
                       platform_variant_id="900", availability="in_stock")
        await _variant(cvid="v2", merchant="m1", platform_product_id="zzz",
                       platform_variant_id="100", availability="available")
        await _variant(cvid="v5", merchant="m1", platform_product_id="mmm",
                       platform_variant_id="500", availability="instock")
        return [await svc.select_probe_variant_gid("m1") for _ in range(3)]

    picks = asyncio.run(scenario())
    # Product ids sort aaa < mmm < zzz, i.e. OPPOSITE the variant ids, so an
    # index scan alone would answer 900. Only the ORDER BY yields 100.
    assert picks == ["gid://shopify/ProductVariant/100"] * 3


def test_a_newly_ingested_variant_cannot_steal_the_pick():
    """Shopify ids grow monotonically, so a newer variant is a LONGER number.
    Under plain lexicographic order a new 14-digit id sorts before an existing
    13-digit one and moves the checkout we test to a different product."""
    async def scenario():
        await _variant(cvid="old", merchant="m1", platform_product_id="zzz",
                       platform_variant_id="4200000000000",   # 13 digits
                       availability="in_stock")
        first = await svc.select_probe_variant_gid("m1")
        await _variant(cvid="new", merchant="m1", platform_product_id="aaa",
                       platform_variant_id="41000000000000",  # 14 digits, newer
                       availability="in_stock")
        return first, await svc.select_probe_variant_gid("m1")

    first, after_ingest = asyncio.run(scenario())
    assert first == "gid://shopify/ProductVariant/4200000000000"
    assert after_ingest == first


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


def test_every_spelling_the_shared_vocabulary_accepts_reaches_the_selector():
    """A second copy of that set would drift. Asserting `_in_stock` agrees with
    its own constant proves nothing; this drives the SELECTOR with each spelling
    so a divergence between the two lanes fails here."""
    from services.offer_buyability import IN_STOCK_AVAILABILITY

    async def scenario(value):
        await database.execute(canonical_offers.delete())
        await database.execute(canonical_variants.delete())
        await _variant(cvid="v1", merchant="m1", platform_variant_id="100",
                       availability=value)
        return await svc.select_probe_variant_gid("m1")

    for spelling in sorted(IN_STOCK_AVAILABILITY):
        assert asyncio.run(scenario(spelling)) == (
            "gid://shopify/ProductVariant/100"
        ), f"selector refused {spelling!r} that offer_buyability accepts"


# ---------------------------------------------------------------------
# The enqueue, which is what actually delivers it
# ---------------------------------------------------------------------


def _enqueue_harness(
    monkeypatch, *, merchant_id="merchant_real", domain="shop.example",
    resolves_to="merchant_real", gid="gid://shopify/ProductVariant/7",
):
    monkeypatch.setenv("STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", "test-key")
    observed = {"selector_calls": [], "resolver_calls": []}

    async def fake_due(**_kwargs):
        return [{
            "execution_route_id": "route-1",
            "last_audit_run_id": "audit-1",
            "merchant_id": merchant_id,
            "normalized_domain": domain,
        }]

    async def fake_in_flight(**_kwargs):
        return False

    async def fake_enqueue(**kwargs):
        observed["enqueue"] = kwargs
        return "verify-1"

    async def fake_resolver(d):
        observed["resolver_calls"].append(d)
        return resolves_to

    async def fake_selector(merchant):
        observed["selector_calls"].append(merchant)
        return gid

    import db.audit_evidence as evidence_module
    import db.merchant_official_domains as domains_module
    monkeypatch.setattr(job, "list_due_ucp_routes", fake_due)
    monkeypatch.setattr(
        evidence_module, "has_in_flight_verification_for_route", fake_in_flight,
    )
    monkeypatch.setattr(evidence_module, "enqueue_verification_run", fake_enqueue)
    monkeypatch.setattr(
        domains_module, "resolve_verified_merchant_for_domain", fake_resolver,
    )
    monkeypatch.setattr(svc, "select_probe_variant_gid", fake_selector)
    return observed


def test_a_proven_domain_carries_the_variant_when_enabled(monkeypatch):
    observed = _enqueue_harness(monkeypatch)
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED", "true")

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())

    assert summary["enqueued"] == 1
    assert summary["variant_carried"] == 1
    assert observed["enqueue"]["product_key"] == "gid://shopify/ProductVariant/7"
    # The variant comes from the merchant that PROVED the route's domain, and
    # the domain asked about is the route's own.
    assert observed["resolver_calls"] == ["shop.example"]
    assert observed["selector_calls"] == ["merchant_real"]


def test_an_unproven_domain_can_never_reach_the_checkout_tier(monkeypatch):
    """The abuse fence. Anyone can type any domain into the public marketing
    form; that must never become a create_checkout on a stranger's store."""
    observed = _enqueue_harness(
        monkeypatch, merchant_id="prospect_deadbeef0000",
        domain="somebody-elses-shop.example", resolves_to=None,
    )
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED", "true")

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())

    assert summary["enqueued"] == 1
    assert summary["variant_carried"] == 0
    assert observed["enqueue"]["merchant_id"] is None
    assert observed["enqueue"]["product_key"] is None
    # Not merely absent from the enqueue — no catalogue was ever consulted.
    assert observed["selector_calls"] == []


def test_the_route_merchant_id_column_cannot_open_the_gate(monkeypatch):
    """A regression guard on the defect this design replaced.

    An earlier cut gated on `route["merchant_id"]`. Nothing in the tree writes
    that column — `claim_execution_route` has no callers — so the gate could
    never open and the feature would have shipped dead. It must now be the
    PROVEN domain association that decides, and route["merchant_id"] must not
    be able to substitute for it in either direction.
    """
    observed = _enqueue_harness(
        monkeypatch, merchant_id=None, domain="shop.example",
        resolves_to="merchant_real",
    )
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED", "true")

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())

    # merchant_id is NULL, exactly as production stores it, and the tier still
    # runs because the domain is proven.
    assert observed["enqueue"]["merchant_id"] is None
    assert summary["variant_carried"] == 1
    assert observed["enqueue"]["product_key"] == "gid://shopify/ProductVariant/7"


def test_checkout_tier_is_default_off(monkeypatch):
    observed = _enqueue_harness(monkeypatch)
    monkeypatch.delenv("STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED", raising=False)

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())

    assert summary["enqueued"] == 1
    assert summary["variant_carried"] == 0
    assert observed["enqueue"]["product_key"] is None
    # Default-off costs nothing: no domain lookup, no catalogue read.
    assert observed["resolver_calls"] == []
    assert observed["selector_calls"] == []


def test_a_variant_lookup_failure_still_enqueues_the_reprobe(monkeypatch):
    observed = _enqueue_harness(monkeypatch)
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED", "true")

    async def boom(_merchant):
        raise RuntimeError("catalogue down")

    monkeypatch.setattr(svc, "select_probe_variant_gid", boom)

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())

    assert summary["enqueued"] == 1
    assert summary["variant_carried"] == 0
    assert observed["enqueue"]["product_key"] is None


def test_a_domain_resolution_failure_still_enqueues_the_reprobe(monkeypatch):
    observed = _enqueue_harness(monkeypatch)
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED", "true")

    async def boom(_domain):
        raise RuntimeError("domains table down")

    import db.merchant_official_domains as domains_module
    monkeypatch.setattr(
        domains_module, "resolve_verified_merchant_for_domain", boom,
    )

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())

    assert summary["enqueued"] == 1
    assert observed["enqueue"]["product_key"] is None
    assert observed["selector_calls"] == []


# ---------------------------------------------------------------------
# The fence itself: which merchant, if any, has PROVEN this domain
# ---------------------------------------------------------------------


async def _official(merchant, domain, verification_status, source="asserted"):
    await database.execute(domains.merchant_official_domains.insert().values(
        merchant_id=merchant, domain=domain, source=source,
        verification_status=verification_status,
    ))


def test_resolves_a_verified_domain_to_its_merchant():
    async def scenario():
        await _official("m1", "shop.example", domains.VERIFICATION_VERIFIED)
        return await domains.resolve_verified_merchant_for_domain("shop.example")

    assert asyncio.run(scenario()) == "m1"


def test_refuses_a_domain_that_is_only_asserted():
    """`asserted` and `inferred` rows sit at NULL until something checks them.
    A self-declared store_url is not proof that the storefront is theirs."""
    async def scenario():
        await _official("m1", "shop.example", None, source="asserted")
        return await domains.resolve_verified_merchant_for_domain("shop.example")

    assert asyncio.run(scenario()) is None


def test_refuses_pending_and_failed_verification():
    async def scenario():
        out = []
        for status in (domains.VERIFICATION_PENDING, domains.VERIFICATION_FAILED):
            await database.execute(domains.merchant_official_domains.delete())
            await _official("m1", "shop.example", status)
            out.append(
                await domains.resolve_verified_merchant_for_domain("shop.example")
            )
        return out

    assert asyncio.run(scenario()) == [None, None]


def test_refuses_a_domain_two_merchants_both_claim_verified():
    """Ambiguity is not a tie to break. Picking one would POST a create_checkout
    built from one merchant's catalogue at whichever storefront answers."""
    async def scenario():
        await _official("m1", "shop.example", domains.VERIFICATION_VERIFIED)
        await _official("m2", "shop.example", domains.VERIFICATION_VERIFIED)
        return await domains.resolve_verified_merchant_for_domain("shop.example")

    assert asyncio.run(scenario()) is None


def test_domain_match_is_case_and_whitespace_insensitive():
    async def scenario():
        await _official("m1", "shop.example", domains.VERIFICATION_VERIFIED)
        return await domains.resolve_verified_merchant_for_domain(
            "  SHOP.Example  "
        )

    assert asyncio.run(scenario()) == "m1"


def test_an_unknown_domain_resolves_to_nobody():
    async def scenario():
        await _official("m1", "shop.example", domains.VERIFICATION_VERIFIED)
        return await domains.resolve_verified_merchant_for_domain("other.example")

    assert asyncio.run(scenario()) is None


def test_a_blank_domain_never_queries():
    assert asyncio.run(
        domains.resolve_verified_merchant_for_domain("   ")
    ) is None
