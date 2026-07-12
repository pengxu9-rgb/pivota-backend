"""The find_products contract previously emitted no `brand` (always null), so
agents couldn't cite "<brand>'s <product>" without parsing the title. Populate it
from StandardProduct.vendor, falling back to the merchant display name with a
trailing " Official Site/Store" suffix stripped."""
from __future__ import annotations

from routes.agent_shop_gateway import _derive_product_brand


class _P:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_brand_prefers_vendor():
    assert _derive_product_brand(_P(vendor="ACROPASS", brand=None, merchant_name="Acropass Official Site")) == "ACROPASS"


def test_brand_falls_back_to_vendorless_brand_field():
    assert _derive_product_brand(_P(vendor=None, brand="COSRX", merchant_name=None)) == "COSRX"


def test_brand_strips_official_suffix_from_merchant_name():
    assert _derive_product_brand(_P(vendor=None, brand=None, merchant_name="Iunik Official Store")) == "Iunik"
    assert _derive_product_brand(_P(vendor=None, brand=None, merchant_name="Beauty of Joseon Official Site")) == "Beauty of Joseon"


def test_brand_none_when_no_source():
    assert _derive_product_brand(_P(vendor=None, brand=None, merchant_name=None)) is None
    assert _derive_product_brand(_P(vendor="", brand="", merchant_name="")) is None
