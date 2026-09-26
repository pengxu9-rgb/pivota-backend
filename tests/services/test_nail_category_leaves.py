"""Lash and nail products get leaves (Peng 2026-09-26).

Before this, `beauty/makeup/nails/*` were declared TAXONOMY_GAPS and false lashes had no leaf: such
a product fell to whatever other noun it carried ("Peel Off Nail Polish" -> exfoliant, a dip powder
-> face powder, "Top Coat" -> a fashion coat), and a curated crawl with only_resolved_category
dropped every lacquer. Each rule below is stated with REFUSING examples next to the accepting ones,
and the product-type rule is checked through the curated resolver's own counter.
"""
from __future__ import annotations

import pytest

from services.curated_brand_feed import _pattern_matches
from services.pdp_category_classifier import (
    category_path_prefix_for_query,
    classify,
    resolve_path_from_row,
)

POLISH = "beauty/makeup/nails/nail-polish"
REMOVER = "beauty/makeup/nails/nail-polish-remover"
CUTICLE = "beauty/makeup/nails/cuticle-oil"
PRESS_ON = "beauty/makeup/nails/press-on-nails"
LASHES = "beauty/makeup/eye/false-lashes"


def _path(text):
    hit = classify(text)
    return hit[1] if hit else None


@pytest.mark.parametrize("title,want", [
    ("OPI Nail Lacquer - Big Apple Red", POLISH),
    ("Essie Gel Couture Nail Polish", POLISH),
    ("OPI Infinite Shine Gel Polish", POLISH),
    ("essie nail color", POLISH),
    ("Sakura - Breathable Nail Polish", POLISH),
    ("Metallic Bronze Peel Off Nail Polish", POLISH),   # was exfoliant via "peel"/"polish"
    ("Nail Dip Powder Pink", POLISH),                    # was face powder
    ("Quick Dry Top Coat", POLISH),                      # was a fashion coat
    ("GELement No-Wipe Top Coat", POLISH),               # was a cleanser via "wipe"
    ("Ridge Filling Base Coat", POLISH),
    ("Chrome Nail Powder Silver", POLISH),
    ("Gel Nail Color", POLISH),                          # re-review: was press-on via "gel nails"
    ("DND Gel Nail Color #123", POLISH),
    ("Soak Off Gel Nail Base Coat", POLISH),
    ("essie Cashmere Matte Nail Polish", POLISH),        # a shade name, not outerwear
    ("OPI Nail Lacquer - Tweed Your Heart", POLISH),
    ("essie Cashmere Matte Top Coat", POLISH),           # a nail finish word outranks the shade
    ("Gel Polish Base Coat & Primer", POLISH),           # a NAIL primer
    ("Cuticle Oil with Lipids", CUTICLE),                # "lipids" is not the lip area
    ("Cuticle Oil for 100 Nails", CUTICLE),
    ("Soy Nail Polish Remover With Almond Essential Oil", REMOVER),
    ("Acetone-Free Nail Polish Remover", REMOVER),
    ("Nail Polish Remover Wipes", REMOVER),
    ("Almond & Ginseng Cuticle Oil", CUTICLE),
    ("Good as Gold Cuticle Serum Pen", CUTICLE),
    ("Kiss GoldFinger Snow Queen Limited Edition 24 Nails", PRESS_ON),   # was haircare/general
    ("Press On Nails Short Almond", PRESS_ON),
    ("Nail Tips Clear 500pcs", PRESS_ON),
    ("24 Pcs Press On Nails with Glue and Mini File", PRESS_ON),   # accessories listed
    ("Press-On Nails with Jelly Glue Stickers", PRESS_ON),
    ("Glue On Nails 24pcs", PRESS_ON),                   # third review: "glue" vetoed "glue-on"
    ("False Nails Nude Color 24pcs", PRESS_ON),
    # KISS's glue-on line (kissusa.com 2026-09-26: ~500 of these titles matched nothing)
    ("Kiss Voguish Fantasy Press On Glue Nails - Brunch Date", PRESS_ON),
    ("Kiss Core French Press On Fake Glue Nails - If You Dare", PRESS_ON),
    ("Kiss Salon X-tend Color Press On Soft Gel Nails - Brick Road", PRESS_ON),
    ("Kiss Gel Fantasy Press On Glue Toenails - Chase It", PRESS_ON),
    ("Impress Design No Glue Fake Press On Toenails - Sweet as Honey", PRESS_ON),
    ("KISS Lash Couture Faux Mink Collection", LASHES),
    ("Ardell Wispies Natural Lashes", LASHES),
    ("KISS imPRESS Falsies Press-On Lashes", LASHES),    # lashes, not press-on nails
    ("Magnetic Lashes Kit", LASHES),
    ("Individual Lashes Clusters", LASHES),
    ("Duo Lash Adhesive", LASHES),
    ("Lash Glue Clear", LASHES),
])
def test_lash_and_nail_products_reach_their_leaf(title, want):
    assert _path(title) == want


@pytest.mark.parametrize("title,want", [
    # no bare "nail": a brand pun is not a nail product
    ("Nailed It Cleansing Balm", "beauty/skincare/cleanse/cleanser"),
    # no bare "polish": a face or body polish is still an exfoliant
    ("Face Polish", "beauty/skincare/treat/exfoliant"),
    ("Body Polish", "beauty/skincare/treat/exfoliant"),
    # another AREA declines every nail rule -- including the "eye"/"brow" compounds (review of
    # #2364: a bare \blash\b never matched "Eyelash") and "lipstick"
    ("Lip Lacquer", "beauty/makeup/lip/lipstick"),
    ("Lip Balm and Cuticle Balm", "beauty/makeup/lip/balm"),
    ("Lash Base Coat", None),
    ("Eyelash Top Coat", None),
    ("Eyelash Extension Sealant Top Coat", None),
    ("Eyebrow Gel Top Coat", None),
    ("Gel Remover for Eyelash Extensions", None),
    ("Lipstick Top Coat", "beauty/makeup/lip/lipstick"),
    ("Eyeshadow Base Coat", "beauty/makeup/eye/eyeshadow"),
    ("Base Coat Primer", "beauty/makeup/face/primer"),
    # a hair "top coat" names hair and is declined
    ("Hair Gloss Top Coat", "beauty/haircare/general"),
    # an outerwear top coat stays a coat, straight or curly apostrophe
    ("Men's Wool Top Coat", "fashion/apparel/outerwear/coat"),
    ("Men’s Top Coat", "fashion/apparel/outerwear/coat"),       # the CURLY apostrophe alone
    ("Women’s Cashmere Top Coat", "fashion/apparel/outerwear/coat"),
    # hand care beats cuticle care, as non_face_leaf rules for "Hand & Nail Cream"
    ("Hand & Cuticle Cream", "beauty/skincare/moisturize/cream"),
    # the narrowed neighbours keep their own rows
    ("Trench Coat", "fashion/apparel/outerwear/coat"),
    ("Setting Powder", "beauty/makeup/face/powder"),
    ("Makeup Remover Wipes", "beauty/skincare/cleanse/cleanser"),
    ("Gel Cleanser", "beauty/skincare/cleanse/cleanser"),
    # nail CARE and nail TOOLS with no leaf of their own stay where they were
    ("Nail Strengthening Serum", "beauty/skincare/treat/serum"),
    ("Acrylic Nail Brush", "beauty/tools/brush"),
    ("Nail Stickers Floral", None),
    # lash products that are NOT false lashes keep their homes
    ("Volume Mascara", "beauty/makeup/eye/mascara"),
    ("Lash Serum", "beauty/skincare/treat/serum"),
    ("Lash Primer", "beauty/makeup/face/primer"),
    ("Eyelash Curler", None),
    ("Lash Glue Remover", None),
    # Maybelline's "Falsies" is a mascara line, not false lashes
    ("Falsies Surreal Extensions Waterproof", None),
    # acrylic SYSTEMS and nail accessories are not press-on nails (re-review of #2364)
    ("Acrylic Nails Kit", "beauty/sets/gift-set"),      # "kit", exactly as on main
    ("Acrylic Nail Powder", None),
    ("Acrylic Nail Liquid", None),
    ("Nail Tip Cutter", None),
    ("Nail Tip Glue", None),
    ("Nail Tips & Tricks Guide", None),
    ("10 Nails Care Tips", None),
    ("Press On Nail Glue", None),                        # an accessory named after the product
    ("Press-On Nail Stickers", None),
    ("Kiss Glow-In-The-Dark Halloween Press On Fake Nail Art Stickers - Creepin", None),
    ("Kiss Press On Glue Nail Glue", None),
    ("Kiss Glue OFF Press On Fake Nails Remover", None),
    ("Kiss PowerFlex Maximum Speed Fake Nail Glue", None),
    # a product FOR the glue-on line, and tool/care/kit words, decline the widened arm (review of #2377)
    ("Kiss Nail Glue for Press On Glue Nails - 3g", None),
    ("Gel X Nail Glue Gel for Press On Soft Gel Nails Extension", None),
    ("Mini UV Lamp for Press On Soft Gel Nails", None),
    ("Nail Dehydrator for Press On Gel Nails", None),
    ("Adhesive Tabs for Press On Gel Nails, 240 pcs", None),
    ("Press On Toenails Glue", None),
    ("Top Coat for Press On Gel Nails", POLISH),
    ("Kiss Press On Glue Nails - Top Coat", POLISH),
    ("Press On Gel Nails Cuticle Oil", CUTICLE),
    ("Press On Gel Nail Care Kit", "beauty/sets/gift-set"),
    ("Kiss Core Press On Glue Nails Bundle - Core Must-Haves", "beauty/sets/gift-set"),
])
def test_refusing_examples_keep_their_old_leaf(title, want):
    assert _path(title) == want


@pytest.mark.parametrize("product_type", [
    "Nail Polish", "Nail Lacquer", "Gel Polish", "Top Coat", "Base Coat", "Dip Powder",
    "Nail Polish Remover", "Nail Polish Remover Wipes", "Cuticle Oil", "Press-On Nails",
    "False Lashes", "Lash Glue", "Gel Nail Color",
    "Gel Polish Base Coat & Primer", "UV Gel Base Coat and Primer",   # nail, not face primer
    # real merchant types on universalnailsupplies.com that the review found ambiguous
    "Powder Nail Color", "Aprés Gel Color Polish", "Polish Remover", "Acetone Polish Remover",
    "Gel Remover Wipes", "No-Wipe Top Coat",
])
def test_a_lash_or_nail_product_type_is_one_match_in_the_curated_resolver(product_type):
    """Measured through services.curated_brand_feed._pattern_matches itself: two matches read as
    ambiguous there and the row resolves to nothing, which only_resolved_category then drops."""
    assert _pattern_matches(product_type) == 1, product_type


def test_a_hand_and_nail_cream_is_still_hand_care():
    """The non-face rule is untouched: nail care named alongside the hand is hand care."""
    for title in ("Hand & Nail Cream", "Hand & Cuticle Cream"):
        hit = resolve_path_from_row(category=None, product_type="Moisturizer", title=title)
        assert hit == ("Body Care", "beauty/body/care"), title


def test_a_nail_query_browses_the_nails_prefix():
    """Backend recall's prefix for "nail polish" was `beauty/skincare/treat/` (via "polish")."""
    assert category_path_prefix_for_query("nail polish") == "beauty/makeup/nails/"
    assert category_path_prefix_for_query("press on nails") == "beauty/makeup/nails/"
    assert category_path_prefix_for_query("false lashes") == "beauty/makeup/eye/"
    assert category_path_prefix_for_query("face polish") == "beauty/skincare/treat/"


def test_an_acrylic_shelf_is_not_press_on_nails():
    """universalnailsupplies.com types 26 products "Acrylic Nails & Tips": 1 is tips, the rest are
    acrylic powders and a top coat. The type alone must not file them all as press-on nails."""
    assert _pattern_matches("Acrylic Nails & Tips") == 0


# "Brush-On" names how a glue or powder is applied, not a brush. Live store titles 2026-09-26
# (kissusa.com, unitedbeautysupply.com, shopbeautydepot.com) that sat on beauty/tools/brush. A nail
# glue has no leaf of its own, so None is the honest answer; the rows below are what each title falls
# through to once Brush lets go -- a first cut that only exempted "brush-on" sent the PowerFlex glues
# to exfoliant ("Applies Like Polish") and DUO's lash glues to gift-set (the brand "Duo").
@pytest.mark.parametrize("title,want", [
    ("Kiss InstaGlue Brush On Nail Glue", None),
    ("Kiss InstaGlue Brush - On Nail Glue", None),                  # feed spacing around the hyphen
    ("Kiss VitaBond Brush-On Nail Glue 2-Pack | Nail Adhesive, Press-On Glue, Pink Tint, "
     "Infused with Vitamins A and E", None),
    ("KISS Powerflex Brush-On Nail Glue, Professional Clear Instant False Nail Adhesive - BGL506", None),
    ("Kiss PowerFlex Brush-On Nail Glue 3-Pack | Nail Adhesive, Press-On Glue, Applies Like Polish, "
     "Lasting Wear", None),
    ("Kiss PowerFlex Brush-On Fake Nail Glue - 0.17 oz. | Nail Adhesive, Press-On Glue, "
     "Applies Like Polish, Lasting Wear", None),
    ("Walker Tape: Ultra-Hold - 0.5oz. Brush-On", None),            # a wig/lace adhesive
    ("Duo Brush On Striplash Adhesive", LASHES),                    # DUO's one-word "Striplash"
    ("DUO Striplash Adhesive 0.25 oz (7g) - Clear & Dark Professional Eyelash Glue", LASHES),
    ("Duo: Brush-On Dark Adhesive with Vitamins", LASHES),          # the brand heads the title
    ("Striplash Adhesive Clear", LASHES),                           # the one-word spelling, no brand
    ("DUO Brush On Strip Lash Adhesive Clear 0.18 oz", LASHES),
    ("Anessa Perfect UV Brush-on Powder", "beauty/makeup/face/powder"),
    ("Anessa Perfect UV Sunscreen Brush On Powder Makeup SPF50+", "beauty/skincare/sun/sunscreen"),
    ("Kiss Brush-On Gel Nail Kit | 48 White & Natural French Tips, Gel Manicure", "beauty/sets/gift-set"),
    ("Kiss Press On Glue Nail Tool Bundle - VitaBond Brush-On Nail Glue + Glue OFF Remover",
     "beauty/sets/gift-set"),
])
def test_a_brush_on_applicator_is_not_a_brush(title, want):
    assert _path(title) == want


@pytest.mark.parametrize("title,want", [
    # a real brush is still a brush, including one whose next word merely starts with "on"
    ("Kabuki Brush", "beauty/tools/brush"),
    ("Acrylic Nail Brush", "beauty/tools/brush"),
    ("Brush Online Exclusive", "beauty/tools/brush"),
    ("Foundation Brush - On Sale", "beauty/tools/brush"),
    ("Brush Onyx Handle", "beauty/tools/brush"),
    # a real polish is still an exfoliant; only the simile "like polish" is declined
    ("Body Polish", "beauty/skincare/treat/exfoliant"),
    ("Lip Polish Scrub", "beauty/skincare/treat/exfoliant"),
    ("Polish Like a Pro Face Scrub", "beauty/skincare/treat/exfoliant"),
    # a "Duo" that is a two-piece set, anywhere but heading a longer title, is still a gift set
    ("Snail Mucin Eye Care Duo", "beauty/sets/gift-set"),
    ("Lash Slick Duo", "beauty/sets/gift-set"),
    ("Duo", "beauty/sets/gift-set"),
    ("Kiss Colors & Care Edge Fixer Duo Bundle", "beauty/sets/gift-set"),
    # a DUO title with no adhesive, or a DUO remover, is not false lashes
    ("Duo Lash Adhesive Remover", None),
    ("Duo Eyeshadow Palette", "beauty/makeup/eye/eyeshadow"),
    ("Duo Glitter Pouch", None),
    # a two-pack of lash-EXTENSION glue ("Adhesive Duo") is not the DUO brand
    ("Nourishing Lash Extension Adhesive Duo", "beauty/sets/gift-set"),
    ("Edge Fixer Duo: Lace Adhesive Spray", "beauty/sets/gift-set"),
])
def test_what_brush_on_titles_fall_through_to_stays_narrow(title, want):
    assert _path(title) == want


@pytest.mark.parametrize("title", [
    # the False Lashes rule and the DUO brand no longer ALSO read as a brush or a gift set
    "Duo Brush On Striplash Adhesive",
    "DUO Brush On Strip Lash Adhesive Clear 0.18 oz",
    "Duo: Individual Lash Adhesive Clear",
    "Kiss i-Envy Waterproof Super Strong Hold Brush-On Eyelash Adhesive",
])
def test_a_brush_on_lash_adhesive_is_one_match_in_the_curated_resolver(title):
    assert _pattern_matches(title) == 1, title


@pytest.mark.parametrize("title", [
    "Kiss InstaGlue Brush On Nail Glue",
    "Kiss PowerFlex Brush-On Nail Glue 2-Pack | Nail Adhesive, Press-On Glue, Applies Like Polish",
    "Walker Tape: Extreme Hold - 0.5oz. Brush-On",
])
def test_a_brush_on_nail_or_wig_glue_matches_no_leaf(title):
    assert _pattern_matches(title) == 0, title
