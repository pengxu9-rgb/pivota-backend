"""An ACID or PEELING pad is an exfoliant; every other pad stays the toner it was.

The Toner entry claims a bare "pad", which is right for the hydrating/soothing pads that dominate
the shelf and wrong for an exfoliating one: measured 2026-09-23 on sokoglam's "Physical" shelf,
SOME BY MI's AHA-BHA-PHA pad and IOPE's Ampoule Peel Pad both read as toners. The new arm sits
above Toner and narrows nothing -- a pad with no acid and no peel noun falls through unchanged.

Prod census (catalog_products, 333 rows whose title or product_type mentions a pad): only titles
naming an exfoliating acid or a peel/exfoliating pad move, and every example below is a real title
unless marked invented.
"""
import pytest

from services.pdp_category_classifier import CATEGORY_PATTERNS, classify

EXFOLIANT = "beauty/skincare/treat/exfoliant"
TONER = "beauty/skincare/tone/toner"


def leaf(title):
    hit = classify(title)
    return hit[1] if hit else None


@pytest.mark.parametrize("title", [
    "AHA-BHA-PHA 30 Days Miracle Truecica Clear Pad",   # sokoglam, the row this unblocks
    "Skin Booster Ampoule Peel Pad",                    # sokoglam IOPE
    "Mediheal Phyto-enzyme Peeling Pad",                # ohlolly, stored as a toner today
    "Ji Woo Gae Heartleaf BHA Peeling Pad",
    "[CLEARDEA] Green Tangerine Mild Dual Peeling Pad 140ml (50 Pads)",
    "REPAIR MILKY Skin Peel Pad",
    "Exfoliating Pad",                                  # invented
])
def test_an_acid_or_peel_pad_is_an_exfoliant(title):
    assert leaf(title) == EXFOLIANT


@pytest.mark.parametrize("title", [
    "Anua Heartleaf 77% Toner Pad (70 Sheets)",
    "BIODANCE Collagen Gel Toner Pads",
    "NEOGEN Dermalogy Real Cica Pad",
    "Torriden DIVE-IN Low Molecule Hyaluronic Acid Multi Pad (80 pads)",   # hyaluronic is no acid peel
    "Anua Azelaic 10 Hyaluron Redness Soothing Pad",                       # azelaic is a treatment acid
    "Round Lab Birch Juice Moisturizing Pad (80pads)",
    "[MD's PICK] TecaTeca Calming Gauze Pad 70 pads, 105ml",               # gauze is not a peel noun
])
def test_every_other_pad_is_still_the_toner_it_was(title):
    assert leaf(title) == TONER


def test_a_title_that_calls_itself_a_toner_pad_keeps_the_toner_leaf():
    """Measured in prod: a BHA toner pad. The acids are ingredients, the noun is the product."""
    assert leaf("Ji Woo Gae Cica BHA Blemish Toner Pad") == TONER
    # NOTE the plural: the Toner entry's bare "pad" has always been SINGULAR, so a plural pad
    # resolves to nothing at all. The arm's own `pads?` is what makes plural acid pads resolve.
    assert leaf("AHA BHA Toning Pads") is None  # invented
    assert leaf("Glycolic Acid Resurfacing Pads") == EXFOLIANT  # invented: new, was None


@pytest.mark.parametrize("title,want", [
    ("SOME BY MI AHA-BHA-PHA 30 Days Miracle Toner (150ml)", TONER),   # acids, no pad noun
    ("AHA-BHA-PHA 30 Days Miracle Cream", "beauty/skincare/moisturize/cream"),
    ("Pyunkang Yul 1/3 Cotton Pads (160pcs)", None),                   # plural: unresolved before and after
])
def test_a_title_without_an_acid_pad_pair_is_untouched(title, want):
    assert leaf(title) == want


def test_the_arm_sits_directly_above_the_toner_entry():
    """First-match-wins is the whole mechanism: below Toner the bare "pad" would win first."""
    paths = [path for _label, path, _rx in CATEGORY_PATTERNS]
    assert paths[paths.index(TONER) - 1] == EXFOLIANT


def test_the_arm_cannot_claim_a_title_with_no_pad_noun():
    arm = CATEGORY_PATTERNS[[p for _l, p, _r in CATEGORY_PATTERNS].index(TONER) - 1][2]
    for title in ["AHA BHA PHA Serum", "Glycolic Acid Toner", "Peeling Gel", "Padded Vest"]:
        assert not arm.search(title), title
