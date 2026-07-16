"""Tests for the retailer<->canonical match layer + StyleKorean adapter pure fns.

Cases are drawn from the StyleKorean COSRX pilot (2026-07-16), where naive
brand+title matching recovered 3/107 and this normalizer recovered 52/107.
"""

from services.pdp_matcher.retailer_match import (
    build_match_index,
    match_record,
    retailer_match_key,
)
from services.retailer_ingest import stylekorean as sk


# --- retailer_match_key: size / pack / promo stripping ------------------------

def test_size_suffix_stripped_so_retailer_aligns_with_canonical():
    # SK appends size; our canonical is size-agnostic -> same key.
    assert retailer_match_key("COSRX", "Advanced Snail 96 Mucin Power Essence 100ml") == \
           retailer_match_key("COSRX", "Advanced Snail 96 Mucin Power Essence")


def test_pack_and_promo_noise_stripped():
    a = retailer_match_key("COSRX", "Low pH Good Morning Gel Cleanser Double Duo (150ml + 50ml freegift)")
    b = retailer_match_key("COSRX", "Low pH Good Morning Gel Cleanser")
    assert a == b


def test_pack_count_stripped():
    assert retailer_match_key("COSRX", "Acne Pimple Master Patch 3ea x 24 patches") == \
           retailer_match_key("COSRX", "Acne Pimple Master Patch")


def test_active_strength_numbers_preserved_not_treated_as_size():
    # These numbers ARE identity; they must survive and keep products distinct.
    k_aha7 = retailer_match_key("COSRX", "AHA 7 Whitehead Power Liquid 100ml")
    k_bha = retailer_match_key("COSRX", "BHA Blackhead Power Liquid 100ml")
    assert "7" in k_aha7
    assert k_aha7 != k_bha
    assert retailer_match_key("COSRX", "The Vitamin C 23 Serum 20ml") != \
           retailer_match_key("COSRX", "The Vitamin C 13 Serum 20ml")


def test_distinct_products_do_not_collide():
    assert retailer_match_key("COSRX", "Advanced Snail 96 Mucin Power Essence 100ml") != \
           retailer_match_key("COSRX", "Advanced Snail 92 All in One Cream 100ml")


def test_brand_normalization_folds_suffixes():
    assert retailer_match_key("Glow Recipe Inc.", "Watermelon Glow Toner 150ml") == \
           retailer_match_key("Glow Recipe", "Watermelon Glow Toner")


def test_leading_brand_prefix_in_title_stripped():
    # brand-official storefront prefixes the brand; retailer/canonical do not.
    assert retailer_match_key("COSRX", "COSRX Advanced Snail 96 Mucin Power Essence") == \
           retailer_match_key("COSRX", "Advanced Snail 96 Mucin Power Essence 100ml")
    # multi-word brand prefix
    assert retailer_match_key("Beauty of Joseon", "Beauty of Joseon Glow Serum 30ml") == \
           retailer_match_key("Beauty of Joseon", "Glow Serum")
    # brand appearing mid-title is NOT stripped (only a leading prefix)
    assert "cosrx" in retailer_match_key("COSRX", "Official COSRX Snail Cream")


def test_storefront_official_vendor_matches_plain_brand():
    # Shopify vendor "COSRX Official" must key the same as catalog "COSRX",
    # including the brand-prefix-in-title strip firing on both sides.
    assert retailer_match_key("COSRX Official", "COSRX Red Rice Inositol Pore Wash") == \
           retailer_match_key("COSRX", "Red Rice Inositol Pore Wash 150ml")


def test_descriptor_strip_does_not_corrupt_real_brands():
    # 'shop'/'beauty'/'store' are real brand words — never stripped.
    from services.pdp_matcher.retailer_match import match_brand
    assert match_brand("The Face Shop") == "the face shop"
    assert match_brand("Huda Beauty") == "huda beauty"
    assert match_brand("COSRX Official") == "cosrx"
    assert match_brand("Official") == "official"  # never strips to empty


def test_empty_when_brand_or_title_missing():
    assert retailer_match_key(None, "Some Toner") == ""
    assert retailer_match_key("COSRX", "") == ""
    assert retailer_match_key("COSRX", "   ") == ""


def test_all_promo_title_falls_back_to_full_title_no_collision():
    # 'Favorites Set' and 'Value Set' are both all-promo; must not collide.
    assert retailer_match_key("COSRX", "Favorites Set") != retailer_match_key("COSRX", "Value Set")


# --- index + lookup -----------------------------------------------------------

def test_index_matches_across_size_drift():
    ours = [
        {"product_key": "pk_mucin", "content_key": "ck1", "brand": "COSRX",
         "title": "Advanced Snail 96 Mucin Power Essence", "pdp_scope": "merchant_owned"},
        {"product_key": "pk_cream", "content_key": "ck2", "brand": "COSRX",
         "title": "Advanced Snail 92 All in One Cream", "pdp_scope": "merchant_owned"},
    ]
    idx = build_match_index(ours)
    hit = match_record(idx, "COSRX", "Advanced Snail 96 Mucin Power Essence 100ml")
    assert hit is not None and hit["product_key"] == "pk_mucin"
    assert match_record(idx, "COSRX", "Totally Unrelated Serum 30ml") is None


def test_index_prefers_canonical_scope_on_collision():
    ours = [
        {"product_key": "pk_owned", "content_key": "ck_o", "brand": "COSRX",
         "title": "Advanced Snail Hydrogel Eye Patch", "pdp_scope": "merchant_owned"},
        {"product_key": "pk_canon", "content_key": "ck_c", "brand": "COSRX",
         "title": "Advanced Snail Hydrogel Eye Patch", "pdp_scope": "multi_merchant_canonical"},
    ]
    idx = build_match_index(ours)
    hit = match_record(idx, "COSRX", "Advanced Snail Hydrogel Eye Patch 60ea")
    assert hit["product_key"] == "pk_canon"


# --- StyleKorean adapter pure functions --------------------------------------

def test_brand_url_filter_is_boundary_safe():
    sitemap = """<urlset>
      <url><loc>https://www.stylekorean.com/product/cosrx-advanced-snail-96-mucin-power-essence-100ml/1450854424</loc></url>
      <url><loc>https://www.stylekorean.com/product/cosrx-aloe-soothing-sun-cream-50ml/1459927938</loc></url>
      <url><loc>https://www.stylekorean.com/product/cosrxdupe-not-really-cosrx/999</loc></url>
      <url><loc>https://www.stylekorean.com/product/missha-4d-mascara/14309639764549</loc></url>
      <url><loc>https://www.stylekorean.com/brand/cosrx/1</loc></url>
    </urlset>"""
    urls = sk.brand_product_urls("cosrx", [sitemap])
    assert len(urls) == 2
    assert all("/product/cosrx-" in u for u in urls)
    assert not any("cosrxdupe" in u for u in urls)
    assert not any("/brand/" in u for u in urls)


def test_to_validated_record_shape_and_retailer_offer():
    crawled = {
        "url": "https://www.stylekorean.com/product/cosrx-advanced-snail-96-mucin-power-essence-100ml/1450854424",
        "brand": "COSRX", "title": "Advanced Snail 96 Mucin Power Essence 100ml",
        "description": "Snail mucin essence.", "image_url": "https://img/x.jpg",
        "price_raw": "17.50", "currency": "USD", "availability": "in_stock",
    }
    rec = sk.to_validated_record(crawled)
    assert rec["pdp"]["brand"] == "COSRX"
    assert rec["pdp"]["source_domain"] == "stylekorean.com"
    assert rec["pdp"]["gtin"] is None
    off = rec["offers"][0]
    assert off["offer_type"] == "retailer" and off["is_first_party"] is False
    assert off["merchant_id"] == "stylekorean_global"
    assert off["list_price"] == 17.50 and off["currency"] == "USD"


def test_to_validated_record_null_price_is_destination_only():
    rec = sk.to_validated_record({"url": "u", "brand": "COSRX", "title": "X 100ml", "price_raw": None})
    assert rec["offers"][0]["list_price"] is None
