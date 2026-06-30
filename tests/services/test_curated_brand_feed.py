"""Tests for the curated-brand-list feed mapper (pure; no network)."""

from services.curated_brand_feed import shopify_product_to_record


def _product(**over):
    base = {
        "title": "Snail Mucin Gel Cleanser",
        "handle": "snail-mucin-gel-cleanser",
        "vendor": "COSRX",
        "product_type": "Cleanser",
        "tags": ["k-beauty", "cleanser"],
        "images": [{"src": "https://cdn.x/img.jpg"}],
        "variants": [{"price": "16.00", "barcode": "8809416470016", "available": True}],
    }
    base.update(over)
    return base


def test_maps_shopify_product_to_validated_record():
    rec = shopify_product_to_record(_product(), domain="cosrx.com", category_path="beauty/skincare/cleanser")
    pdp, offers = rec["pdp"], rec["offers"]
    assert pdp["brand"] == "COSRX"
    assert pdp["product_name"] == "Snail Mucin Gel Cleanser"
    assert pdp["category_path"] == "beauty/skincare/cleanser"
    assert pdp["barcode"] == "8809416470016"  # GTIN carried (strongest deposit basis)
    assert pdp["source_domain"] == "cosrx.com"
    assert pdp["tags"] == ["k-beauty", "cleanser"]
    assert offers[0]["canonical_url"] == "https://cosrx.com/products/snail-mucin-gel-cleanser"
    assert offers[0]["merchant_inferred"] == "COSRX"
    assert offers[0]["in_stock"] is True
    assert offers[0]["price"] == 16.0  # coerced to float (numeric columns reject strings)


def test_brand_override_and_domain_cleaning():
    rec = shopify_product_to_record(
        _product(vendor=""), domain="https://Cosrx.com/", category_path="x", brand_override="COSRX"
    )
    assert rec["pdp"]["brand"] == "COSRX"
    assert rec["offers"][0]["canonical_url"].startswith("https://cosrx.com/products/")


def test_comma_string_tags():
    rec = shopify_product_to_record(_product(tags="a, b ,c"), domain="x.com", category_path="x")
    assert rec["pdp"]["tags"] == ["a", "b", "c"]


def test_skips_unactionable():
    assert shopify_product_to_record({"handle": "h", "vendor": "V"}, domain="x.com", category_path="x") is None  # no title
    assert shopify_product_to_record({"title": "T", "vendor": "V"}, domain="x.com", category_path="x") is None  # no handle
    assert shopify_product_to_record(_product(vendor=""), domain="x.com", category_path="x") is None  # no brand
    assert shopify_product_to_record(None, domain="x.com", category_path="x") is None


def test_drops_unpriced_gift_items():
    # Gift-with-purchase / $0 / unpriced items have no purchasable offer → dropped
    # entirely so they never enter the commerce index.
    assert shopify_product_to_record(_product(variants=[{"price": None}]), domain="x.com", category_path="x") is None
    assert shopify_product_to_record(_product(variants=[{"price": "0.00"}]), domain="x.com", category_path="x") is None
    assert shopify_product_to_record(_product(variants=[]), domain="x.com", category_path="x") is None


def test_picks_first_positive_priced_variant():
    # First variant unpriced, second priced → keep the product, use the priced variant.
    rec = shopify_product_to_record(
        _product(variants=[{"price": "0.00", "barcode": "Z"}, {"price": "24.00", "barcode": "8809416470016", "available": True}]),
        domain="x.com",
        category_path="x",
    )
    assert rec is not None
    assert rec["offers"][0]["price"] == 24.0
    assert rec["pdp"]["barcode"] == "8809416470016"  # GTIN from the priced variant
