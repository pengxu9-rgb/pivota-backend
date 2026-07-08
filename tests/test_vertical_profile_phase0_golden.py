"""Phase-0 multi-vertical architecture — golden regression guard + unit tests.

The golden guard re-runs the DETERMINISTIC beauty-audit layers (sidewalk
queries, competitor filtering, category/head-noun resolution, base query specs,
_vertical_for) over a fixed beauty fixture set and asserts the output is
byte-identical to a committed snapshot. The snapshot was captured from the
pre-refactor code (`git stash`) and verified equal to the post-refactor code, so
it is the proof that migrating the beauty constants into the beauty
`VerticalProfile` changed no beauty output. It gates the whole phase: if a future
change diverges here, the refactor is wrong, not the fixture.

Scope note: this guard covers the DETERMINISTIC layers only. The LLM-generated
strategic_brief is a Phase-1 concern (its leakage needs a mocked-LLM snapshot
test, not a byte-golden) and is intentionally out of scope here.
"""
import json
from pathlib import Path

import pytest

import services.agent_center_bd_report_service as R
from services.competitor_brand_filter import (
    filter_competitor_brands,
    is_ingredient_or_category_type,
)
from services.sku_sidewalk import build_sku_attribute_graph, generate_sidewalk_query_specs
from services.vertical_profiles import get_profile, resolve_vertical

GOLDEN_PATH = Path(__file__).parent / "golden" / "phase0_beauty_deterministic.json"

# --- Beauty fixtures (ANUKO-style supplements + K-beauty skincare + URL audit) ---
FIXTURES = [
    {
        "name": "anuko_marine_collagen",
        "product": {
            "title": "ANUKO Marine Collagen Peptides",
            "brand": "ANUKO",
            "vendor": "ANUKO",
            "product_type": "Supplements",
            "category": "Health/Supplements",
            "category_path": "beauty/supplement/collagen",
            "attributes_raw": {
                "tags": ["collagen", "marine collagen", "anti-aging", "vegan"],
                "form": "powder",
                "benefit": "skin elasticity",
            },
        },
        "sku": {"title": "30 Sticks, 1-Month Supply"},
    },
    {
        "name": "cosrx_snail_essence",
        "product": {
            "title": "COSRX Advanced Snail 96 Mucin Power Essence",
            "brand": "COSRX",
            "vendor": "COSRX",
            "product_type": "Beauty/Skincare/Essence",
            "category": "Skincare",
            "category_path": "beauty/skincare/essence",
            "attributes_raw": {
                "tags": ["snail mucin", "hydrating", "fragrance-free", "essence"],
                "skin_type": "all",
            },
        },
        "sku": {"title": "100ml"},
    },
    {
        "name": "urlaudit_storeless_hair_oil",
        "product": {
            "title": "Olaplex Bond & Repair Hair Oil",
            "brand": "Olaplex",
            "vendor": "Olaplex",
            "product_type": None,
            "category": None,
            "category_path": None,
            "attributes_raw": {"tags": ["damaged hair", "shine", "heat protectant"]},
        },
        "sku": {"title": "Default"},
    },
    {
        "name": "melatonin_gummies_no_type",
        "product": {
            "title": "Sleep Well Melatonin Gummies",
            "brand": "Sleep Well",
            "vendor": "Sleep Well",
            "product_type": "",
            "category": "",
            "category_path": "",
            "attributes_raw": {"tags": ["melatonin", "sleep", "gummies"]},
        },
        "sku": {"title": "60 count"},
    },
    {
        "name": "noisy_variant_title",
        "product": {
            "title": "Triple Shine Grape",
            "brand": "Vitagummies",
            "vendor": "Vitagummies",
            "product_type": None,
            "category": None,
            "category_path": None,
            "attributes_raw": {"tags": ["grape", "shine"]},
        },
        "sku": {"title": "Grape"},
    },
]

COMPETITOR_NAMES = [
    "Magnesium", "Thorne", "Vital Proteins", "Ashwagandha", "Coupang",
    "The Ordinary", "Vitamin D", "hyaluronic acid", "Olive Young", "Sephora",
    "Ancient Nutrition", "Probiotics", "magnesium glycinate", "Amazon",
]

TITLE_CASES = [
    "Bond & Repair Hair Oil", "Triple Shine Grape", "Vitamin C Brightening Serum",
    "Deep Moisture Face Cream", "Nourishing Scalp Treatment", "Matte Lipstick Ruby",
    "Daily Multivitamin Gummies", "Random Widget Thing",
]
NOISY_CASES = ["glow", "grape", "hair oil", "serum", "orange", "sunscreen"]


def _capture():
    """Run every deterministic beauty layer with default (beauty) behavior.

    Uses the ORIGINAL call signatures (no `profile=` kwarg) so the beauty default
    path is exercised — the exact path a beauty audit takes."""
    out = {}
    out["category_from_title"] = {t: R._category_from_title(t) for t in TITLE_CASES}
    out["noisy_prompt_category"] = {t: R._noisy_prompt_category(t) for t in NOISY_CASES}

    unbranded, vertical, base_specs, attr_graphs, sidewalk = {}, {}, {}, {}, {}
    for fx in FIXTURES:
        product = fx["product"]
        pt = str(product.get("product_type") or "")
        graph = build_sku_attribute_graph(product)
        attr_graphs[fx["name"]] = graph
        unbranded[fx["name"]] = R._category_for_unbranded_prompts(product, pt, graph)
        vertical[fx["name"]] = R._vertical_for(product)
        specs, title, cat = R._build_per_sku_base_query_specs(
            {"product": product, "sku": fx["sku"]}
        )
        base_specs[fx["name"]] = {"specs": specs, "title": title, "category": cat}
        sw = generate_sidewalk_query_specs(graph, title=product["title"], product_type=pt, n=12)
        sidewalk[fx["name"]] = [
            {"query": s.get("query"), "attribute_basis": s.get("attribute_basis")} for s in sw
        ]

    out["unbranded_category"] = unbranded
    out["vertical_for"] = vertical
    out["attribute_graph"] = attr_graphs
    out["base_query_specs"] = base_specs
    out["sidewalk_query_specs"] = sidewalk
    out["filter_competitor_brands"] = filter_competitor_brands(COMPETITOR_NAMES)
    out["is_ingredient_or_category_type"] = {
        n: is_ingredient_or_category_type(n) for n in COMPETITOR_NAMES
    }
    out["competitor_is_brandlike"] = {n: R._competitor_is_brandlike(n) for n in COMPETITOR_NAMES}
    # Round-trip through JSON so the comparison matches the committed snapshot
    # exactly (tuples -> lists, key sorting, default=str).
    return json.loads(json.dumps(out, sort_keys=True, default=str))


def test_beauty_deterministic_layers_byte_identical_golden():
    expected = json.loads(GOLDEN_PATH.read_text())
    actual = _capture()
    # Per-section diff first for a readable failure, then whole-object equality.
    for section in sorted(expected):
        assert actual.get(section) == expected[section], (
            f"golden drift in section '{section}' — beauty output changed; "
            f"the refactor is wrong, not the fixture"
        )
    assert actual == expected


# --------------------------- resolver unit tests --------------------------- #

@pytest.mark.parametrize("product_type,expected", [
    ("Supplements", "beauty"),
    ("Beauty/Skincare", "beauty"),
    ("Wellness Vitamins", "beauty"),
    ("Supplement Collagen Tablets", "beauty"),   # 'supplement' keyword -> beauty
    ("Womens Wellness Gummies", "beauty"),   # fashion token present but never demotes beauty
    ("Wireless Headphones", "electronics"),
    ("Bone Conduction Earphones", "electronics"),
    ("True Wireless Earbuds", "electronics"),
    ("ANC Over-Ear Headphones", "electronics"),
    ("Womens Dress", "fashion"),
    ("Running Sneaker", "fashion"),
    ("Random Widget", "other"),
    ("", "other"),
])
def test_resolver_category_tier(product_type, expected):
    assert resolve_vertical({"product_type": product_type}) == expected


def test_incidental_weak_beauty_token_loses_to_clear_audio():
    # The scope-doc litmus: a stray "wellness" token must not make a clearly-audio
    # SKU resolve beauty.
    assert resolve_vertical({"product_type": "Wellness Earbuds"}) == "electronics"
    assert resolve_vertical({"product_type": "Bone Conduction Wellness Headphones"}) == "electronics"


def test_anc_substring_does_not_false_fire_on_beauty():
    # 'anc' must not fire inside 'fragrance'/'radiance'/'balance'.
    assert resolve_vertical({"product_type": "Fragrance Balance Radiance Serum"}) != "electronics"


def test_supplement_tablet_never_pulled_to_electronics():
    # 'tablet(s)' is a supplement dosage form (Pivota's core), not a computing
    # tablet — it is deliberately excluded from the electronics keyword set, so a
    # supplement tablet is never mis-resolved to electronics.
    assert resolve_vertical({"product_type": "Collagen Tablets"}) != "electronics"
    assert resolve_vertical({"product_type": "Vitamin Tablets"}) == "beauty"


def test_title_tier_resolves_storeless_beauty():
    assert resolve_vertical({"product_type": ""}, title="Marine Collagen") == "beauty"
    assert resolve_vertical({"product_type": ""}, title="Sleep Melatonin Gummies") == "beauty"
    # Title tier only reached when the category tier finds nothing.
    assert resolve_vertical({"product_type": "Wireless Earbuds"}, title="Marine Collagen") == "electronics"


def test_override_wins():
    assert resolve_vertical({"product_type": "Earbuds"}, override="beauty") == "beauty"
    assert resolve_vertical({}, override="electronics_audio") == "electronics"
    assert resolve_vertical({}, override="generic") == "other"


# --------------------------- registry unit tests --------------------------- #

def test_generic_is_default_not_beauty():
    # Unknown vertical -> generic (NOT beauty). Beauty is a profile, not the fallback.
    assert get_profile("other").name == "generic"
    assert get_profile("nonsense").name == "generic"
    assert get_profile(None).name == "generic"
    assert get_profile("fashion").name == "generic"  # no fashion profile in Phase 0


def test_electronics_profile_has_phase1_content_and_is_not_beauty():
    prof = get_profile("electronics")
    assert prof.name == "electronics_audio"
    # Phase 1 populated audio content...
    assert "headphones" in prof.category_head_nouns
    assert "earbuds" in prof.category_head_nouns
    assert "earbuds" in prof.competitor_form_tokens  # type-name drop
    assert "anc" in prof.competitor_form_tokens
    assert "newegg" in prof.retailer_tokens
    assert prof.health_sensitive is False           # NOT swapped to elec tokens
    assert "rtings.com" in prof.authority_hosts
    # ...but it is NOT beauty and keeps no beauty-style category fallbacks.
    assert prof.category_fallbacks == ()            # unknown -> "", never "beauty supplement"
    assert prof.competitor_ingredient_tokens == frozenset()  # electronics has no "ingredients"
    assert "serum" not in prof.category_head_nouns
    assert "sephora" not in prof.retailer_tokens


def test_electronics_competitor_filter_drops_type_names_keeps_brands():
    from services.competitor_brand_filter import filter_competitor_brands
    prof = get_profile("electronics")
    kept = filter_competitor_brands(
        ["wireless earbuds", "Bose", "noise cancelling headphones", "Shokz",
         "bone conduction earphones", "Sony WH-1000XM5"],
        ingredient_tokens=prof.competitor_ingredient_tokens,
        form_tokens=prof.competitor_form_tokens,
    )
    assert kept == ["Bose", "Shokz", "Sony WH-1000XM5"]


def test_electronics_head_noun_and_retailer_resolution():
    import services.agent_center_bd_report_service as R
    prof = get_profile("electronics")
    assert R._category_from_title(
        "Shokz OpenRun Pro Bone Conduction Headphones", brand="Shokz", profile=prof
    ) == "headphones"
    assert R._competitor_is_brandlike("Bose", profile=prof) is True
    assert R._competitor_is_brandlike("wireless earbuds", profile=prof) is False
    assert R._competitor_is_brandlike("Newegg", profile=prof) is False
    # beauty default unchanged
    assert R._competitor_is_brandlike("Coupang") is False
    assert R._competitor_is_brandlike("Thorne") is True


def test_beauty_profile_carries_migrated_constants():
    prof = get_profile("beauty")
    assert prof.name == "beauty"
    assert "serum" in prof.category_head_nouns
    assert prof.category_fallbacks[0][1] == "beauty supplement"
    assert "sephora" in prof.retailer_tokens
    assert "collagen" in prof.competitor_ingredient_tokens
    assert "supplement" in prof.competitor_form_tokens
    assert prof.noisy_prompt_tokens == frozenset({"glow", "grape", "jelly", "orange", "shine"})


def test_generic_profile_kills_beauty_supplement_leak():
    # The URL-audit collapse: a store-less product with a supplement token resolves
    # to "beauty supplement" ONLY under the beauty profile; the generic profile
    # (unknown vertical) has no fallback and returns "".
    product = {"title": "Marine Collagen Powder", "attributes_raw": {}}
    assert R._category_for_unbranded_prompts(product, "", {}) == "beauty supplement"
    generic = get_profile("electronics")
    assert R._category_for_unbranded_prompts(product, "", {}, profile=generic) == ""
