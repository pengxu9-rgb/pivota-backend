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
        {"brand": "COSRX", "title": "Low pH Good Morning Gel Cleanser Special Set (150ml + 50ml)"},
        # a Double-Duo BUNDLE is not the base product — stays residue by design
        # ("double" is line identity, 2026-07-16 review)
        {"brand": "COSRX", "title": "Low pH Good Morning Gel Cleanser Double Duo (150ml+150ml)"},
    ]
    resolved, residue = predict_official_matches(mint, official)
    assert len(resolved) == 2
    assert len(residue) == 1 and "Double Duo" in residue[0]["title"]
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
    net_new, already, suspects = filter_net_new(official, our_rows)
    assert len(already) == 1 and len(net_new) == 1 and suspects == []
    assert "NAD+" in net_new[0]["pdp"]["product_name"]


def test_filter_net_new_flags_line_name_drift_as_suspect_not_mint():
    # SKIN1004 pilot: official prefixes the line name; our old rows don't.
    # Minting would duplicate the product — must be SUSPECT (propose-only).
    our_rows = [
        {"brand": "SKIN1004", "title": "Centella Ampoule", "pdp_scope": "merchant_owned"},
        {"brand": "COSRX", "title": "Propolis Synergy Toner", "pdp_scope": "merchant_owned"},
    ]
    official = [
        _official("SKIN1004", "Madagascar Centella Ampoule 100ml"),      # superset of ours -> suspect
        _official("COSRX", "Full Fit Propolis Synergy Toner"),           # superset of ours -> suspect
        _official("SKIN1004", "Tone Brightening Capsule Ampoule"),       # genuinely new -> mint
    ]
    net_new, already, suspects = filter_net_new(official, our_rows)
    assert len(suspects) == 2
    assert len(net_new) == 1 and "Brightening" in net_new[0]["pdp"]["product_name"]
    assert already == []


def test_filter_net_new_single_shared_token_is_not_suspect():
    # One overlapping token must not trigger the containment guard —
    # 'Centella Cream' vs 'Centella Toner' are different products.
    our_rows = [{"brand": "SKIN1004", "title": "Centella Cream", "pdp_scope": "merchant_owned"}]
    official = [_official("SKIN1004", "Centella Toner")]
    net_new, already, suspects = filter_net_new(official, our_rows)
    assert len(net_new) == 1 and suspects == [] and already == []


def test_dominant_brand():
    items = [{"brand": "COSRX"}, {"brand": "COSRX"}, {"brand": "Cosrx"}, {"brand": None}]
    assert dominant_brand(items) == "COSRX"
    assert dominant_brand([]) is None


# --- body_html → attribute_summary (draft-canonical promotion, 2026-07-17) ----
# Minted rows previously carried attribute_summary=product_type ("Toner"), so
# description landed < 50 chars, is_candidate_ready failed, and every
# brand-official canonical sat at pdp_lifecycle_stage='draft', trust-blocked.

def test_body_html_to_text_strips_tags_entities_and_caps():
    from services.curated_brand_feed import BODY_TEXT_MAX_LEN, body_html_to_text

    assert body_html_to_text("<p>Rich &amp; <b>gentle</b><br>toner</p>") == "Rich & gentle toner"
    assert body_html_to_text(None) == ""
    assert body_html_to_text("   <div>  </div> ") == ""
    assert len(body_html_to_text("x" * (BODY_TEXT_MAX_LEN + 500))) == BODY_TEXT_MAX_LEN


def test_body_html_to_text_drops_script_style_comment_blocks():
    """Page-builder body_html carries <style>/<script> blocks whose INNER text
    is code — it must never become a served 'brand description'."""
    from services.curated_brand_feed import BODY_TEXT_MAX_LEN, body_html_to_text

    html_in = ("<style>.x{color:red;font-size:12px}</style>"
               "<p>Real brand copy</p>"
               "<script type='text/javascript'>var a = 1;</script>"
               "<!-- internal note --> more copy")
    assert body_html_to_text(html_in) == "Real brand copy more copy"
    # cap lands on a word boundary, never mid-token
    long = " ".join(["hydrating"] * 400)
    out = body_html_to_text(f"<p>{long}</p>")
    assert len(out) <= BODY_TEXT_MAX_LEN and out.endswith("hydrating")


def test_record_prefers_body_copy_over_product_type():
    from services.curated_brand_feed import shopify_product_to_record

    product = {
        "title": "Snail Essence", "handle": "snail-essence", "vendor": "COSRX",
        "product_type": "Essence",
        "body_html": "<p>A lightweight essence with 96% snail secretion filtrate "
                     "to repair and soothe stressed skin.</p>",
        "variants": [{"price": "17.50", "available": True, "barcode": ""}],
        "images": [{"src": "https://cosrx.com/i.jpg"}], "tags": ["soothing"],
    }
    rec = shopify_product_to_record(product, domain="cosrx.com", category_path="beauty/skincare")
    summary = rec["pdp"]["attribute_summary"]
    assert summary.startswith("A lightweight essence")
    assert len(summary) >= 50  # clears is_candidate_ready's description floor


def test_record_falls_back_to_product_type_when_no_body():
    from services.curated_brand_feed import shopify_product_to_record

    product = {
        "title": "Snail Essence", "handle": "snail-essence", "vendor": "COSRX",
        "product_type": "Essence", "body_html": "",
        "variants": [{"price": "17.50", "available": True}],
        "images": [], "tags": [],
    }
    rec = shopify_product_to_record(product, domain="cosrx.com", category_path="beauty/skincare")
    assert rec["pdp"]["attribute_summary"] == "Essence"


def test_backfill_handle_from_url():
    from scripts.backfill_brand_official_descriptions import handle_from_url

    assert handle_from_url("https://www.cosrx.com/products/snail-essence") == ("cosrx.com", "snail-essence")
    assert handle_from_url("https://misshaus.com/products/time-revolution?variant=1") == ("misshaus.com", "time-revolution")
    assert handle_from_url("https://cosrx.com/pages/about") == ("cosrx.com", "")
    assert handle_from_url("") == ("", "")
