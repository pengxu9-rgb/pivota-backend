"""Fix #2: external-seed quality scoring must read ingredients + fall back to
category_kind, so genuinely-stocked crawl products clear the serving threshold.

Before this fix, build_servable_quality_payload passed only title/desc/price/
image/brand/product_type. Crawl products with a null product_type and a
separate ingredient list scored ~50 (three zeroed components: summary,
attributes, brand+category) and never served.
"""
from services.external_seed_servability import build_servable_quality_payload
from services.index_pipeline_state_service import QUALITY_SCORE_THRESHOLD
from services.product_quality_service import (
    preview_quality,
    source_backed_attribute_signal_count,
)

# A realistic thin crawl product: rich-enough text, image, price, brand — but a
# NULL product_type and its INCI carried separately.
THIN = dict(
    title="Heartleaf 77% Soothing Toner",
    description=(
        "A gentle daily toner for sensitive, blemish-prone skin. Calms redness, "
        "balances moisture, and preps skin without stripping. Fragrance-free and "
        "suitable for morning and night use."
    ),
    price=22.0,
    image_url="https://cdn.shopify.com/x.jpg",
    brand="Anua",
    product_type=None,
)
INCI = ("Water, Houttuynia Cordata Extract, Butylene Glycol, Glycerin, "
        "1,2-Hexanediol, Niacinamide, Panthenol, Sodium Hyaluronate, "
        "Allantoin, Ethylhexylglycerin")


def test_category_fallback_populates_global_category():
    # null product_type but a category_kind fallback -> category is present
    p = build_servable_quality_payload(**THIN, category="skincare")
    assert p["global_category_id"] == "skincare"


def test_raw_inci_feeds_source_backed_attributes():
    p = build_servable_quality_payload(**THIN, category="skincare", raw_inci=INCI)
    assert source_backed_attribute_signal_count(p) >= 3
    assert p["seed_data"]["inci_list"][0].lower() == "water"
    assert "houttuynia" in p["seed_data"]["pdp_ingredients_raw"].lower()


def test_no_inci_leaves_no_seed_data():
    p = build_servable_quality_payload(**THIN, category="skincare")
    assert "seed_data" not in p  # no ingredients -> nothing injected


def test_enrichment_lifts_score_over_serving_threshold():
    thin = build_servable_quality_payload(**THIN)  # null product_type, no inci
    rich = build_servable_quality_payload(**THIN, category="skincare", raw_inci=INCI)
    thin_score = preview_quality(thin, score_source_backed_components=True)["content_quality_score"]
    rich_score = preview_quality(rich, score_source_backed_components=True)["content_quality_score"]
    # Reference the CONSTANT, not a literal: the floor moved 65.0 -> 71.4 on
    # 2026-07-28 alongside dropping the dead `summary` component, and a hard-coded
    # bar silently stops testing the policy the moment the policy changes.
    assert rich_score > thin_score
    assert rich_score >= QUALITY_SCORE_THRESHOLD   # crosses the serving gate
    assert thin_score < QUALITY_SCORE_THRESHOLD    # baseline would NOT serve
