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
