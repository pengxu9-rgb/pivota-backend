"""Pure-function tests for services/pdp_taxonomy.py (Phase O-2).

The module is called by all three onboarding paths (Shopify ingest /
external seed mirror / catalog enrichment agent) — keeping the rules
deterministic and tested here means each path stays consistent
without re-implementing extraction logic.
"""

from __future__ import annotations

import math

from services.pdp_taxonomy import (
    PRICE_TIER_UNKNOWN,
    derive_price_tier,
    derive_taxonomy_v1,
    extract_demographic,
    extract_lifestyle_tags,
    extract_use_case_tags,
)


# ---------------------------------------------------------------------------
# price_tier
# ---------------------------------------------------------------------------


def test_price_tier_buckets_cover_canonical_examples():
    assert derive_price_tier(0.99) == "under_50"
    assert derive_price_tier(49.99) == "under_50"
    assert derive_price_tier(50.0) == "50_100"
    assert derive_price_tier(99.99) == "50_100"
    assert derive_price_tier(100.0) == "100_200"
    assert derive_price_tier(150.0) == "100_200"
    assert derive_price_tier(200.0) == "200_500"
    assert derive_price_tier(499.99) == "200_500"
    assert derive_price_tier(500.0) == "500_plus"
    assert derive_price_tier(9999.0) == "500_plus"


def test_price_tier_zero_is_unknown_not_under_50():
    """Free / login-walled prices come through as 0.0 — distinguish
    "we don't know" from "actually cheap"."""
    assert derive_price_tier(0) == PRICE_TIER_UNKNOWN
    assert derive_price_tier(0.0) == PRICE_TIER_UNKNOWN


def test_price_tier_returns_none_for_invalid():
    assert derive_price_tier(None) is None
    assert derive_price_tier(-1.0) is None
    assert derive_price_tier(float("nan")) is None
    assert derive_price_tier("not a number") is None


def test_price_tier_accepts_int_and_decimal_like():
    """Many Shopify payloads ship price as int or numeric string."""
    assert derive_price_tier(75) == "50_100"
    assert derive_price_tier("250") == "200_500"


# ---------------------------------------------------------------------------
# lifestyle_tags
# ---------------------------------------------------------------------------


def test_lifestyle_tags_picks_clear_brand_claims():
    out = extract_lifestyle_tags(
        title="Vegan & Cruelty-Free Lipstick",
        description="100% paraben-free formula. Hypoallergenic.",
    )
    # canonical token spelling
    assert "vegan" in out
    assert "cruelty_free" in out
    assert "paraben_free" in out
    assert "hypoallergenic" in out


def test_lifestyle_tags_dedupes_when_same_signal_appears_multiple_times():
    out = extract_lifestyle_tags(
        title="Vegan Vegan Vegan Cream",
        description="vegan formula, vegan friendly",
        tags=["vegan", "VEGAN"],
    )
    assert out.count("vegan") == 1


def test_lifestyle_tags_returns_empty_when_no_claims():
    out = extract_lifestyle_tags(title="Just a Plain Product", description="No claims here.")
    assert out == []


def test_lifestyle_tags_handles_all_none():
    assert extract_lifestyle_tags() == []
    assert extract_lifestyle_tags(title=None, description=None, tags=None) == []


# ---------------------------------------------------------------------------
# use_case_tags
# ---------------------------------------------------------------------------


def test_use_case_tags_daily_match():
    assert "daily" in extract_use_case_tags(title="Daily Moisturizer SPF 50")
    assert "daily" in extract_use_case_tags(description="for everyday use")
    assert "daily" in extract_use_case_tags(description="every day comfort")


def test_use_case_tags_does_not_overmatch_substrings():
    """'professional' is whole-word — `unprofessional` shouldn't match.
    'sport' should not match 'transport' / 'support'."""
    out = extract_use_case_tags(title="Hair Transport Container")
    assert "sport" not in out
    out = extract_use_case_tags(description="great support for sensitive skin")
    assert "sport" not in out


def test_use_case_tags_picks_gift_set():
    out = extract_use_case_tags(title="Holiday Gift Set — 3 Lipsticks")
    assert "gift" in out


def test_use_case_tags_picks_travel_size():
    out = extract_use_case_tags(title="Travel-Size Lotion 50ml")
    assert "travel" in out


def test_use_case_tags_returns_empty_when_no_signals():
    assert extract_use_case_tags(title="Plain Product") == []


# ---------------------------------------------------------------------------
# demographic
# ---------------------------------------------------------------------------


def test_demographic_men_signals():
    assert extract_demographic(title="Men's Cologne") == "men"
    assert extract_demographic(title="Eau de Toilette for Men") == "men"


def test_demographic_women_signals():
    assert extract_demographic(title="Women's Perfume") == "women"
    assert extract_demographic(title="For Her — Limited Edition") == "women"
    assert extract_demographic(title="Ladies' Sport Watch") == "women"


def test_demographic_unisex_signal():
    assert extract_demographic(title="Unisex Fragrance") == "unisex"


def test_demographic_kids_signals():
    assert extract_demographic(title="Kids' Shampoo") == "kids"
    assert extract_demographic(title="Toddler Bath Gel") == "kids"
    assert extract_demographic(title="Children's Sunscreen SPF 50") == "kids"


def test_demographic_returns_none_when_ambiguous():
    """Most products have no demographic signal in the title.
    Conservative: return None and let Phase O-3 LabelAgent decide."""
    assert extract_demographic(title="Hyaluronic Acid Serum") is None
    assert extract_demographic(title="Just a Plain Product") is None


def test_demographic_kids_takes_precedence_over_women():
    """Order matters: 'baby girl' should match kids first, not women."""
    out = extract_demographic(title="Baby Girl Onesie Set")
    assert out == "kids"


# ---------------------------------------------------------------------------
# derive_taxonomy_v1 — integration of the above
# ---------------------------------------------------------------------------


def test_derive_taxonomy_v1_picks_all_four():
    result = derive_taxonomy_v1(
        price=85.00,
        title="Vegan Daily Moisturizer for Women",
        description="Cruelty-free, fragrance-free formula for everyday use.",
        tags=["k-beauty"],
    )
    assert result == {
        "price_tier": "50_100",
        "use_case_tags": ["daily"],
        "lifestyle_tags": ["vegan", "cruelty_free", "fragrance_free"],
        "demographic": "women",
    }


def test_derive_taxonomy_v1_returns_consistent_shape_when_all_signals_absent():
    """Empty product still produces the same shape — None scalars,
    empty lists. Downstream INSERT writes [] to lifestyle/use_case
    (ingest saw and was empty), NULL to price/demographic (no signal)."""
    result = derive_taxonomy_v1()
    assert result == {
        "price_tier": None,
        "use_case_tags": [],
        "lifestyle_tags": [],
        "demographic": None,
    }


def test_derive_taxonomy_v1_zero_price_is_unknown():
    result = derive_taxonomy_v1(price=0.0, title="Free Sample")
    assert result["price_tier"] == PRICE_TIER_UNKNOWN
