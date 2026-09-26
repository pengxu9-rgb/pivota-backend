"""wet n wild's own store files 500 of 580 products under its CORPORATE vendor "Markwins WNW".

Measured 2026-09-26 on www.wetnwildbeauty.com (/products.json: 500 "Markwins WNW", 80 "wet n wild
Beauty"; /meta.json name "wet n wild Beauty", myshopify domain markwins-wnw). Without a family those
500 rows were branded "Markwins WNW" and the brand_official domain review held the job, because the
store's name does not start with "Markwins WNW".
"""
from __future__ import annotations

import pytest

from services.curated_brand_feed import shopify_product_to_record
from services.retailer_ingest.pipeline import brand_official_domain_flags

STOREFRONT = {"name": "wet n wild Beauty", "currency": "USD", "country": "US",
              "ships_to_countries": ["CA", "US"], "myshopify_domain": "markwins-wnw.myshopify.com"}


def _product(vendor, title="MegaLast Lipstick"):
    return {"title": title, "handle": "h-" + vendor.replace(" ", "-"), "vendor": vendor, "product_type": "Lipstick",
            "variants": [{"id": 1, "price": "5.99", "available": True, "sku": "S1"}],
            "images": [{"src": "https://www.wetnwildbeauty.com/i.jpg"}]}


@pytest.mark.parametrize("vendor", ["Markwins WNW", "wet n wild Beauty", "wet n wild", "WET N WILD"])
def test_every_wet_n_wild_vendor_spelling_writes_one_brand(vendor):
    for source_role, kwargs in (("brand_official", {}), ("retailer", {"retailer_name": "Some Store"})):
        rec = shopify_product_to_record(_product(vendor), domain="www.wetnwildbeauty.com" if source_role ==
                                        "brand_official" else "somestore.com", category_path="beauty",
                                        brand_override="wet n wild", source_role=source_role, **kwargs)
        assert rec is not None, (vendor, source_role)
        assert rec["pdp"]["brand"] == "wet n wild", (vendor, source_role)


def test_another_markwins_label_is_not_relabelled():
    """Only the WNW code names this brand: Markwins' other labels keep their own vendor."""
    rec = shopify_product_to_record(_product("Markwins Physicians Formula"), domain="somestore.com",
                                    category_path="beauty", brand_override=None, source_role="retailer",
                                    retailer_name="Some Store")
    assert rec is not None and rec["pdp"]["brand"] == "Markwins Physicians Formula"


def test_the_store_proves_itself_once_the_brand_is_right():
    """Tier B: the store's /meta.json name starts with the brand, ships to the US, prices in USD."""
    assert brand_official_domain_flags("www.wetnwildbeauty.com", ["wet n wild"], storefront=STOREFRONT) == []


def test_the_corporate_spelling_is_what_the_review_refused():
    """The control: with the vendor's own spelling as the brand, the same storefront does not prove it."""
    flags = brand_official_domain_flags("www.wetnwildbeauty.com", ["Markwins WNW"], storefront=STOREFRONT)
    assert [f["rule"] for f in flags] == ["brand_official_domain_unproven"]
