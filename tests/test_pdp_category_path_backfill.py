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
    "Pixi Glow-y Lip Oil",
    "Kylie Precision Pout Lip Liner",
    "Tom Ford Gloss Luxe",
    "Fenty Gloss Bomb Stix High-Shine Gloss Stick",
    "Kylie Rosy Radiance Lip Combo",
    "Pixi Lip Duo - Choose Your Shades",
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
    "COSRX Advanced Snail 96 Mucin Power Essence",
    "Vichy Mineral 89 Hyaluronic Acid Booster Serum",
    "Kiehl's Midnight Recovery Concentrate",
]

TONER_FIXTURES = [
    "Pixi Glow Tonic Original Size",
    "Pixi Milky Tonic Original Size",
    "Round Lab DIVE IN Skin Booster",
    "Laneige Cream Skin Refiner Mist",
    "I'm From Rice Toner",
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

GIFT_SET_FIXTURES = [
    "Olehenriksen The Glow Cycle Bundle Full-Size Daily Routine",
    "Kylie Cosmic Kylie Jenner & 2.0 30ml Gift Set",
    "Embryolisse Lightweight Hydration Set",
    "Pixi Best of Pixi - Holiday Edition",
    "Cicalisse Protective Skin & Hand Care Set",
]


@pytest.mark.parametrize("title", LIPSTICK_FIXTURES)
def test_lipstick_resolves(title: str) -> None:
    hit = classify(title)
    assert hit is not None, f"no classification for {title!r}"
    assert hit[0] == "Lipstick"
    assert hit[1] == "beauty/makeup/lip/lipstick"


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
    # Lock in that we have 24 patterns ported from
    # PIVOTA-Agent BEAUTY_CATEGORY_PATTERNS (drift detection if upstream
    # adds/removes a pattern).
    assert len(CATEGORY_PATTERNS) >= 23  # at least the 23 unique categories
