"""PR #2158 safeguards plus the two bounded, measured Meitu exceptions."""
import pytest
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent import ingestion as ing


def record(title="Retro Matte Lipstick", product_type="Lipstick", category="beauty/makeup"):
    return feed.shopify_product_to_record({
        "title": title, "product_type": product_type, "handle": "example", "vendor": "MAC",
        "variants": [{"price": "24", "available": True}], "images": [{"src": "https://x.com/i.jpg"}],
    }, domain="maccosmetics.com", category_path=category)


def test_one_storefront_has_multiple_product_categories_and_confidence_survives_insert():
    lipstick = record()
    mascara = record(title="Gigablack Lash", product_type="Mascara")
    assert lipstick["pdp"]["category_path"] == "beauty/makeup/lip/lipstick"
    assert mascara["pdp"]["category_path"] == "beauty/makeup/eye/mascara"
    payload = ing._build_pdp_payload(lipstick)
    row = ing._build_pdp_insert(pdp_payload=payload, offers=lipstick["offers"], source_jsonl=None,
                                seller={"merchant_id": "m", "platform": "shopify"})
    assert row["category_confidence"] == feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE
    assert row["category_label_source"] == "enrichment_agent_v1"
    # This input field is not allowed to change canonical-scope lane semantics.
    lipstick["pdp"]["category_label_source"] = "merchant_payload"
    assert "category_label_source" not in ing._build_pdp_payload(lipstick)


@pytest.mark.parametrize("title", ["Powder Kiss Lipstick", "Powder Kiss Liquid Lipcolour",
                                    "Powder Kiss Velvet Blur Slim Stick", "Strobe Cream"])
def test_claude_powder_kiss_marketing_negatives_are_preserved(title):
    rec = record(title=title, product_type=None)
    assert rec["pdp"]["category_path"] is None
    assert rec["pdp"]["category_resolution_status"] == "unresolved"
    assert rec["pdp"]["category_confidence"] == feed.CATEGORY_CONFIDENCE_FEED_DEFAULT


@pytest.mark.parametrize("ptype", ["Blush & Highlighter", "Bronzer & Blush", "Lip & Cheek"])
def test_ambiguous_product_type_does_not_force_a_leaf(ptype):
    assert feed._resolve_category(product_type=ptype, title="Powder Kiss", flag_path="beauty/makeup") == (
        "beauty/makeup", feed.CATEGORY_CONFIDENCE_FEED_DEFAULT,
    )


def test_product_type_can_correct_a_coarse_storefront_shelf():
    assert feed._resolve_category(product_type="Lipstick", title="Powder Kiss", flag_path="beauty/skincare") == (
        "beauty/makeup/lip/lipstick", feed.CATEGORY_CONFIDENCE_MERCHANT_TYPE,
    )
    assert feed._resolve_category(product_type="Brush", title="Artistool", flag_path="beauty/skincare")[0] == "beauty/tools/brush"
    # A deliberate leaf flag is stronger than an area-wide default.
    assert feed._resolve_category(product_type="Lipstick", title="x", flag_path="beauty/skincare/moisturize/cream")[0] == "beauty/skincare/moisturize/cream"


@pytest.mark.parametrize("title", ["Layering Fit Brush", "Artistool Foundation Brush #101", "Powder Brush", "Makeup Brushes"])
@pytest.mark.parametrize("ptype", [None, "MAKEUP", "SKIN CARE"])
def test_narrow_tool_titles_correct_the_measured_missha_error(title, ptype):
    rec = record(title=title, product_type=ptype, category="beauty/skincare")
    assert rec["pdp"]["category_path"] == "beauty/tools/brush"
    assert rec["pdp"]["category_confidence"] == feed.CATEGORY_CONFIDENCE_EXPLICIT_TITLE
    assert feed.product_category_path(title=title, product_type=ptype, fallback="beauty/skincare") == rec["pdp"]["category_path"]


@pytest.mark.parametrize("title", ["Brush-On SPF Powder", "Foundation with Brush", "Mascara Includes Brush",
                                    "Brush Cleaner", "Brush Cleansing Oil", "Airbrush Foundation",
                                    "Liquid Lipstick with Applicator Brush", "Soap for Brush", "Mascara and Brush",
                                    "Foundation + Brush", "Powder/Brush", "Foundation & Brush"])
def test_formula_titles_cannot_enter_the_tool_exception(title):
    rec = record(title=title, product_type="MAKEUP", category="beauty/skincare")
    assert rec["pdp"]["category_path"] is None
    assert rec["pdp"]["category_resolution_status"] == "unresolved"
    assert rec["pdp"]["category_confidence"] == feed.CATEGORY_CONFIDENCE_FEED_DEFAULT


def test_specific_formula_type_is_not_overruled_by_a_tool_word():
    assert feed._resolve_category(product_type="Foundation", title="Foundation Brush", flag_path="beauty")[0] == "beauty/makeup/face/foundation"


def test_flag_case_and_canonicalization_are_not_lost():
    assert feed._resolve_category(product_type="Lipstick", title="x", flag_path="BEAUTY/MAKEUP/")[0] == "beauty/makeup/lip/lipstick"
    assert feed._resolve_category(product_type="Cleanser", title="x", flag_path="beauty/skincare/cleanser")[0] == "beauty/skincare/cleanse/cleanser"


@pytest.mark.parametrize("raw,want", [(None,None),("not-a-number",None),(float("nan"),None),
                                      (0.0,0.0),("0.85",0.85),(7,1.0),(-3,0.0)])
def test_claude_confidence_coercion_contract(raw, want):
    assert ing._category_confidence_or_none(raw) == want


def test_manual_records_without_lane_confidence_retain_legacy_default():
    rec = record()
    rec["pdp"].pop("category_confidence")
    row = ing._build_pdp_insert(pdp_payload=ing._build_pdp_payload(rec), offers=rec["offers"], source_jsonl=None,
                                seller={"merchant_id":"m","platform":"shopify"})
    assert row["category_confidence"] == ing.DEFAULT_CATEGORY_CONFIDENCE


@pytest.mark.parametrize("observation_index", [0, 1, 2])
def test_captured_merchant_type_contradictions_cannot_publish_a_specific_category(observation_index):
    import json
    from pathlib import Path
    from scripts.plan_curated_category_repair import plan_category_repair
    from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan

    observations = json.loads((Path(__file__).parents[1] / "fixtures/category_type_conflicts_public_observations.json").read_text())
    assert len(observations) == 3
    for observed in [observations[observation_index]]:
        for shelf in ("beauty", "beauty/makeup"):
            rec = feed.shopify_product_to_record(observed["product"], domain=observed["domain"],
                                                category_path=shelf, currency="USD", emit_native_variants=True)
            assert rec["pdp"]["category_path"] is None
            assert rec["pdp"]["category_resolution_status"] == "unresolved"
            planned = ing.ingest_validated_jsonl([rec])
            assert inspect_primary_plan(planned)["status"] == "blocked"
            assert planned["pdps"][0]["pdp_lifecycle_stage"] == "draft"
            assert plan_category_repair([{"product_key": "review", "title": observed["product"]["title"],
                                         "product_type": observed["product"]["product_type"],
                                         "category_path": shelf}])["changes"] == []


@pytest.mark.parametrize("ptype", ["Lip Gloss/Oil", "Lip Gloss / Oil", "Lip Gloss (Lip Tint)", "Lip Gloss / Lip Tint"])
def test_explicit_competing_lip_types_cannot_pass_as_one_regex_hit(ptype):
    rec = record(title="Dewy Glow Lip Gloss", product_type=ptype)
    assert rec["pdp"]["category_resolution_status"] == "unresolved"


@pytest.mark.parametrize("title,ptype,want", [
    ("Convertible Color Dual Lip & Cheek Cream", "Blush", "beauty/makeup/face/blush"),
    ("Liquid Eyeliner & Cream Blush Duo", "Eye Liner", "beauty/makeup/eye/eyeliner"),
    ("Eye Liner & Cream Blush Duo", "Eye Liner", "beauty/makeup/eye/eyeliner"),
    ("Liquid Eye Liner + Cheek Palette", "Blush", "beauty/makeup/face/blush"),
    ("Liquid Eyeliner & Cream Blush Duo", "Blush", "beauty/makeup/face/blush"),
    ("Get Real Serum Concealer", "Face Concealer", "beauty/makeup/face/concealer"),
    ("Collagen Sun Serum", "chemical-sunscreen", "beauty/skincare/sun/sunscreen"),
    ("Blush", "Eye Liner", "beauty/makeup/eye/eyeliner"),
    ("Stay All Day Eyeliner in Blush", "Eye Liner", "beauty/makeup/eye/eyeliner"),
    ("Liquid Eyeliner", "Eye Liner", "beauty/makeup/eye/eyeliner"),
])
def test_specific_types_and_legitimate_hybrids_keep_their_supported_category(title, ptype, want):
    assert record(title=title, product_type=ptype)["pdp"]["category_path"] == want


def test_repeating_wrong_leaf_cannot_override_direct_product_contradiction():
    rec = record(title="Stay All Day Chroma-Flash Liquid Eye Liner", product_type="Blush", category="beauty/makeup/face/blush")
    assert rec["pdp"]["category_resolution_status"] == "unresolved"


def test_cross_sell_body_does_not_reclassify_the_actual_product():
    product = {"title": "Liquid Eyeliner", "product_type": "Eye Liner", "handle": "liner", "vendor": "Stila",
               "body_html": "<p>Complete the look with our Cream Blush and Cheek Duo.</p>",
               "variants": [{"price": "20", "available": True}]}
    rec = feed.shopify_product_to_record(product, domain="stilacosmetics.com", category_path="beauty", currency="USD")
    assert rec["pdp"]["category_path"] == "beauty/makeup/eye/eyeliner"
