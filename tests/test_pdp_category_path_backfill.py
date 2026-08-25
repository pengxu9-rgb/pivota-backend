"""Test the regex → category_path mapping in backfill_pdp_category_path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill_pdp_category_path import (  # noqa: E402
    CATEGORY_PATTERNS,
    classify,
    resolve_path_from_row,
)


# 10+ examples per (or covering) category, exercising multiple pattern variants.
LIPSTICK_FIXTURES = [
    "MAC Ruby Woo Matte Lipstick",
    "Charlotte Tilbury Pillow Talk Lip Color",
    "YSL Liquid Lip 22",
    "Fenty Beauty Stunna Lip Lacquer",
    "NARS Audacious Lipstick",
    "Tom Ford Lip Color Sheer",
    "Maybelline Superstay Liquid Lip",
    "Glossier Generation G Sheer Lipstick",
    "Pat McGrath MatteTrance Lipstick",
    "Rare Beauty Stay Vulnerable Lip Color",
    "Kylie Rosy Radiance Lip Combo",
    "Pixi Lip Duo - Choose Your Shades",
]

# Lip subtypes. These four titles used to live in LIPSTICK_FIXTURES because the
# Lipstick regex was a lip catch-all. They moved here — not deleted — when the
# dedicated Lip Gloss / Lip Oil / Lip Liner / Lip Tint patterns landed ahead of
# Lipstick, so each keeps its coverage under the label it now resolves to.
LIP_GLOSS_FIXTURES = [
    "Tom Ford Gloss Luxe",
    "Fenty Gloss Bomb Stix High-Shine Gloss Stick",
]

LIP_OIL_FIXTURES = [
    "Pixi Glow-y Lip Oil",
]

LIP_LINER_FIXTURES = [
    "Kylie Precision Pout Lip Liner",
]

FOUNDATION_FIXTURES = [
    "Estée Lauder Double Wear Foundation",
    "Charlotte Tilbury Hollywood Flawless Foundation",
    "Maybelline Fit Me Matte Foundation",
    "L'Oréal True Match Skin Tint",
    "NARS Cushion Foundation Refill",
    "Chanel Les Beiges Foundation Stick",
    "Tarte Shape Tape Foundation",
    "Bobbi Brown Skin Long-Wear Foundation",
    "Dior Forever Foundation 3CR",
    "Armani Luminous Silk Foundation",
    "Fenty Eaze Drop Blur + Smooth Tint Stick",
]

MASCARA_FIXTURES = [
    "Maybelline Lash Sensational Mascara",
    "Dior Iconic Overcurl Mascara",
    "Lancôme Hypnose Mascara",
    "MAC Stack Mascara Volume",
    "Glossier Lash Slick Mascara",
    "Too Faced Better Than Sex Mascara",
    "Benefit BadGal Bang Mascara",
    "L'Oréal Voluminous Mascara Carbon Black",
    "Tarte Tubing Mascara",
    "Charlotte Tilbury Pillow Talk Mascara",
]

FRAGRANCE_FIXTURES = [
    "Chanel No. 5 Eau de Parfum",
    "Dior Sauvage Eau de Toilette",
    "Tom Ford Black Orchid Parfum",
    "Le Labo Santal 33 Cologne",
    "Jo Malone English Pear Cologne",
    "YSL Black Opium Eau de Parfum",
    "Maison Margiela Replica Beach Walk Perfume",
    "Diptyque Tam Dao Eau de Parfum",
    "Byredo Gypsy Water Perfume",
    "Issey Miyake L'Eau d'Issey Eau de Toilette",
    "Tom Ford Ébène Fumé All Over Body Spray",
    "Guerlain Vanille Planifolia Extrait 21",
]

SUNSCREEN_FIXTURES = [
    "Supergoop Unseen Sunscreen SPF 40",
    "La Roche-Posay Anthelios SPF 60",
    "Beauty of Joseon Relief Sun SPF 50+",
    "Black Girl Sunscreen SPF 30",
    "Anessa Perfect UV Sunscreen Milk",
    "Shiseido Ultimate Sun Protector Lotion SPF 50",
    "EltaMD UV Clear Broad Spectrum SPF 46",
    "Drunk Elephant Umbra Sheer Physical Daily Defense SPF 30",
    "Naturium Dew-Glow Moisturizer SPF 50",
    "Hawaiian Tropic Silk Hydration UV Lock SPF 30",
]

MOISTURIZER_FIXTURES = [
    "CeraVe Moisturizing Cream",
    "Cetaphil Moisturizing Lotion",
    "La Mer Moisturizing Cream",
    "Tatcha The Water Cream",
    "Drunk Elephant Lala Retro Moisturizer",
    "Olay Regenerist Micro-Sculpting Cream",
    "Belif The True Cream Aqua Bomb",
    "First Aid Beauty Ultra Repair Cream",
    "Embryolisse Lait-Crème Concentré Lotion",
    "Sunday Riley Tidal Brightening Enzyme Cream",
]

CLEANSER_FIXTURES = [
    "CeraVe Hydrating Facial Cleanser",
    "Cetaphil Gentle Skin Cleanser",
    "La Roche-Posay Toleriane Hydrating Cleanser",
    "Glossier Milky Jelly Cleanser",
    "Banila Co Clean It Zero Cleansing Balm",
    "Tatcha The Rice Wash Cleanser",
    "Drunk Elephant Beste No. 9 Jelly Cleanser",
    "Boscia Detoxifying Black Cleansing Foam",
    "First Aid Beauty Pure Skin Face Cleanser",
    "Innisfree Apple Seed Cleansing Oil",
    "Beekman 1802 Fresh Air Face Wipes",
]

SERUM_FIXTURES = [
    "The Ordinary Niacinamide Serum",
    "SkinCeuticals C E Ferulic Serum",
    "Estée Lauder Advanced Night Repair Serum",
    "Olay Regenerist Retinol 24 Serum",
    "Naturium Vitamin C Complex Face Serum",
    "Caudalie Vinopure Salicylic Serum",
    "Beauty of Joseon Glow Serum",
    "Vichy Mineral 89 Hyaluronic Acid Booster Serum",
    "Kiehl's Midnight Recovery Concentrate",
]

TONER_FIXTURES = [
    "Pixi Glow Tonic Original Size",
    "Pixi Milky Tonic Original Size",
    "Round Lab DIVE IN Skin Booster",
    "Laneige Cream Skin Refiner Mist",
    "I'm From Rice Toner",
    # "essence" is a Toner keyword: a K-beauty essence is functionally a toner,
    # and matching it here also keeps "Mask Fit Tone Up Essence" off the Mask
    # pattern, which would otherwise win on the product-line word "Mask".
    "COSRX Advanced Snail 96 Mucin Power Essence",
]

TREATMENT_FIXTURES = [
    "Naturium Azelaic Acid Derivative Complex 10%",
    "Olehenriksen GFH Glow from Home Vitamin C Duo",
    "Murad Retinol Youth Renewal Night Treatment",
    "Pixi Spot Stickers Trio",
    "Peace Out Acne Stickers",
]

FACE_OIL_FIXTURES = [
    "Jurlique Lavender Pure Essential Oil",
    "Jurlique Face Oils Discovery",
    "NUXE Huile Prodigieuse Body Oil",
    "Sunday Riley Juno Antioxidant + Superfood Face Oil",
    "Kiehl's Midnight Recovery Oil Drops",
]

TANNING_FIXTURES = [
    "Pixi GradualGlow Self-Tan Petite Size",
    "Isle of Paradise Self Tanning Drops",
    "Tan-Luxe The Face Illuminating Self-Tan Drops",
    "St. Tropez Self Tan Classic Bronzing Mousse",
    "Bondi Sands Gradual Tanning Milk",
]

MASK_FIXTURES = [
    "Round Lab Birch Juice Moisturizing Gel Mask",
    "COSRX Poreless Clarifying Charcoal Mask Pink",
    "Fenty Cookies N Clean Whipped Clay Pore Detox Face Mask",
    "Beauty of Joseon Revive Under Eye Patch",
    "Mediheal Tea Tree Essential Sheet Mask",
    "Laneige Water Sleeping Mask",
    "Innisfree Super Volcanic Pore Clay Mask",
    "Hero Cosmetics Mighty Pimple Patch",
    "Pixi LipPatch",
    "Summer Fridays Jet Lag Mask",
    "Anua Ultra-Thin Spot Cover Patch",
    "Patyka Patchs Lift Regard 360°",
]

EXFOLIANT_FIXTURES = [
    "Pixi Clarity Acid Peel",
    "Beauty of Joseon Apricot Blossom Peeling Gel",
    "The Ordinary AHA 30% + BHA 2% Peeling Solution",
    "Dermalogica Daily Microfoliant Exfoliant",
    "Olehenriksen Lemonade Smoothing Scrub",
    "Tatcha The Rice Polish",
    "Drunk Elephant Babyfacial AHA BHA Peel",
    "Paula's Choice Skin Perfecting 2% BHA Liquid Exfoliant",
    "Dr. Dennis Gross Alpha Beta Daily Peel",
    "Kate Somerville ExfoliKate Intensive Exfoliating Treatment",
]

PRIMER_FIXTURES = [
    "TIRTIR Flawless Pore Prep Primer",
    "Benefit POREfessional Face Primer",
    "Fenty Pro Filt'r Instant Retouch Primer",
    "Rare Beauty Always an Optimist Illuminating Primer",
    "Milk Makeup Hydro Grip Primer",
    "Smashbox Photo Finish Primer",
    "e.l.f. Pore-Filling Primer",
    "Hourglass Veil Mineral Primer",
    "Laura Mercier Pure Canvas Primer",
    "Tatcha The Silk Canvas Primer",
]

EYELINER_FIXTURES = [
    "Fenty Flypencil Longwear Pencil Eyeliner",
    "Glitty Lid Shimmer Liquid Eyeliner",
    "Stila Stay All Day Waterproof Liquid Eye Liner",
    "Urban Decay 24/7 Glide-On Pencil Liner",
    "KVD Tattoo Liquid Liner",
    "NYX Epic Ink Eyeliner",
    "Charlotte Tilbury Rock 'N' Kohl Eyeliner",
    "Rare Beauty Perfect Strokes Matte Liquid Liner",
    "Make Up For Ever Aqua Resist Pencil Liner",
    "Lancôme Idôle Ultra-Precise Felt Tip Liquid Liner",
]

BODY_CARE_FIXTURES = [
    "KraveBeauty Great Body Relief",
    "Fenty Butta Drop Hydrating Body Milk",
    "Rare Beauty Find Comfort Mini Body Essentials",
    "Cicalisse Protective Skin & Hand Care Set",
    "Kylie Loofah",
]

GIFT_SET_FIXTURES = [
    "Olehenriksen The Glow Cycle Bundle Full-Size Daily Routine",
    "Kylie Cosmic Kylie Jenner & 2.0 30ml Gift Set",
    "Embryolisse Lightweight Hydration Set",
    "Pixi Best of Pixi - Holiday Edition",
    "Pixi Daily Glow Duo",
    "Sigma Soft Blend Eye Duo",
]


@pytest.mark.parametrize("title", LIPSTICK_FIXTURES)
def test_lipstick_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None, f"no classification for {title!r}"
    assert hit[0] == "Lipstick"
    assert hit[1] == "beauty/makeup/lip/lipstick"


@pytest.mark.parametrize("title", LIP_GLOSS_FIXTURES)
def test_lip_gloss_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None, f"no classification for {title!r}"
    assert hit[0] == "Lip Gloss"
    assert hit[1] == "beauty/makeup/lip/gloss"


@pytest.mark.parametrize("title", LIP_OIL_FIXTURES)
def test_lip_oil_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None, f"no classification for {title!r}"
    assert hit[0] == "Lip Oil"
    assert hit[1] == "beauty/makeup/lip/oil"


@pytest.mark.parametrize("title", LIP_LINER_FIXTURES)
def test_lip_liner_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None, f"no classification for {title!r}"
    assert hit[0] == "Lip Liner"
    assert hit[1] == "beauty/makeup/lip/liner"


@pytest.mark.parametrize("title", FOUNDATION_FIXTURES)
def test_foundation_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Foundation"
    assert hit[1] == "beauty/makeup/face/foundation"


@pytest.mark.parametrize("title", MASCARA_FIXTURES)
def test_mascara_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Mascara"
    assert hit[1] == "beauty/makeup/eye/mascara"


@pytest.mark.parametrize("title", FRAGRANCE_FIXTURES)
def test_fragrance_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Fragrance"
    assert hit[1] == "beauty/fragrance/perfume"


@pytest.mark.parametrize("title", SUNSCREEN_FIXTURES)
def test_sunscreen_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Sunscreen"
    assert hit[1] == "beauty/skincare/sun/sunscreen"


@pytest.mark.parametrize("title", MOISTURIZER_FIXTURES)
def test_moisturizer_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Moisturizer"
    assert hit[1] == "beauty/skincare/moisturize/cream"


@pytest.mark.parametrize("title", CLEANSER_FIXTURES)
def test_cleanser_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Cleanser"
    assert hit[1] == "beauty/skincare/cleanse/cleanser"


@pytest.mark.parametrize("title", SERUM_FIXTURES)
def test_serum_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Serum"
    assert hit[1] == "beauty/skincare/treat/serum"


@pytest.mark.parametrize("title", TONER_FIXTURES)
def test_toner_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Toner"
    assert hit[1] == "beauty/skincare/treat/toner"


@pytest.mark.parametrize("title", TREATMENT_FIXTURES)
def test_treatment_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Treatment"
    assert hit[1] == "beauty/skincare/treat/treatment"


@pytest.mark.parametrize("title", FACE_OIL_FIXTURES)
def test_face_oil_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Face Oil"
    assert hit[1] == "beauty/skincare/moisturize/oil"


@pytest.mark.parametrize("title", TANNING_FIXTURES)
def test_tanning_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Tanning"
    assert hit[1] == "beauty/body/tanning"


@pytest.mark.parametrize("title", MASK_FIXTURES)
def test_mask_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Mask"
    assert hit[1] == "beauty/skincare/treat/mask"


@pytest.mark.parametrize("title", EXFOLIANT_FIXTURES)
def test_exfoliant_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Exfoliant"
    assert hit[1] == "beauty/skincare/treat/exfoliant"


@pytest.mark.parametrize("title", PRIMER_FIXTURES)
def test_primer_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Primer"
    assert hit[1] == "beauty/makeup/face/primer"


@pytest.mark.parametrize("title", EYELINER_FIXTURES)
def test_eyeliner_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Eyeliner"
    assert hit[1] == "beauty/makeup/eye/eyeliner"


@pytest.mark.parametrize("title", BODY_CARE_FIXTURES)
def test_body_care_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Body Care"
    assert hit[1] == "beauty/body/care"


@pytest.mark.parametrize("title", GIFT_SET_FIXTURES)
def test_gift_set_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None
    assert hit[0] == "Gift Set"
    assert hit[1] == "beauty/sets/gift-set"


def test_priority_uses_category_first_then_product_type_then_title() -> None:
    # Category wins even if title would match a different one.
    hit = resolve_path_from_row(
        category="Lipstick",
        product_type="random unrelated string",
        title="MAC Iconic Mascara",  # would match Mascara if title-first
    )
    assert hit is not None
    assert hit[0] == "Lipstick"


def test_returns_none_when_nothing_matches() -> None:
    assert resolve_path_from_row(
        category=None,
        product_type=None,
        title="some quirky thing with no signal",
    ) is None


def test_handles_none_inputs() -> None:
    assert classify(None) is None
    assert classify("") is None
    assert resolve_path_from_row(category=None, product_type=None, title=None) is None


def test_pattern_count_matches_source() -> None:
    # Original 23 beauty + 19 fashion/apparel (PR #551) + 6 pet-accessory/
    # vest/base-layer/sponge/brush-pouch additions (this PR, covers the
    # long tail of PawStyle product_types not matched by the first cut).
    assert len(CATEGORY_PATTERNS) >= 48


# Phase O-5b drift-detection: lock in fashion category mappings.
APPAREL_FIXTURES = [
    ("Warm Fall/Winter Color-Block Sleeveless Knitted Sweater for Dogs & Cats",
     "Sweater", "fashion/apparel/tops/sweater"),
    ("Push-Up Lingerie Set", "Lingerie", "fashion/apparel/intimates/lingerie"),
    ("Linen Summer Dress", "Dress", "fashion/apparel/dresses"),
    ("Slim Fit Skinny Jeans", "Jeans", "fashion/apparel/bottoms/jeans"),
    ("Wool Trench Coat", "Coat", "fashion/apparel/outerwear/coat"),
    ("Air Jordan 1 Sneakers", "Shoes", "fashion/shoes"),
    ("Leather Crossbody Handbag", "Bag", "fashion/accessories/bag"),
    ("Pet Sweater for Small Dogs", "Sweater", "fashion/apparel/tops/sweater"),
    # Pet-accessory + vest + base-layer + beauty-tool extension (this PR).
    ("Comfy Dog Harness for Small to Medium Dogs",
     "Pet Accessory", "fashion/accessories/pet"),
    ("Tactical Dog Harness", "Pet Accessory", "fashion/accessories/pet"),
    ("Retractable Dog Leash", "Pet Accessory", "fashion/accessories/pet"),
    ("Cat Harness with Leash", "Pet Accessory", "fashion/accessories/pet"),
    ("Padded Vest", "Vest", "fashion/apparel/outerwear/vest"),
    ("Down Puffer Vest", "Vest", "fashion/apparel/outerwear/vest"),
    ("4-Leg Onesie", "Pet Apparel", "fashion/apparel/pet"),
    ("Pet Overalls", "Pet Apparel", "fashion/apparel/pet"),
    ("2-Leg Base Layer", "Base Layer", "fashion/apparel/base-layer"),
    ("Makeup Sponge/Puff", "Makeup Sponge", "beauty/tools/sponge"),
    ("Beauty Sponge Blender", "Makeup Sponge", "beauty/tools/sponge"),
    ("Brush Bag", "Brush Pouch", "beauty/tools/brush-accessory"),
    ("Makeup Brush Pouch", "Brush Pouch", "beauty/tools/brush-accessory"),
]


@pytest.mark.parametrize("title,expected_label,expected_path", APPAREL_FIXTURES)
def test_apparel_fixtures_classify_to_fashion(title: str, expected_label: str, expected_path: str) -> None:
    hit = classify(title)
    assert hit is not None, f"no classification for {title!r}"
    assert hit[0] == expected_label
    assert hit[1] == expected_path


def test_beauty_classifications_unchanged_by_fashion_additions() -> None:
    # Sanity check: beauty titles still match original beauty paths.
    cases = [
        ("MAC Ruby Woo Matte Lipstick", "Lipstick", "beauty/makeup/lip/lipstick"),
        ("CeraVe Moisturizing Cream", "Moisturizer", "beauty/skincare/moisturize/cream"),
        ("Maybelline Lash Sensational Mascara", "Mascara", "beauty/makeup/eye/mascara"),
        ("La Roche-Posay Anthelios SPF 60", "Sunscreen", "beauty/skincare/sun/sunscreen"),
    ]
    for title, label, path in cases:
        hit = classify(title)
        assert hit is not None, f"beauty regression: no classification for {title!r}"
        assert hit[0] == label
        assert hit[1] == path


# ---------- fold_category_from_variants (Phase O-5) ----------

from services.pdp_category_classifier import (  # noqa: E402
    CATEGORY_CONFIDENCE_MERCHANT,
    CATEGORY_CONFIDENCE_VARIANT,
    CATEGORY_SOURCE_MERCHANT,
    CATEGORY_SOURCE_VARIANT,
    fold_category_from_variants,
)


def test_fold_uses_product_level_when_it_hits() -> None:
    result = fold_category_from_variants(
        category="Lipstick",
        product_type=None,
        title="random",
        variants=[{"title": "Mascara variant"}],  # should be ignored
    )
    assert result is not None
    (label, path), source, confidence = result
    assert label == "Lipstick"
    assert path == "beauty/makeup/lip/lipstick"
    assert source == CATEGORY_SOURCE_MERCHANT
    assert confidence == CATEGORY_CONFIDENCE_MERCHANT


def test_fold_falls_back_to_variant_when_product_level_misses() -> None:
    # Product level has nothing classifiable; a variant title carries "lipstick".
    result = fold_category_from_variants(
        category=None,
        product_type=None,
        title="opaque branded item with no category words",
        variants=[
            {"title": "Red shade"},  # no signal
            {"title": "MAC Ruby Woo Matte Lipstick"},  # hits Lipstick
        ],
    )
    assert result is not None
    (label, path), source, confidence = result
    assert label == "Lipstick"
    assert source == CATEGORY_SOURCE_VARIANT
    assert confidence == CATEGORY_CONFIDENCE_VARIANT


def test_fold_reads_variant_platform_metadata_product_type() -> None:
    # Some Shopify webhook payloads put category info inside platform_metadata
    # rather than at the variant top level.
    result = fold_category_from_variants(
        category=None,
        product_type=None,
        title=None,
        variants=[
            {"platform_metadata": {"product_type": "Mascara"}},
        ],
    )
    assert result is not None
    (label, _path), source, _ = result
    assert label == "Mascara"
    assert source == CATEGORY_SOURCE_VARIANT


def test_fold_returns_none_when_nothing_matches_anywhere() -> None:
    result = fold_category_from_variants(
        category=None,
        product_type=None,
        title="quirky item with no category signal",
        variants=[{"title": "also no signal"}],
    )
    assert result is None


def test_fold_handles_empty_variants_list() -> None:
    result = fold_category_from_variants(
        category="Foundation",
        product_type=None,
        title=None,
        variants=[],
    )
    assert result is not None
    (label, _path), source, _ = result
    assert label == "Foundation"
    assert source == CATEGORY_SOURCE_MERCHANT


def test_fold_handles_none_variants() -> None:
    result = fold_category_from_variants(
        category="Foundation",
        product_type=None,
        title=None,
        variants=None,
    )
    assert result is not None
    (label, _path), _, _ = result
    assert label == "Foundation"
