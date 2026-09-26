"""Nail products get leaves (Peng 2026-09-26).

Before this, `beauty/makeup/nails/*` were declared TAXONOMY_GAPS: a nail product had no honest leaf
and fell to whatever other noun it carried ("Peel Off Nail Polish" -> exfoliant, a dip powder -> face
powder, "Top Coat" -> a fashion coat), and a curated crawl with only_resolved_category dropped every
lacquer. Each rule below is stated with REFUSING examples next to the accepting ones.
"""
from __future__ import annotations

import pytest

from services.pdp_category_classifier import (
    CATEGORY_PATTERNS,
    category_path_prefix_for_query,
    classify,
    resolve_path_from_row,
)

POLISH = "beauty/makeup/nails/nail-polish"
REMOVER = "beauty/makeup/nails/nail-polish-remover"
CUTICLE = "beauty/makeup/nails/cuticle-oil"


def _path(text):
    hit = classify(text)
    return hit[1] if hit else None


def _matches(text):
    """services/curated_brand_feed._pattern_matches: distinct paths ANY pattern gives. A merchant
    product_type that matches two reads as ambiguous there and resolves to nothing."""
    return len({path for _label, path, pattern in CATEGORY_PATTERNS if pattern.search(text)})


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
    ("Soy Nail Polish Remover With Almond Essential Oil", REMOVER),
    ("Acetone-Free Nail Polish Remover", REMOVER),
    ("Nail Polish Remover Wipes", REMOVER),
    ("Almond & Ginseng Cuticle Oil", CUTICLE),
    ("Good as Gold Cuticle Serum Pen", CUTICLE),
])
def test_nail_products_reach_a_nail_leaf(title, want):
    assert _path(title) == want


@pytest.mark.parametrize("title,want", [
    # no bare "nail": a brand pun is not a nail product
    ("Nailed It Cleansing Balm", "beauty/skincare/cleanse/cleanser"),
    # no bare "polish": a face or body polish is still an exfoliant
    ("Face Polish", "beauty/skincare/treat/exfoliant"),
    ("Body Polish", "beauty/skincare/treat/exfoliant"),
    # a lip lacquer is lipstick; lips / lashes / brows decline every nail rule
    ("Lip Lacquer", "beauty/makeup/lip/lipstick"),
    ("Lip Balm and Cuticle Balm", "beauty/makeup/lip/balm"),
    ("Lash Base Coat", None),
    # a hair "top coat" names hair and is declined
    ("Hair Gloss Top Coat", "beauty/haircare/general"),
    # the narrowed neighbours keep their own rows
    ("Trench Coat", "fashion/apparel/outerwear/coat"),
    ("Setting Powder", "beauty/makeup/face/powder"),
    ("Makeup Remover Wipes", "beauty/skincare/cleanse/cleanser"),
    ("Gel Cleanser", "beauty/skincare/cleanse/cleanser"),
    # nail CARE with no nail leaf of its own stays where it was
    ("Nail Strengthening Serum", "beauty/skincare/treat/serum"),
])
def test_refusing_examples_keep_their_old_leaf(title, want):
    assert _path(title) == want


@pytest.mark.parametrize("product_type", [
    "Nail Polish", "Nail Lacquer", "Gel Polish", "Top Coat", "Base Coat", "Dip Powder",
    "Nail Polish Remover", "Nail Polish Remover Wipes", "Cuticle Oil",
])
def test_a_nail_product_type_is_one_match_not_an_ambiguous_two(product_type):
    """Remover sits above polish and polish refuses a trailing "remover"; exfoliant, powder, coat
    and cleanser decline the nail spellings. Otherwise a nail store's shelf would be ambiguous."""
    assert _matches(product_type) == 1, product_type


def test_a_hand_and_nail_cream_is_still_hand_care():
    """The non-face rule is untouched: nail care named alongside the hand is hand care."""
    hit = resolve_path_from_row(category=None, product_type="Moisturizer", title="Hand & Nail Cream")
    assert hit == ("Body Care", "beauty/body/care")


def test_a_nail_query_browses_the_nails_prefix():
    """Backend recall's prefix for "nail polish" was `beauty/skincare/treat/` (via "polish")."""
    assert category_path_prefix_for_query("nail polish") == "beauty/makeup/nails/"
    assert category_path_prefix_for_query("face polish") == "beauty/skincare/treat/"
