"""P1 — 'wire the reader': flag-gated brand_direct offer-type resolution.

Makes a VERIFIED brand_direct claim load-bearing at serving. Neutrality note:
offer_type is decision/display metadata, NEVER a rank signal (P0.3).
"""

import inspect

import services.pivot_query_service as pqs
from services.offer_classification import OFFER_TYPE_BRAND_DIRECT
from services.pivot_query_service import (
    _brand_direct_reader_enabled,
    _resolve_offer_type,
)


def test_all_canonical_offer_paths_supply_brand_relationship():
    """All three offer-node SQL paths must expose the OFFER SELLER's
    brand_relationship so the shared _build_canonical_offer_node reader can
    classify brand_direct consistently — canonical search, product-scoped, and
    sku-scoped. The join MUST be offer-seller-scoped (o.merchant_id), not the
    product owner (p.merchant_id), or a retailer offer on a brand's product would
    be mis-classified."""
    for fn in (
        pqs._fetch_canonical_search_rows,
        pqs._fetch_canonical_rows_for_product,
        pqs._fetch_canonical_rows_for_sku,
    ):
        src = inspect.getsource(fn)
        assert "metadata_json->>'brand_relationship'" in src, (
            f"{fn.__name__} must SELECT the seller's brand_relationship"
        )
        assert "bm.merchant_id = o.merchant_id" in src, (
            f"{fn.__name__} must join the seller merchant on o.merchant_id"
        )


def test_stored_offer_type_wins():
    assert (
        _resolve_offer_type(
            "retailer", "internal_merchant", "brand_direct", brand_direct_enabled=True
        )
        == "retailer"
    )


def test_external_referral_null_stays_unknown_regardless_of_flag():
    # Fix Plan C: a NULL stored offer_type on an external_referral row is an
    # authoritative "unknown" (the domain-based derivation already ran at write
    # time and found no evidence). The reader must NOT re-guess 'retailer' from
    # the lane — neither with the brand_direct flag on nor off.
    assert (
        _resolve_offer_type(None, "external_referral", None, brand_direct_enabled=False)
        is None
    )
    assert (
        _resolve_offer_type(None, "external_referral", None, brand_direct_enabled=True)
        is None
    )


def test_external_referral_stored_offer_type_still_wins():
    # The domain-derived value written to catalog_offers.offer_type is
    # authoritative and still surfaces unchanged.
    assert (
        _resolve_offer_type("retailer", "external_referral", None, brand_direct_enabled=False)
        == "retailer"
    )
    assert (
        _resolve_offer_type(
            "brand_direct", "external_referral", None, brand_direct_enabled=False
        )
        == "brand_direct"
    )


def test_internal_brand_direct_only_when_flag_on_and_verified():
    # flag ON + verified seller -> brand_direct (now load-bearing)
    assert (
        _resolve_offer_type(
            None, "internal_merchant", "brand_direct", brand_direct_enabled=True
        )
        == OFFER_TYPE_BRAND_DIRECT
    )
    # flag OFF -> ships dark (unchanged: None) even for a verified seller
    assert (
        _resolve_offer_type(
            None, "internal_merchant", "brand_direct", brand_direct_enabled=False
        )
        is None
    )
    # flag ON but seller NOT verified -> never assumed
    assert (
        _resolve_offer_type(None, "internal_merchant", None, brand_direct_enabled=True)
        is None
    )
    assert (
        _resolve_offer_type(
            None, "internal_merchant", "reseller", brand_direct_enabled=True
        )
        is None
    )


def test_reader_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("ENABLE_BRAND_DIRECT_OFFER_TYPE", raising=False)
    assert _brand_direct_reader_enabled() is False
    monkeypatch.setenv("ENABLE_BRAND_DIRECT_OFFER_TYPE", "true")
    assert _brand_direct_reader_enabled() is True
    monkeypatch.setenv("ENABLE_BRAND_DIRECT_OFFER_TYPE", "0")
    assert _brand_direct_reader_enabled() is False
