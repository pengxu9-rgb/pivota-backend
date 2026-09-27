"""Brand-store categories for the Tarte / Stila / Tower 28 spelling re-runs (read 2026-09-27).

The held re-runs kept 290/435, 54/131 and 15/64 products: brow shelves ("Eyebrow", "Brows") name no
pattern, "Concealer Brush" names two, and Tower 28 leaves 45 of 64 types blank. Three narrow fixes,
each measured on every product of every host in the shelf table plus kissusa / impressbeauty /
falscara (11 hosts, lip and lash doors on and off): only these three stores change.
Titles are the stores' own unless marked.
"""
import pytest

from services import curated_brand_feed as feed

SHELF = feed.CATEGORY_CONFIDENCE_MEASURED_HOST_TYPE
HEAD = feed.CATEGORY_CONFIDENCE_MEASURED_HOST_TITLE
TITLE = feed.CATEGORY_CONFIDENCE_EXPLICIT_TITLE
BROW = "beauty/makeup/eye/brow"
BRUSH = "beauty/tools/brush"


def resolve(product_type, title, domain, flag_path="beauty"):
    return feed._resolve_category(product_type=product_type, title=title, flag_path=flag_path, domain=domain)


def unresolved(result):
    from services.category_path_aliases import resolve as leaf
    return not leaf(result[0])


# --- measured shelves -------------------------------------------------------------------------

@pytest.mark.parametrize("domain,ptype,title,want", [
    ("tartecosmetics.com", "Eyebrow", "busy gal BROWS tinted brow gel", BROW),
    ("tartecosmetics.com", "Eyebrow", "maneater™ emphasEYES™ high definition brow", BROW),
    ("tartecosmetics.com", "Eyebrow", "frameworker™ brow pomade", BROW),
    ("tartecosmetics.com", "Eyebrow", "maracuja juicy brow", BROW),
    ("tartecosmetics.com", "Lip Plump", "maracuja juicy lip plump", "beauty/makeup/lip/gloss"),
    ("tartecosmetics.com", "Lip Plump", "maracuja juicy shift", "beauty/makeup/lip/gloss"),
    ("tartecosmetics.com", "Face Tool", "quickie blending sponge", "beauty/tools/sponge"),
    ("tartecosmetics.com", "Face Tool", "set & go powder puff", "beauty/tools/sponge"),
    ("stilacosmetics.com", "Brows", "Stay All Day® Waterproof Brow Color", BROW),
    ("stilacosmetics.com", "Lip Care", "Heaven’s Dew™ Honey Glow Balm - Golden Sun", "beauty/makeup/lip/balm"),
])
def test_the_brand_store_shelves_resolve_their_products(domain, ptype, title, want):
    assert resolve(ptype, title, domain) == (want, SHELF)


def test_a_two_area_tint_on_a_lip_shelf_is_set_aside():
    """tarte files its "lip & cheek shift" (a lip AND cheek tint) on the Lip Plump shelf."""
    assert unresolved(resolve("Lip Plump", "lime-coded maracuja juicy lip & cheek shift", "tartecosmetics.com"))
    # the same area rule on the older lip shelf (constructed title; none on sokoglam today)
    assert unresolved(resolve("Lip Balms", "Lip & Cheek Balm", "sokoglam.com"))
    assert resolve("Lip Balms", "Phyto-Glow Lip Balm SPF 45", "sokoglam.com")[0] == "beauty/makeup/lip/balm"


def test_the_brow_word_is_exempt_only_on_a_brow_shelf():
    # On a face shelf a brow product is still another area (constructed).
    assert unresolved(resolve("Moisturizers", "Brow Growth Cream", "eyurs.com"))
    # ...and a brow shelf still refuses the other areas.
    assert unresolved(resolve("Eyebrow", "brow & body glow oil", "tartecosmetics.com"))


@pytest.mark.parametrize("domain", ["sephora.com", "", None, "eyurs.com"])
def test_the_brand_store_shelves_mean_nothing_elsewhere(domain):
    assert unresolved(resolve("Eyebrow", "busy gal BROWS tinted brow gel", domain))
    assert unresolved(resolve("Lip Plump", "maracuja juicy lip plump", domain))


# --- "<X> Brush" types ------------------------------------------------------------------------

@pytest.mark.parametrize("ptype,title", [
    ("Concealer Brush", "shape tape™ blur concealer brush"),
    ("Concealer Brush", "double-ended detail & buff concealer brush"),
    ("Eyeshadow Brush", "double-ended shadow & liner brush"),
    ("Eyeliner Brush", "eye definer line & smudge brush"),
    ("Foundation Brush", "the buffer™ brush"),
    ("Eyeshadow Brush, Cheek Brush", "glow pot eye & cheek brush"),
])
def test_a_brush_type_with_a_brush_title_is_a_brush(ptype, title):
    assert resolve(ptype, title, "tartecosmetics.com") == (BRUSH, TITLE)
    assert resolve(ptype, title, "unknown-store.com") == (BRUSH, TITLE)  # not host-scoped


@pytest.mark.parametrize("ptype,title", [
    ("Foundation Brush", "jumbo quickie blending sponge"),        # on tarte's brush shelf, not a brush
    ("Foundation Brush", "2-in-1 twist tool"),
    ("Concealer Brush", "face card never declines CC undereye & brush"),  # a formula sold WITH a brush
    ("Foundation Brush", "Serum Foundation with Brush"),                  # constructed
    ("Concealer Brush", "Brush-On Concealer"),                            # constructed
    ("Hair Brush", "Flex Gentle Brush"),                                  # sokoglam: a hairbrush
])
def test_a_brush_type_does_not_make_everything_on_it_a_brush(ptype, title):
    assert resolve(ptype, title, "tartecosmetics.com")[0] != BRUSH


# --- head-noun title rule on measured hosts ---------------------------------------------------

@pytest.mark.parametrize("domain,ptype,title,want", [
    ("www.tower28beauty.com", "", "Swipe Serum Foundation", "beauty/makeup/face/foundation"),
    ("tower28beauty.com", "Foundations & Concealers", "Swipe Serum Concealer®", "beauty/makeup/face/concealer"),
    ("tower28beauty.com", "", "BeachPlease Cream Blush", "beauty/makeup/face/blush"),
    ("tower28beauty.com", "", "Sculptino® Cream Contour", "beauty/makeup/face/bronzer"),
    ("tower28beauty.com", "", "GetSet® Matte Powder Blush", "beauty/makeup/face/blush"),
    ("tower28beauty.com", "", "MakeWaves® Mascara", "beauty/makeup/eye/mascara"),
    ("tower28beauty.com", "", "SOS Recovery Cream", "beauty/skincare/moisturize/cream"),
    ("tower28beauty.com", "", "SOS Gel Cleanser", "beauty/skincare/cleanse/cleanser"),
    ("stilacosmetics.com", "Sale", "Stay All Day® Waterproof Liquid Eye Liner - Micro Tip", "beauty/makeup/eye/eyeliner"),
    ("stilacosmetics.com", "Eye Products", "HUGE™ Extreme Lash Mascara", "beauty/makeup/eye/mascara"),
    ("stilacosmetics.com", "new", "All About The Blur Blurring & Smoothing Primer", "beauty/makeup/face/primer"),
])
def test_the_head_noun_names_the_product_on_a_measured_title_host(domain, ptype, title, want):
    assert resolve(ptype, title, domain) == (want, HEAD)


def test_the_body_area_rule_still_applies_after_the_head_noun():
    assert resolve("", "SOS Rescue + Relief Body Wash Treatment", "tower28beauty.com")[0] == "beauty/body/care"


@pytest.mark.parametrize("ptype,title", [
    ("", "SOS FaceGuard™ SPF 30 - Sample"),               # a sample
    ("", "MakeWaves® Mascara Set"),                       # a set
    ("", "JELL-O® X TOWER 28 Lip Jelly Lip Gloss Gift Set"),
    ("", "The Cheeky Duo"),
    ("", "Keep-It Together Keychain"),                    # merch
    ("", "$10 Gift Card"),
    ("", "SOS Rescue Spray"),                             # names no leaf
    ("", "MakeWaves Mascara Trio"),                       # constructed: a set word no gift-set pattern reads
    ("", "MakeWaves Mascara Holiday Edition"),            # constructed: the gift-set pattern, no set word
    ("Hidden", "Free Mini HUGE Extreme Lash Mascara"),    # a type nobody listed
])
def test_the_head_noun_rule_refuses_sets_samples_merch_and_unlisted_types(ptype, title):
    assert unresolved(resolve(ptype, title, "tower28beauty.com" if ptype != "Hidden" else "stilacosmetics.com"))


def test_two_leaves_ending_together_are_a_tie_not_a_head():
    # constructed: "Lip Serum" is the lip balm (lip care) AND the serum pattern, both ending at "Serum"
    assert feed._measured_host_title_leaf(domain="tower28beauty.com", product_type="",
                                          title="ShineOn Lip Serum") is None


@pytest.mark.parametrize("domain", ["sephora.com", "tartecosmetics.com", "", None])
def test_the_head_noun_is_no_evidence_on_any_other_host(domain):
    assert unresolved(resolve("", "BeachPlease Cream Blush", domain))
    assert unresolved(resolve("", "Swipe Serum Foundation", domain))


def test_a_refusal_and_a_resolved_row_are_never_touched():
    # Stila's swapped type: a "Blush" typed eyeliner is refused, and stays refused.
    assert resolve("Blush", "Stay All Day® Chroma-Flash Liquid Eye Liner", "stilacosmetics.com")[0] == ""
    # A merchant type that names its class resolves at merchant confidence, not the head-noun rule's.
    assert resolve("Mascara", "MakeWaves® Mascara", "tower28beauty.com")[1] == feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE


def test_the_new_confidences_are_no_other_writers_value():
    from scripts.backfill_pdp_category_path import CONFIDENCE_REGEX_BACKFILL
    from services.pdp_category_classifier import CATEGORY_CONFIDENCE_VARIANT
    others = {CONFIDENCE_REGEX_BACKFILL, CATEGORY_CONFIDENCE_VARIANT, feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE,
              feed.CATEGORY_CONFIDENCE_EXPLICIT_TITLE, feed.CATEGORY_CONFIDENCE_FEED_DEFAULT, SHELF,
              feed.CATEGORY_CONFIDENCE_LIP_TITLE, feed.CATEGORY_CONFIDENCE_LASH_NAIL_TITLE}
    assert HEAD not in others
