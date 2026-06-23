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
    async def _get(merchant_id, platform, source_product_id, geo_code="default"):
        return by_triple.get((merchant_id, platform, source_product_id))

    monkeypatch.setattr(db.product_enrichment, "get_enrichment", _get)


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
