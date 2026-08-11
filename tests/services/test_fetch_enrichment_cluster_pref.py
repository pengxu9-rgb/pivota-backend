"""P1 Finding B — _fetch_enrichment_for_canonical prefers a brand-attested overlay
across the content_key cluster, so attest on a non-canonical seed still serves
(previously the assembler fetched enrichment only for pick_canonical's product)."""

import asyncio

import db.product_enrichment
from services.agent_pdp_view_assembler import _fetch_enrichment_for_canonical


def _seed():  # url_audit seed: no pivota_signature_id -> loses pick_canonical
    return {
        "merchant_id": "m1",
        "platform": "url_audit",
        "source_product_id": "anua.com/p/x",
        "product_key": "m1|url_audit|anua.com/p/x",
        "pivota_signature_id": None,
    }


def _synced():  # synced product: has a signature -> wins pick_canonical
    return {
        "merchant_id": "m1",
        "platform": "shopify",
        "source_product_id": "999",
        "product_key": "prod::m1::shopify::999",
        "pivota_signature_id": "sig_abc",
    }


def _patch_get_enrichment(monkeypatch, by_triple):
    """Stub the BULK fetch the assembler now uses.

    It previously called `get_enrichment` once per cluster member — one query AND
    one `ensure_product_enrichment_table()` DDL attempt each, on a path now
    reached from a live merchant PUT. It issues one merchant-scoped
    `get_enrichments_for_products` per DISTINCT MERCHANT instead. These tests pin
    the selection SEMANTICS, which are unchanged: same fixtures, same
    expectations, driven through the new call.
    """
    calls: list = []

    async def _get_bulk(merchant_id, *, product_keys=None, geo_code="default"):
        calls.append(merchant_id)
        wanted = set(product_keys or [])
        return {
            (platform, source_product_id): overlay
            for (m, platform, source_product_id), overlay in by_triple.items()
            if m == merchant_id and (not wanted or (platform, source_product_id) in wanted)
        }

    monkeypatch.setattr(db.product_enrichment, "get_enrichments_for_products", _get_bulk)
    return calls


def test_brand_attested_on_noncanonical_seed_wins(monkeypatch):
    # canonical = the synced product (has a signature), but the brand attested
    # the SEED -> the attested overlay must still be the one served.
    attested = {"title_override": "Anua Toner", "updated_by_employee_id": "brand_attestation"}
    _patch_get_enrichment(
        monkeypatch,
        {
            ("m1", "url_audit", "anua.com/p/x"): attested,  # seed's attested overlay
            ("m1", "shopify", "999"): {"description_markdown": "synced copy"},  # canonical, not attested
        },
    )
    out = asyncio.run(_fetch_enrichment_for_canonical([_seed(), _synced()]))
    assert out == attested  # brand-attested overlay wins across the cluster


def test_falls_back_to_canonical_when_no_attestation(monkeypatch):
    canonical_overlay = {"description_markdown": "synced copy"}
    _patch_get_enrichment(
        monkeypatch,
        {
            ("m1", "shopify", "999"): canonical_overlay,  # canonical
            ("m1", "url_audit", "anua.com/p/x"): {"description_markdown": "seed copy"},  # non-attested
        },
    )
    out = asyncio.run(_fetch_enrichment_for_canonical([_seed(), _synced()]))
    assert out == canonical_overlay  # canonical's overlay when nothing is attested


def test_none_when_no_overlays(monkeypatch):
    _patch_get_enrichment(monkeypatch, {})
    assert asyncio.run(_fetch_enrichment_for_canonical([_seed(), _synced()])) is None
    assert asyncio.run(_fetch_enrichment_for_canonical([])) is None


def test_one_query_per_merchant_not_one_per_cluster_member(monkeypatch):
    """The cost guard. Prod clusters run to 45 members and the largest are all
    merchant_id 'external_seed' — the exact cohort the seed writer fires on — and
    this path is now reached from a live merchant PUT, so a per-member loop is
    both N round trips and N DDL attempts. Nothing asserted the query count
    before, so a regression to per-member would have been invisible."""
    cluster = [_seed(), _synced()]
    # A 12-member single-merchant cluster, the shape that dominates the tail.
    cluster += [
        {"merchant_id": "m1", "platform": "url_audit",
         "source_product_id": f"anua.com/p/{i}",
         "product_key": f"m1|url_audit|anua.com/p/{i}",
         "pivota_signature_id": None}
        for i in range(10)
    ]
    calls = _patch_get_enrichment(monkeypatch, {("m1", "shopify", "999"): {"description_markdown": "x"}})

    asyncio.run(_fetch_enrichment_for_canonical(cluster))

    assert calls == ["m1"], (
        f"expected ONE merchant-scoped query for a 12-member single-merchant "
        f"cluster, got {len(calls)}: {calls}"
    )


def test_a_multi_merchant_cluster_queries_each_merchant_once(monkeypatch):
    """Grouping must not collapse merchants together — get_enrichments_for_products
    is merchant-scoped, so a cluster spanning merchants needs one call each."""
    other = {"merchant_id": "m2", "platform": "shopify", "source_product_id": "555",
             "product_key": "prod::m2::shopify::555", "pivota_signature_id": None}
    calls = _patch_get_enrichment(monkeypatch, {})

    asyncio.run(_fetch_enrichment_for_canonical([_seed(), _synced(), other]))

    assert sorted(calls) == ["m1", "m2"]


def test_one_merchants_failure_does_not_hide_anothers_overlay(monkeypatch):
    """Grouping introduces a failure mode the per-member loop did not have: a
    whole merchant's overlays vanish on one error. The surviving merchant's
    overlay must still be found, and the failure must still raise the sentinel
    only when nothing was resolved."""
    import services.agent_pdp_view_assembler as apv

    async def _get_bulk(merchant_id, *, product_keys=None, geo_code="default"):
        if merchant_id == "m2":
            raise RuntimeError("connection reset")
        return {("shopify", "999"): {"description_markdown": "synced copy"}}

    monkeypatch.setattr(db.product_enrichment, "get_enrichments_for_products", _get_bulk)
    other = {"merchant_id": "m2", "platform": "shopify", "source_product_id": "555",
             "product_key": "prod::m2::shopify::555", "pivota_signature_id": None}

    out = asyncio.run(_fetch_enrichment_for_canonical([_synced(), other]))
    assert out == {"description_markdown": "synced copy"}
    assert out is not apv.FETCH_FAILED
