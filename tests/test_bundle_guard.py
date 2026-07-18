"""Tests for the HITL bundle-vs-base price guard.

Cases are the seven real mint_and_attach proposals from the 2026-07-18 review
queue (scripts/stylekorean_hitl.py emit output). The guard must fire on exactly
one — the Beauty of Joseon "Dynasty Cream" duo — and leave the six genuine
single-product mints alone.
"""

from services.retailer_ingest.bundle_guard import (
    bundle_marker,
    bundle_price_guard,
)


def _official(name, price, canonical_url):
    return {"pdp": {"product_name": name},
            "offers": [{"price": price, "canonical_url": canonical_url}]}


def _item(title, price):
    return {"title": title, "price": price, "currency": "USD"}


# --- the target: bundle official certified == base single --------------------

def test_dynasty_duo_flagged():
    # official is a $72 duo (slug -duo-f, title hides it) certified equal to a
    # $23.8 SK single -> bundle-vs-base mismatch.
    official = _official("Dynasty Cream 3.38 fl.oz.(100ml)", 72.0,
                         "https://beautyofjoseon.com/products/dynasty-cream-100ml-duo-f")
    fired, reason = bundle_price_guard(official, [_item("*size up* Dynasty Cream 100ml", 23.8)])
    assert fired is True
    assert "duo" in reason and "3.0x" in reason


# --- the six that must NOT fire ----------------------------------------------

def test_anua_marketing_bundle_tag_not_a_marker():
    # anua carries a marketing "☆BUNDLE" collection tag (not read here) and an 8x
    # price gap from single-mask vs box pack size — neither slug nor title marks a
    # bundle, so it must not fire (tags-alone / price-alone are false positives).
    official = _official("PDRN Hyaluronic Acid Capsule 100 Serum Mask", 28.8,
                         "https://anua.us/products/pdrn-hyaluronic-acid-capsule-100-serum-mask")
    official["pdp"]["tags"] = ["☆BUNDLE", "☆NEW"]
    items = [_item("PDRN ... Serum Mask", 3.6), _item("*4EA* PDRN ...", 12.8),
             _item("PDRN ... Serum Mask", 32.0)]
    assert bundle_price_guard(official, items)[0] is False


def test_bblab_in_product_count_close_price_not_flagged():
    # "30 packs"/"30 stick" is the retail unit, and the price gap is only 1.25x.
    official = _official("The Collagen Powder S Season 2, 2g x 30 stick", 31.99,
                         "https://bblab.shop/products/the-collagen-powder-s-season-2-30-packs")
    items = [_item("(Halal) The Collagen Powder S Plus", 25.6),
             _item("(Halal) Low Molecular Collagen & Powder", 36.8)]
    assert bundle_price_guard(official, items)[0] is False


def test_roundlab_no_marker_small_gap_not_flagged():
    for name, price, slug, item_t, item_p in [
        ("Birch Juice Moisturizing Peeling Cleansing Oil", 20.0,
         "https://roundlab.com/products/birch-juice-moisturizing-peeling-cleansing-oil",
         "Birch Juice Moisturizing Peeling Cleansing Oil", 17.5),
        ("Birch Moisturizing Hand Cream", 8.5,
         "https://roundlab.com/products/birch-moisturizing-hand-cream",
         "Birch Juice Hand Cream 30ml", 5.6),
    ]:
        assert bundle_price_guard(_official(name, price, slug), [_item(item_t, item_p)])[0] is False


def test_skin1004_no_marker_not_flagged():
    for name, price, slug, item_t, item_p in [
        ("Centella Air-Fit Suncream Plus", 16.2,
         "https://skin1004.com/products/centella-air-fit-suncream-plus",
         "Madagascar Centella Air-Fit Suncream", 14.0),
        ("[60% Off] Centella Ampoule 100ml", 8.8,
         "https://skin1004.com/products/60-off-centella-ampoule",
         "Madagascar Centella Ampoule 100ml", 11.0),
    ]:
        assert bundle_price_guard(_official(name, price, slug), [_item(item_t, item_p)])[0] is False


# --- marker detection + edge cases -------------------------------------------

def test_marker_requires_separator_boundary():
    # "duo" inside a word (e.g. a brand) is not a bundle marker.
    assert bundle_marker(_official("Duologi Radiance Serum", 30.0,
                                   "https://x.com/products/duologi-radiance-serum")) is None
    assert (bundle_marker(_official("Radiance Serum Duo", 30.0,
                                    "https://x.com/products/radiance-serum-duo")) or "").lower() == "duo"


def test_price_gap_alone_without_marker_never_fires():
    # 10x gap but no bundle token anywhere -> not a bundle mismatch (pack-size).
    official = _official("Vitamin C Serum", 50.0, "https://x.com/products/vitamin-c-serum")
    assert bundle_price_guard(official, [_item("Vitamin C Serum sample", 5.0)])[0] is False


def test_marker_but_sub_threshold_price_not_flagged():
    # explicit duo marker but only 1.4x -> below the 1.8x bundle-multiple bar.
    official = _official("Toner Duo", 14.0, "https://x.com/products/toner-duo")
    assert bundle_price_guard(official, [_item("Toner", 10.0)])[0] is False


def test_marker_with_no_price_flags_conservatively():
    official = {"pdp": {"product_name": "Cream Bundle"},
                "offers": [{"canonical_url": "https://x.com/products/cream-bundle"}]}
    fired, reason = bundle_price_guard(official, [_item("Cream", 20.0)])
    assert fired is True and "no official price" in reason


def test_numeric_pack_marker_flags_at_bundle_price():
    official = _official("Sheet Mask 5 Pack", 20.0, "https://x.com/products/sheet-mask-5-pack")
    fired, reason = bundle_price_guard(official, [_item("Sheet Mask", 5.0)])
    assert fired is True and "4.0x" in reason
