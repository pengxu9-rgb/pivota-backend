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
