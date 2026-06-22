"""P1 — 'wire the reader': flag-gated brand_direct offer-type resolution.

Makes a VERIFIED brand_direct claim load-bearing at serving. Neutrality note:
offer_type is decision/display metadata, NEVER a rank signal (P0.3).
"""

from services.offer_classification import OFFER_TYPE_BRAND_DIRECT, OFFER_TYPE_RETAILER
from services.pivot_query_service import (
    _brand_direct_reader_enabled,
    _resolve_offer_type,
)


def test_stored_offer_type_wins():
    assert (
        _resolve_offer_type(
            "retailer", "internal_merchant", "brand_direct", brand_direct_enabled=True
        )
        == "retailer"
    )


def test_external_referral_is_retailer_regardless_of_flag():
    assert (
        _resolve_offer_type(None, "external_referral", None, brand_direct_enabled=False)
        == OFFER_TYPE_RETAILER
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
