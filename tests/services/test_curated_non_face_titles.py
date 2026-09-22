"""A product that names a non-face body area never gets a FACE skincare leaf.

The merchant product_type door keeps only the noun a pattern recognises and drops its qualifier:
ohlolly's type "Hand Cream" became the face-cream leaf, eyurs' "Eye Lash Serum" the face-serum
leaf, and ohlolly files its body and hand washes under the type "Cleanser". Seen in the
2026-09-22 wave-2 dry runs; merchant types below are the ones those hosts serve.

No-regression: the rule changes an answer only where that answer is a face skincare leaf AND the
title/type names a non-face area. Measured 2026-09-22 over every product on eyurs.com, ohlolly.com
and sokoglam.com under five flags (7,535 resolutions): every changed row names such an area; zero
changes on any other row.
"""
import pytest

from services import curated_brand_feed as feed
from services.category_path_aliases import resolve as alias_resolve

BODY = "beauty/body/care"
HAIR = "beauty/haircare/general"


def resolve(product_type, title, domain="ohlolly.com", flag_path="beauty"):
    return feed._resolve_category(product_type=product_type, title=title, flag_path=flag_path, domain=domain)


def unguarded(product_type, title, domain="ohlolly.com", flag_path="beauty"):
    return feed._resolve_category_unguarded(product_type=product_type, title=title, flag_path=flag_path, domain=domain)


# The six rows from the dry run, with the merchant types their hosts actually serve.
@pytest.mark.parametrize("domain,ptype,title,was,want", [
    ("ohlolly.com", "Cleanser", "Pyunkang Yul Body Wash Dancheong", "beauty/skincare/cleanse/cleanser", BODY),
    ("ohlolly.com", "Cleanser", "Pyunkang Yul Hand Wash Dancheong", "beauty/skincare/cleanse/cleanser", BODY),
    ("ohlolly.com", "Moisturizer", "Pyunkang Yul Body Lotion Dancheong", "beauty/skincare/moisturize/cream", BODY),
    ("ohlolly.com", "Hand Cream", "Pyunkang Yul Hand Cream Dancheong", "beauty/skincare/moisturize/cream", BODY),
    ("ohlolly.com", "Hand Cream", "Melixir Vegan Hand Cream Untamed Nature", "beauty/skincare/moisturize/cream", BODY),
    # No lash-care leaf exists: unresolved, never a face serum.
    ("eyurs.com", "Eye Lash Serum", "ETUDE My Lash Serum 9g", "beauty/skincare/treat/serum", ""),
])
def test_the_dry_run_rows_leave_the_face_shelf(domain, ptype, title, was, want):
    assert unguarded(ptype, title, domain)[0] == was  # the defect, reproduced
    assert resolve(ptype, title, domain)[0] == want


def test_a_remapped_leaf_does_not_claim_the_merchant_type_confidence():
    path, confidence = resolve("Hand Cream", "Pyunkang Yul Hand Cream Dancheong")
    assert path == BODY and confidence == feed.CATEGORY_CONFIDENCE_EXPLICIT_TITLE


def test_an_unresolved_row_carries_the_feed_default_confidence():
    assert resolve("Eye Lash Serum", "ETUDE My Lash Serum 9g", "eyurs.com") == (
        "", feed.CATEGORY_CONFIDENCE_FEED_DEFAULT)


@pytest.mark.parametrize("ptype,title,want", [
    ("Moisturizer", "Bifi Blanche Microbiome Foot Cream", BODY),
    ("Moisturizer", "Silky Feet Cream", BODY),
    ("Moisturizer", "Shea Handcream", BODY),
    ("Moisturizer", "Softening Hands Cream", BODY),
    ("Cleanser", "Tiela Perfume Therapy Body Scrub Sunset", BODY),
    ("Serum", "Rosemary Scalp Serum", HAIR),
    ("Serum", "Hair Growth Serum", HAIR),
    ("Serum", "Eyelash Growth Serum", ""),
    ("Serum", "Eye Lash Serum", ""),
    ("Serum", "Eyebrow Serum", ""),
    ("Serum", "Brow Growth Serum", ""),
    ("Serum", "Nail Strengthening Serum", ""),
    ("Serum", "Cuticle Serum", ""),
    ("Moisturizer", "Beard Cream", ""),
    # A hand-and-nail cream is a hand cream.
    ("Hand Cream", "haruharu wonder Black Bamboo Nourishing Calming Hand & Nail Cream", BODY),
    # Two areas on two different shelves: no single honest leaf.
    ("Cleanser", "2-in-1 Hair & Body Wash", ""),
])
def test_every_non_face_area(ptype, title, want):
    assert unguarded(ptype, title)[0].startswith("beauty/skincare/")
    assert resolve(ptype, title)[0] == want


# REFUSING EXAMPLES: face products whose titles contain an area word's letters, or a brand, or
# that name the face as well. Each must resolve EXACTLY as before.
@pytest.mark.parametrize("ptype,title", [
    ("Moisturizer", "Handmade Cream"),
    ("Moisturizer", "Behind the Ear Cream"),
    ("Moisturizer", "Secondhand Glow Cream"),
    ("Serum", "Splash Serum"),
    ("Toner", "Splash Mist"),
    ("Toner", "Brown Rice Toner"),
    ("Moisturizer", "Full-Bodied Rich Cream"),
    ("Moisturizer", "Somebody's Favourite Cream"),
    ("Moisturizer", "Hairline-Safe Cream"),
    ("Moisturizer", "Blushing Cream"),
    ("Cleanser", "Nailed It Cleansing Balm"),
    ("Moisturizer", "The Body Shop Vitamin C Glow Cream"),
    ("Moisturizer", "Face & Body Lotion"),
    ("Cleanser", "Facial and Body Wash"),
    ("Eye Cream", "Aippo Expert Firming Eye & Neck Cream"),
    ("Moisturizer", "LAGOM Collagen Lifting Neck Cream"),
    ("Serum", "Crows Feet Serum"),
    ("Serum", "Crow Feet Smoothing Serum"),
])
def test_face_titles_are_untouched(ptype, title):
    assert resolve(ptype, title) == unguarded(ptype, title)
    assert resolve(ptype, title)[0].startswith("beauty/skincare/")


@pytest.mark.parametrize("ptype,title", [
    ("Sunscreen", "Body Sunscreen SPF 50"),     # sun/sunscreen names no body area
    ("Shampoo", "Scalp Shampoo"),               # already a hair leaf
    ("Body Care", "Hand Care Balm"),            # already body care
    ("Lip Balm", "Lip & Body Balm"),            # a makeup leaf, not a face skincare leaf
    ("Hand Soap", "Foaming Hand Soap"),         # unresolved before, unresolved after
])
def test_a_non_face_leaf_or_unresolved_answer_is_never_touched(ptype, title):
    assert resolve(ptype, title) == unguarded(ptype, title)


def test_a_callers_face_leaf_is_refused_too():
    """"Never" includes a caller's flag: the feed default is a guess about the whole cohort."""
    flag = "beauty/skincare/moisturize/cream"
    assert unguarded("", "Silky Body Butter", flag_path=flag)[0] == flag
    assert resolve("", "Silky Body Butter", flag_path=flag)[0] == BODY
    assert resolve("", "Barrier Cream", flag_path=flag)[0] == flag


def test_an_off_taxonomy_face_spelling_is_caught_through_the_alias():
    # A caller path the alias table folds into a face leaf is still a face leaf.
    assert resolve("", "Body Lotion", flag_path="beauty/skincare/moisturizers")[0] == BODY


def test_the_measured_shelf_guard_is_unchanged():
    # #2211's guard still refuses the shelf for a non-face title; the evidence answer stands.
    assert feed._measured_host_type_leaf(domain="eyurs.com", product_type="Moisturizers",
                                         title="Derma:B Daily Moisture Body Lotion") is None


def test_the_feed_mapper_writes_the_body_leaf():
    rec = feed.shopify_product_to_record({
        "title": "Pyunkang Yul Hand Wash Dancheong", "product_type": "Cleanser", "handle": "hw",
        "vendor": "Pyunkang Yul", "variants": [{"price": "12", "available": True}],
        "images": [{"src": "https://ohlolly.com/i.jpg"}],
    }, domain="ohlolly.com", category_path="beauty")
    assert rec["pdp"]["category_path"] == BODY


def test_the_repair_planner_agrees():
    assert feed.product_category_path(title="Pyunkang Yul Body Lotion Dancheong", product_type="Moisturizer",
                                      fallback="beauty", domain="ohlolly.com") == BODY


def test_the_shared_alias_no_longer_files_hand_and_nail_as_face_cream():
    assert alias_resolve("beauty/skincare/hand/nail") == BODY


def test_the_merchant_type_alone_can_name_the_area():
    # The qualifier the type door drops ("Hand" in "Hand Cream") is evidence even when the
    # marketing title omits it.
    assert unguarded("Hand Cream", "Vegan Cream Untamed Nature")[0] == "beauty/skincare/moisturize/cream"
    assert resolve("Hand Cream", "Vegan Cream Untamed Nature")[0] == BODY


@pytest.mark.parametrize("title", [
    # Live in prod on beauty/skincare/treat/treatment (reviewed_ext_seed_mirror), 2026-09-22: an EYE serum.
    "Multi-Peptide Eye Serum for Wrinkles and Crow's Feet",
    "Crows Feet Serum",
])
def test_crows_feet_is_the_eye_area(title):
    assert feed._non_face_leaf("beauty/skincare/treat/treatment", title=title, product_type="Eye Treatment") is None
    # ...while a real foot product on the same leaf is still caught.
    assert feed._non_face_leaf("beauty/skincare/treat/treatment", title="Cracked Feet Treatment",
                               product_type="") == BODY
