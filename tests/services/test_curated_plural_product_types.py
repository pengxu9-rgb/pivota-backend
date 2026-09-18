"""Plural merchant product types must resolve -- and must not invent a category on the way.

Every CATEGORY_PATTERNS entry matches the SINGULAR noun, so a merchant filing products under
"Cleansers" / "Sheet Masks" / "Serums" resolved to nothing, and one unresolved row blocks its whole
curated cohort. Measured 2026-09-18 over every product of three US K-beauty Shopify hosts
(eyurs.com 434, ohlolly.com 510, sokoglam.com 567): eyurs resolved 204 before and 387 after;
ohlolly 456 -> 479; sokoglam 477 -> 478; and NO product that resolved before changed.

The titles below are the real ones from that crawl. Each negative case is a way the first draft
of this door would have lied: eyurs files "Pyunkang Yul Essence Toner" under "Cleansers"; the
toner pattern's "pad" turned "Cotton Pads" into a toner; the facial mask pattern turned a foot
peel into a facial mask.
"""
import pytest

from services import curated_brand_feed as feed

LEAF = feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE
FEED_DEFAULT = feed.CATEGORY_CONFIDENCE_FEED_DEFAULT


def resolve(product_type, title, flag_path="beauty"):
    return feed._resolve_category(product_type=product_type, title=title, flag_path=flag_path)


@pytest.mark.parametrize("ptype,title,want", [
    ("Sheet Masks", "Beauty of Joseon Centella Asiatica Calming Sheet Mask", "beauty/skincare/treat/mask"),
    ("Moisturizers", "Pyunkang Yul Nutrition Cream (100ml)", "beauty/skincare/moisturize/cream"),
    ("Serums", "Beauty of Joseon Glow Serum: Propolis + Niacinamide (30ml)", "beauty/skincare/treat/serum"),
    ("Cleansers", "Pyunkang Yul Deep Clear Cleansing Balm", "beauty/skincare/cleanse/cleanser"),
    ("Toners", "SOME BY MI AHA-BHA-PHA 30 Days Miracle Toner (150ml)", "beauty/skincare/tone/toner"),
    ("Oil Cleansers", "Beauty of Joseon Ginseng Cleansing Oil (210ml)", "beauty/skincare/cleanse/cleanser"),
    ("Lip Balms", "Phyto-Glow Lip Balm SPF 45", "beauty/makeup/lip/balm"),
])
def test_a_plural_merchant_type_resolves_to_its_singular_leaf(ptype, title, want):
    assert resolve(ptype, title) == (want, LEAF)


def test_a_title_that_says_nothing_does_not_stop_the_merchant_type():
    # The title names no leaf at all: the merchant's shelf is the only evidence, and it is enough.
    assert resolve("Serums", "Pyunkang Yul 100")[0] == "beauty/skincare/treat/serum"


def test_the_title_naming_a_different_leaf_refuses_rather_than_guessing():
    """eyurs.com files its Essence Toner under "Cleansers". A plural alias would have filed a
    toner as a cleanser with confidence; the door returns the honest unresolved answer instead,
    and it does NOT fall back to the title's own guess either."""
    assert resolve("Cleansers", "Pyunkang Yul Essence Toner (100ml)") == ("", FEED_DEFAULT)
    assert resolve("Masks", "NEOGEN Dermalogy Real Bakuchiol Firming Serum (30ml)") == ("", FEED_DEFAULT)


@pytest.mark.parametrize("ptype,title", [
    ("Cotton Pads", "Pyunkang Yul 1/3 Cotton Pads (160pcs)"),
    ("Foot Masks", "PUREDERM Shiny & Soft Foot Peeling Mask (1 Pair)"),
    ("Hand Masks", "Moisturizing Hand Mask"),
    ("Body Serums", "Glow Body Serum"),
])
def test_a_body_area_or_accessory_material_is_not_rescued_as_face_care(ptype, title):
    """Measured: "Cotton Pads" matched the toner pattern through "pad" and "Foot Masks" matched the
    facial mask. The door rescues face-care nouns; these name another shelf and stay unresolved."""
    path, confidence = resolve(ptype, title)
    assert path == "beauty"
    assert confidence == FEED_DEFAULT


def test_a_plural_the_patterns_still_cannot_classify_stays_unresolved():
    assert resolve("Accessories", "Makeup Pouch") == ("beauty", FEED_DEFAULT)


def test_a_generic_plural_shelf_is_not_a_product_class():
    # "lip treatments" is deliberately generic: only an explicit lip-oil title refines it.
    assert resolve("Lip Treatments", "Dr. Ceuracle Vegan Kombucha Tea Lip Balm") == ("beauty", FEED_DEFAULT)


def test_a_type_the_patterns_already_matched_keeps_its_existing_answer():
    """The door only runs for a type that matched NOTHING. "Hair Conditioners" already matches one
    pattern ("hair") through the pre-existing mapping, and singularising it would match two -- so
    it is neither rescued nor re-decided: it keeps exactly the answer it had before the door."""
    assert resolve("Hair Conditioners", "Repair Conditioner") == ("beauty/haircare/general", LEAF)


def test_a_singular_that_matches_several_leaves_is_not_rescued():
    # "Lips" -> "lip" names several lip leaves at once: one regex hit is required.
    assert resolve("Lips", "Dewy Gloss") == ("beauty", FEED_DEFAULT)


def test_singular_types_are_untouched_by_the_door():
    assert resolve("Cleanser", "Pyunkang Yul Essence Toner (100ml)")[0] == "beauty/skincare/cleanse/cleanser"


def test_an_explicit_leaf_flag_still_wins_over_a_rescued_plural():
    # The existing precedence: a caller who names a LEAF keeps it. The door must not bypass accept().
    assert resolve("Serums", "Glow Serum", flag_path="beauty/skincare/treat/mask") == (
        "beauty/skincare/treat/mask", FEED_DEFAULT)


@pytest.mark.parametrize("word,want", [
    ("masks", "mask"), ("sheet masks", "sheet mask"), ("accessories", "accessory"),
    ("brushes", "brush"), ("gloss", ""), ("serum", ""), ("gels", "gel"), ("lips", "lip"), ("", ""),
])
def test_singularising_touches_only_a_plural_head_noun(word, want):
    assert feed._singular_product_type(word) == want


@pytest.mark.parametrize("ptype,title,want", [
    ("Wash Off Mask", "Beauty of Joseon Red Bean Refreshing Pore Mask", "beauty/skincare/treat/mask"),
    ("Sleeping Pack", "Cosrx Ultimate Nourishing Rice Overnight Spa Mask", "beauty/skincare/treat/mask"),
    ("Sun Care", "Isntree Hyaluronic Acid Daily Sun Gel", "beauty/skincare/sun/sunscreen"),
    ("Suncream", "SOME BY MI Truecica Mineral Calming Tone-Up Suncream SPF50+", "beauty/skincare/sun/sunscreen"),
])
def test_the_four_measured_exact_types_map_to_their_one_class(ptype, title, want):
    """Each of these was all one class across every product carrying it at the three hosts.
    "Wash off mask" is otherwise AMBIGUOUS (cleanser "wash" + mask), which is why it needs naming."""
    assert resolve(ptype, title) == (want, LEAF)


def test_a_shelf_named_by_the_type_must_agree_with_the_leaf():
    """The face patterns key on the head noun, so "Lip Masks" found the facial sheet mask. A type
    that says "lip" is on the lip shelf; the leaf must be too. "Lip Balms" -- the one lip plural
    measured, at sokoglam.com -- already is, and keeps resolving."""
    assert resolve("Lip Masks", "Overnight Lip Mask") == ("beauty", FEED_DEFAULT)
    assert resolve("Lip Balms", "Phyto-Glow Lip Balm SPF 45")[0] == "beauty/makeup/lip/balm"
    assert resolve("Lip Oils", "Juicy Lip Oil")[0] == "beauty/makeup/lip/oil"


def test_a_singular_that_is_itself_a_generic_shelf_is_not_rescued():
    """"Lip Colors" is not on the generic list, but its singular "lip color" is: a generic shelf
    does not become a product class by losing an s."""
    assert resolve("Lip Colors", "Velvet Lip Color") == ("beauty", FEED_DEFAULT)


def test_a_singular_hitting_two_leaves_is_ambiguous_not_a_first_match(monkeypatch):
    """No head noun in today's patterns reaches this (measured over 25 common plural heads), but
    first-match-wins is not evidence -- the same PR #2158 rule the main mapping enforces. Pinned with
    an injected overlapping pattern so the guard is a tested rule rather than dead code."""
    import re
    from services import pdp_category_classifier as clf

    overlap = ("Overlap", "beauty/skincare/treat/serum", re.compile(r"\bgizmo\b", re.I))
    first = ("Gizmo", "beauty/skincare/treat/mask", re.compile(r"\bgizmo\b", re.I))
    monkeypatch.setattr(clf, "CATEGORY_PATTERNS", [first, overlap, *clf.CATEGORY_PATTERNS])
    assert resolve("Gizmos", "Plain Title") == ("beauty", FEED_DEFAULT)
