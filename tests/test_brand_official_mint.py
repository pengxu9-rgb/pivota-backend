"""Tests for the brand-official MINT lane's pure matching (no network/DB)."""

from services.retailer_ingest.brand_official import (
    dominant_brand,
    filter_net_new,
    predict_official_matches,
)


def _official(brand, title):
    # shape returned by curated_brand_feed.shopify_product_to_record
    return {"pdp": {"brand": brand, "product_name": title}, "offers": [{"price": 1.0}]}


def test_mint_sku_resolves_to_brand_official_across_size_drift():
    official = [
        _official("COSRX", "Advanced Snail 96 Mucin Power Essence"),
        _official("COSRX", "Low pH Good Morning Gel Cleanser"),
    ]
    mint = [
        {"brand": "COSRX", "title": "Advanced Snail 96 Mucin Power Essence 100ml"},
        {"brand": "COSRX", "title": "Low pH Good Morning Gel Cleanser Double Duo (150ml + 50ml)"},
    ]
    resolved, residue = predict_official_matches(mint, official)
    assert len(resolved) == 2 and residue == []
    assert resolved[0]["official"]["pdp"]["product_name"] == "Advanced Snail 96 Mucin Power Essence"


def test_retailer_exclusive_with_no_official_match_is_residue():
    official = [_official("COSRX", "Advanced Snail 96 Mucin Power Essence")]
    mint = [
        {"brand": "COSRX", "title": "Advanced Snail 96 Mucin Power Essence 100ml"},
        {"brand": "COSRX", "title": "StyleKorean Exclusive Mystery Box"},
    ]
    resolved, residue = predict_official_matches(mint, official)
    assert len(resolved) == 1
    assert len(residue) == 1 and residue[0]["title"] == "StyleKorean Exclusive Mystery Box"


def test_no_official_records_all_residue():
    mint = [{"brand": "COSRX", "title": "Anything 50ml"}]
    resolved, residue = predict_official_matches(mint, [])
    assert resolved == [] and len(residue) == 1


def test_filter_net_new_excludes_already_owned_across_drift():
    # our catalog uses plain brand + no size; cosrx.com uses "Official" + brand-in-title + size
    our_rows = [
        {"brand": "COSRX", "title": "Advanced Snail 96 Mucin Power Essence", "pdp_scope": "merchant_owned"},
    ]
    official = [
        _official("COSRX Official", "COSRX Advanced Snail 96 Mucin Power Essence 100ml"),  # we HAVE this
        _official("COSRX Official", "COSRX 5 PDRN NAD+ Multi Repair Cream"),               # NET-NEW
    ]
    net_new, already = filter_net_new(official, our_rows)
    assert len(already) == 1 and len(net_new) == 1
    assert "NAD+" in net_new[0]["pdp"]["product_name"]


def test_dominant_brand():
    items = [{"brand": "COSRX"}, {"brand": "COSRX"}, {"brand": "Cosrx"}, {"brand": None}]
    assert dominant_brand(items) == "COSRX"
    assert dominant_brand([]) is None
