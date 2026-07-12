"""External-seed products (the bulk of catalog results) previously emitted no
`brand`, so agents couldn't cite the brand. Populate it from seed_data
brand/vendor, falling back to the merchant display name with a storefront suffix
stripped. Companion to the connected-merchant fix in _standard_to_shop_product."""
from __future__ import annotations

from routes.agent_shop_gateway import _external_seed_to_shop_product


def _p(**kw):
    return _external_seed_to_shop_product(
        row=kw.get("row", {"title": "X"}),
        seed_data=kw.get("seed_data", {"title": "X"}),
        redirect_url=kw.get("redirect_url"),
    )


def test_brand_from_seed_data_brand():
    p = _p(row={"title": "Centella Ampoule"}, seed_data={"brand": "SKIN1004", "title": "Centella Ampoule"})
    assert p["brand"] == "SKIN1004"


def test_brand_falls_back_to_vendor():
    p = _p(row={"title": "Snail Essence"}, seed_data={"vendor": "COSRX", "title": "Snail Essence"})
    assert p["brand"] == "COSRX"


def test_brand_falls_back_to_merchant_name_with_suffix_stripped():
    p = _p(row={"title": "Toner", "merchant_name": "Beauty of Joseon Official Site"}, seed_data={"title": "Toner"})
    assert p["brand"] == "Beauty of Joseon"


def test_brand_from_domain_derived_display_name():
    # The display-name builder derives "Cosrx Official Site" from the domain; the
    # suffix strip recovers the brand.
    p = _p(row={"title": "Toner", "destination_url": "https://cosrx.com/products/toner"}, seed_data={"title": "Toner"})
    assert p["brand"] == "Cosrx"


def test_brand_none_when_no_source():
    # The display-name builder falls back to a bare "Official Site" when nothing is
    # known — that is NOT a brand, so brand must be None (not the string
    # "Official Site").
    p = _p(row={"title": "X"}, seed_data={"title": "X"})
    assert p["brand"] is None
