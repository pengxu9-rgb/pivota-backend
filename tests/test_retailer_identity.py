"""Retailer-aware merchant identity (R0 + R1): fold a product's vendor into the
merchant identity only when the vendor IS the merchant (a D2C brand selling its own
products). A RETAILER's resold brands must NOT be folded — otherwise their domains
(ownist.com) get mis-credited as the store's own findability (the Chydan bug).
"""

from services.agent_center_bd_report_service import (
    _audit_merchant_vendors,
    _vendor_is_merchant,
)
from services.brand_alias import derive_brand_aliases


def test_reseller_does_not_fold_resold_brands():
    # Chydan resells NUTRIONE + Ownist products — neither is Chydan.
    ident, is_reseller = _audit_merchant_vendors("Chydan", "chydan.com", ["NUTRIONE", "Ownist"])
    assert is_reseller is True
    # identity is the store only; resold brands excluded
    assert "ownist" not in [a.lower() for a in ident]
    assert "nutrione" not in [a.lower() for a in ident]


def test_d2c_brand_folds_its_own_vendor():
    # bblab.shop selling its own "BB Lab" product — a D2C brand, unchanged.
    ident, is_reseller = _audit_merchant_vendors("BB Lab", "bblab.shop", ["BB Lab"])
    assert is_reseller is False


def test_d2c_with_legal_name_differing_from_brand_still_folds_via_host():
    # The edge case: merchant_name is the legal entity, vendor is the consumer
    # brand, but the store domain bridges them — must stay a brand (fold the vendor).
    ident, is_reseller = _audit_merchant_vendors("NUTRIONE CO LTD", "bblab.shop", ["BB Lab"])
    assert is_reseller is False
    assert any("bb lab" in a.lower() or "bblab" in a.lower() for a in ident)


def test_mixed_reseller_with_own_private_label():
    # Sells its own brand AND a third-party brand → reseller; own brand folded,
    # third-party not.
    ident, is_reseller = _audit_merchant_vendors("Chydan", "chydan.com", ["Chydan", "Ownist"])
    assert is_reseller is True  # carries a foreign brand
    assert "ownist" not in [a.lower() for a in ident]


def test_vendor_is_merchant_matching():
    own = frozenset(derive_brand_aliases("Chydan", "chydan.com"))
    assert _vendor_is_merchant("Chydan", own) is True
    assert _vendor_is_merchant("Ownist", own) is False
    assert _vendor_is_merchant("", own) is False


def test_no_vendors_is_not_reseller():
    ident, is_reseller = _audit_merchant_vendors("Chydan", "chydan.com", [])
    assert is_reseller is False  # nothing resold → not flagged


# --- store_less durable brand classification (issue #1521 sibling) ----------


def test_store_less_is_brand_regardless_of_vendor_mix():
    # A store-less signup IS a brand by definition (no retail storefront). Even a
    # catalog of foreign-looking vendors must NOT re-derive it as a reseller —
    # the demo merch_a2b08ee928dd9da5 flip-flopping to reseller on every audit.
    ident, is_reseller = _audit_merchant_vendors(
        "Demo Brand", "demobrand.com", ["NUTRIONE", "Ownist"],
        operating_mode="store_less",
    )
    assert is_reseller is False


def test_store_less_case_insensitive():
    _, is_reseller = _audit_merchant_vendors(
        "Demo Brand", "demobrand.com", ["Ownist"], operating_mode="Store_Less",
    )
    assert is_reseller is False


def test_storefront_mode_unchanged_still_reseller():
    # operating_mode='storefront' (or None) must be byte-identical to before.
    ident_sf, is_reseller_sf = _audit_merchant_vendors(
        "Chydan", "chydan.com", ["NUTRIONE", "Ownist"], operating_mode="storefront",
    )
    ident_none, is_reseller_none = _audit_merchant_vendors(
        "Chydan", "chydan.com", ["NUTRIONE", "Ownist"],
    )
    assert is_reseller_sf is True
    assert is_reseller_none is True
    assert ident_sf == ident_none


def test_store_less_identity_folding_preserved():
    # The fix flips only the reseller flag — identity folding is unchanged, so a
    # store_less D2C brand still folds its own vendor.
    ident, is_reseller = _audit_merchant_vendors(
        "BB Lab", "bblab.shop", ["BB Lab"], operating_mode="store_less",
    )
    assert is_reseller is False
    assert any("bb lab" in a.lower() or "bblab" in a.lower() for a in ident)
