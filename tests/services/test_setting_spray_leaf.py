"""Setting spray is a leaf: beauty/makeup/face/setting-spray (2026-10-10).

It was a declared gap. Measured that day: ~72 drain products on 28 domains left out as
category_unresolved (types "Setting Spray", "Setting Sprays", "Makeup Fixer", "Makeup Setting Fixer"),
and 6 live setting mists filed on the toner leaf through its bare "mist". Titles are the stores' own
unless marked.
"""
import pytest

from services import curated_brand_feed as feed
from services.category_path_aliases import ALIASES, TAXONOMY_GAPS, TAXONOMY_LEAVES, resolve as leaf
from services.pdp_category_classifier import (
    SETTING_SPRAY_QUERY_ROUTE_FLAG,
    category_path_prefix_for_query,
    classify,
    resolve_path_from_row,
)

LEAF = "beauty/makeup/face/setting-spray"
TONER = "beauty/skincare/tone/toner"
POWDER = "beauty/makeup/face/powder"


def resolve(product_type, title, domain=None):
    return feed._resolve_category(product_type=product_type, title=title, flag_path="beauty", domain=domain)


def test_it_is_a_leaf_and_the_old_gap_spelling_aliases_to_it():
    assert LEAF in TAXONOMY_LEAVES
    assert "beauty/makeup/setting-spray" not in TAXONOMY_GAPS
    assert ALIASES["beauty/makeup/setting-spray"] == LEAF
    assert leaf("beauty/makeup/setting-spray") == LEAF


@pytest.mark.parametrize("title", [
    "Makeup Fixing Mist",                                  # pixibeauty, was on tone/toner
    "You Mist Makeup-Extending Setting Spray",             # fentybeauty, was on tone/toner
    "RMS Beauty Radiance Lock Setting Mist 100ml",
    "Jill Stuart Crystal Glow & Fix Mist",
    "Mist & Fix Matte Spray",
    "Fix+ Stay Over 24HR Setting Spray",
    "Mask Fit Waterproof Setting Spray",                   # was on treat/mask via "Mask"
    "3CE Makeup Fixer Mist",
    "Hera Make Up Fixer 80ml",
    "espoir Fresh Setting Fixer 100ml",
])
def test_a_setting_spray_title_names_the_leaf_not_toner(title):
    assert classify(title)[1] == LEAF


@pytest.mark.parametrize("title,want", [
    ("Hydrating Face Mist", TONER),
    ("Rose Water Mist", TONER),
    ("Setting Powder", POWDER),
    ("Makeup Setting Puffs", None),                        # constructed: puffs, not a spray
    ("Volumizing Finishing Spray", None),                  # a hair spray
    ("Finishing Hairspray", None),
    ("Hair Fixing Spray", "beauty/haircare/general"),
])
def test_the_leaf_does_not_take_mists_powders_puffs_or_hair_sprays(title, want):
    hit = classify(title)
    assert (hit[1] if hit else None) == want


@pytest.mark.parametrize("ptype,title", [
    ("Setting Spray", "Life Lock™ hydrating setting spray"),             # tartecosmetics.com
    ("Setting Sprays", "Airbrush Flawless Setting Spray"),               # bluemercury.com
    ("SETTING SPRAY", "NYX Makeup Setting Spray 2.03oz"),                # ikatehouse.com
    ("Makeup Fixer", "Espoir Fresh Setting Fixer 100ml"),                # koolseoul.com
    ("Makeup Setting Fixer", "3CE MAKEUP FIXER MIST 100ml"),             # nanasbeautyholic.com
    ("Setting Mist", "Continuous Setting Mist"),                         # constructed type
    ("Mist & Fix", "Mist & Fix Spray"),                                  # constructed type
])
def test_a_setting_spray_shelf_resolves(ptype, title):
    assert resolve(ptype, title)[0] == LEAF


@pytest.mark.parametrize("title", [
    "[ETUDE HOUSE] Dr. Mascara Fixer 6ml",
    "[ETUDE HOUSE] Bare Edge Brow Fixer",
    "[ETUDE HOUSE] Pang Pang Haircara Fixer 10ml",
    "[A’PIEU] Pro Curling The Black Fixer Mascara",
])
def test_a_lash_brow_or_hair_fixer_on_a_setting_spray_shelf_is_refused(title):
    # holiholic.com shelves these under "Setting Spray"
    assert not leaf(resolve("Setting Spray", title)[0])


@pytest.mark.parametrize("ptype,title,want", [
    ("makeup, face, setting spray & powder", "Slip Tint Undetectable Baked Setting Powder", POWDER),
    ("makeup, face, setting spray & powder", "Vital Pressed Skincare Powder", POWDER),
    ("Setting Spray and Powder", "Ruby Kisses RFS Never Touch Up Setting Spray 1.69 oz", LEAF),
    # a title naming a third leaf is not plainly a setting spray: the shelf's old answer (constructed)
    ("Setting Spray and Powder", "Primer & Setting Spray", POWDER),
    ("Setting Spray and Powder", "Fragrance Setting Mist", POWDER),
    ("Setting Spray and Powder", "Cleanser & Setting Spray", POWDER),
    # names the powder and a gift set ("Set N"): a powder title keeps the shelf's powder
    ("Setting Spray and Powder", "Ruby Kisses HD Set N Forget Setting Powder -RRSP .4 oz", POWDER),
    ("Setting Spray and Powder", "Setting Spray & Powder Duo", POWDER),               # constructed: both
])
def test_a_combined_spray_and_powder_shelf_moves_only_spray_titles(ptype, title, want):
    got = resolve(ptype, title)[0]
    assert (leaf(got) or None) == want


@pytest.mark.parametrize("ptype", ["Setting Spray and Powder", "makeup, face, setting spray & powder"])
@pytest.mark.parametrize("title", [
    "Hydrating Face Mist", "Makeup Setting Puffs", "Prep & Set Mist", "Make Keep Mist EX", "Fix+ Original",
    "메이크업 픽서", "Translucent Powder", "Lash Fixing Mist",                       # constructed
])
def test_a_combined_shelf_title_naming_no_spray_stays_as_it_was_filed(ptype, title):
    # before the leaf, the type read as powder alone: same leaf, same confidence
    assert resolve(ptype, title) == ("beauty/makeup/face/powder", feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE)


# Constructed shelves that name two products in the plural: before the leaf they were two matches (the
# #2158 ambiguity, unresolved), and they stay so -- as does the singular spelling, which main filed on
# tone/toner through its bare "mist".
_TWO_PRODUCT_SHELVES = [
    "Face Mists & Setting Sprays", "Mists & Setting Sprays", "Setting Sprays & Mists", "Primers & Setting Sprays",
    "Face Mist & Setting Spray",
    # any case, and -es plurals (narrow review of #2547)
    "FACE MISTS & SETTING SPRAYS", "PRIMERS & SETTING SPRAYS", "Face MISTS & Setting SPRAYS",
    "Blushes & Setting Sprays", "Brushes & Setting Sprays", "Lip Glosses & Setting Sprays",
]


@pytest.mark.parametrize("ptype", _TWO_PRODUCT_SHELVES)
@pytest.mark.parametrize("title", ["Rose Water Facial Mist", "All Nighter Setting Spray", "Blurring Primer"])
def test_a_two_product_shelf_naming_a_setting_spray_is_ambiguous(ptype, title):
    assert not leaf(resolve(ptype, title)[0])


@pytest.mark.parametrize("ptype,title,want", [
    ("Setting Spray and Powder", "Translucent Powder", POWDER),
    ("Setting Spray and Powder", "Never Touch Up Setting Spray", LEAF),
    ("Face Mist & Setting Spray", "Rose Water Facial Mist", TONER),
    ("Face Mists & Setting Sprays", "Blurring Primer", "beauty/makeup/face/primer"),
    ("Setting Sprays", "Translucent Powder", LEAF),                     # one product: the type decides
])
def test_the_backfill_lets_the_title_decide_on_a_two_product_type(ptype, title, want):
    hit = resolve_path_from_row(category=None, product_type=ptype, title=title)
    assert (hit[1] if hit else None) == want


@pytest.mark.parametrize("ptype", _TWO_PRODUCT_SHELVES)
@pytest.mark.parametrize("title,want", [
    ("Rose Water Facial Mist", TONER), ("Cheek Blush", "beauty/makeup/face/blush"), ("Translucent Powder", POWDER),
])
def test_the_backfill_reads_every_two_product_shelf_by_its_title(ptype, title, want):
    assert resolve_path_from_row(category=None, product_type=ptype, title=title)[1] == want


@pytest.mark.parametrize("title,want", [
    ("All Nighter Setting Spray", (LEAF, feed.CATEGORY_CONFIDENCE_EXPLICIT_TITLE)),
    ("Translucent Powder", (POWDER, feed.CATEGORY_CONFIDENCE_EXPLICIT_TITLE)),
    # titles naming neither: the plural shelf read as nothing before the leaf, and still does
    ("Rose Water Facial Mist", ("beauty", feed.CATEGORY_CONFIDENCE_FEED_DEFAULT)),
    ("Blurring Primer", ("beauty", feed.CATEGORY_CONFIDENCE_FEED_DEFAULT)),
    ("Foundation Brush", ("beauty", feed.CATEGORY_CONFIDENCE_FEED_DEFAULT)),
])
def test_the_plural_combined_shelf_moves_only_spray_and_powder_titles(title, want):
    assert resolve("Setting Sprays & Powders", title) == want


@pytest.mark.parametrize("ptype", ["Setting Sprays", "Setting Spray", "SETTING SPRAY"])
def test_a_one_product_plural_shelf_is_still_one_product(ptype):
    assert resolve(ptype, "All Nighter Setting Spray")[0] == LEAF


@pytest.mark.parametrize("title", ["Foundation Brush", "Kabuki Brush"])
def test_a_brush_on_a_setting_spray_shelf_is_a_brush(title):
    # before the leaf the type read as nothing, and the tool rescue filed the brush
    assert resolve("Setting Spray", title) == ("beauty/tools/brush", feed.CATEGORY_CONFIDENCE_EXPLICIT_TITLE)
    # ...in the backfill's resolver too
    assert resolve_path_from_row(category=None, product_type="Setting Spray", title=title)[1] == "beauty/tools/brush"


def test_the_classifiers_brush_rescue_reads_the_feeds_own_tool_words():
    from services import pdp_category_classifier as classifier

    assert classifier._BRUSH_TITLE.pattern == feed._TOOL_NOUN_SUFFIX.pattern
    assert classifier._BRUSH_TITLE_FORMULA_CONTEXT.pattern == feed._TOOL_FORMULA_CONTEXT.pattern


def test_the_combined_shelf_rule_reads_only_that_pair():
    # any other two-product type stays the #2158 ambiguity it was
    assert not leaf(resolve("Primer & Foundation", "Blurring Primer")[0])


# The review of #2547's titles that the bare phrases released onto face makeup (all constructed), and
# what the classifier answered before the leaf existed -- the answer they keep.
@pytest.mark.parametrize("title,want", [
    ("Lash Fixing Mist", None),
    ("Brow Setting Spray", None),
    ("Eyebrow Fixing Spray", None),
    ("Curl Setting Spray", None),
    ("Setting Spray for Curls", None),
    ("Lace Wig Fixing Spray", None),
    ("Edge Fixing Spray", None),
    ("Style Fixing Spray", None),
    ("Wrap Setting Spray", "fashion/accessories/scarf"),
    ("Body Setting Spray", None),
    ("Tattoo Setting Spray", None),
    ("Nail Setting Spray", None),
    ("Beard Setting Spray", None),
    ("Lace Melt Setting Spray", None),
    ("Hand Setting Spray", None),
    ("Leg Makeup Setting Spray", None),
    ("Tan Fixing Spray", None),
    ("Self Tan Setting Mist", "beauty/body/tanning"),
    ("Scalp Setting Spray", "beauty/haircare/general"),
    ("Cuticle Fixing Mist", None),
    ("Hair Fixer", "beauty/haircare/general"),
    # a bare "fix"/"fixer" spray names no setting spray
    ("Quick Fix Spray", None),
    ("Stain Fix Spray", None),
    ("Fixer Spray", None),
])
def test_a_setting_phrase_for_another_area_is_not_a_face_setting_spray(title, want):
    for hit in (classify(title), resolve_path_from_row(category=None, product_type=None, title=title)):
        assert (hit[1] if hit else None) == want


@pytest.mark.parametrize("title", ["Face & Body Setting Spray", "Setting Spray for Face and Body"])
def test_a_title_naming_the_face_keeps_the_leaf_despite_the_body(title):
    assert classify(title)[1] == LEAF


@pytest.mark.parametrize("title", [
    "Lace Wig Fixing Spray", "Brow Setting Spray", "Body Mist", "Hair Fixer", "Hairspray Setting Mist",
    "Tattoo Setting Spray", "Cuticle Oil", "Scalp Serum", "Shea Handcream", "Self Tanning Mist",
])
def test_the_typed_shelf_refuses_the_same_titles_as_the_classifier(title):
    # one rule (setting_spray_names_another_area) for the shelf and the title
    assert not leaf(resolve("Setting Spray", title)[0])


@pytest.mark.parametrize("query", ["setting spray", "setting mist", "makeup fixer", "fixing spray", "mist and fix"])
def test_with_the_flag_on_a_setting_spray_query_browses_face_makeup(query, monkeypatch):
    monkeypatch.setenv(SETTING_SPRAY_QUERY_ROUTE_FLAG, "on")
    assert category_path_prefix_for_query(query) == "beauty/makeup/face/"


@pytest.mark.parametrize("query", ["setting spray spf", "setting spray spf 30", "sunscreen setting spray"])
def test_with_the_flag_on_a_setting_spray_spf_query_is_a_sunscreen_ask(query, monkeypatch):
    monkeypatch.setenv(SETTING_SPRAY_QUERY_ROUTE_FLAG, "on")
    assert category_path_prefix_for_query(query) == "beauty/skincare/sun/"


# category_path_prefix_for_query on origin/main 46d9257f8, before the leaf (captured 2026-10-30).
_QUERY_PREFIX_ON_MAIN = {
    "blush": "beauty/makeup/face/",
    "body setting spray": None,
    "body spray": "beauty/fragrance/",
    "bronzer": "beauty/makeup/face/",
    "brow setting spray": None,
    "cleanser": "beauty/skincare/cleanse/",
    "concealer": "beauty/makeup/face/",
    "dewy setting spray": None,
    "eyeliner": "beauty/makeup/eye/",
    "eyeshadow": "beauty/makeup/eye/",
    "face mist": "beauty/skincare/tone/",
    "facial mist": "beauty/skincare/tone/",
    "false lashes": "beauty/makeup/eye/",
    "finishing spray": None,
    "fix spray": None,
    "fixer": None,
    "fixing spray": None,
    "foundation": "beauty/makeup/face/",
    "glow serum setting spray": "beauty/skincare/treat/",
    "hair fixing spray": "beauty/haircare/",
    "hair spray": "beauty/haircare/",
    "highlighter": "beauty/makeup/face/",
    "lash fixing mist": "beauty/skincare/tone/",
    "lip balm": "beauty/makeup/lip/",
    "lip gloss": "beauty/makeup/lip/",
    "lipstick": "beauty/makeup/lip/",
    "make up fixer": None,
    "makeup fixer": None,
    "makeup fixing mist": "beauty/skincare/tone/",
    "makeup setting spray": None,
    "mascara": "beauty/makeup/eye/",
    "matte setting spray": None,
    "mist": "beauty/skincare/tone/",
    "mist & fix": "beauty/skincare/tone/",
    "mist and fix": "beauty/skincare/tone/",
    "moisturizer": "beauty/skincare/moisturize/",
    "nail polish": "beauty/makeup/nails/",
    "perfume": "beauty/fragrance/",
    "primer": "beauty/makeup/face/",
    "quick fix spray": None,
    "rose water mist": "beauty/skincare/tone/",
    "serum": "beauty/skincare/treat/",
    "setting fixer": None,
    "setting mist": "beauty/skincare/tone/",
    "setting powder": "beauty/makeup/face/",
    "setting spray": None,
    "setting spray spf": None,
    "setting spray spf 30": "beauty/skincare/sun/",
    "shampoo": "beauty/haircare/",
    "skin booster": "beauty/skincare/tone/",
    "sunscreen": "beauty/skincare/sun/",
    "toner": "beauty/skincare/tone/",
    "toner pad": "beauty/skincare/tone/",
    "tonic": "beauty/skincare/tone/",
}


@pytest.mark.parametrize("value", [None, "", "off", "false", "0", "no"])
def test_with_the_flag_off_query_routing_is_exactly_main(value, monkeypatch):
    if value is None:
        monkeypatch.delenv(SETTING_SPRAY_QUERY_ROUTE_FLAG, raising=False)
    else:
        monkeypatch.setenv(SETTING_SPRAY_QUERY_ROUTE_FLAG, value)
    assert {q: category_path_prefix_for_query(q) for q in _QUERY_PREFIX_ON_MAIN} == _QUERY_PREFIX_ON_MAIN
