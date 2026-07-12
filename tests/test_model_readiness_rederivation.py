"""Tests for the model_readiness_score re-derivation (Fix Plan G — T2).

The prior formula read ONLY the L3 enrichment overlay (summary_short/bullets/L3
attributes), which is unpopulated for ~all of the catalog, pinning readiness at
~0 (prod: avg 2.5). These pin the re-derivation:
  - readiness now reflects the BASE catalog structure that exists (a fully-
    structured product is no longer stuck near 0);
  - a resolved vertical and a populated llm_attributes payload lift it further;
  - content_quality_score and the content `components` are UNCHANGED;
  - the llm_attributes field-counter tolerates the versioned envelope, a JSON
    string, and the legacy grounded-span cache.
"""

from __future__ import annotations

import json

from services.product_quality_service import (
    _llm_attributes_field_count,
    build_quality_payload,
    preview_quality,
)


def _structured_payload(**over):
    """A product with full BASE structure but NO L3 overlay — the exact shape
    that scored ~0 under the old formula."""
    base = {
        "title_local": "COSRX Advanced Snail 96 Mucin Power Essence",
        "description_local": (
            "A lightweight hydrating essence with 96% snail mucin to repair and "
            "plump dry, dull skin. Absorbs fast, no sticky finish. 100ml."
        ),
        "price_local_value": 21.0,
        "main_image_url": "https://img/1.jpg",
        "image_list": ["https://img/1.jpg"],
        "brand": "COSRX",
        "global_category_id": "beauty/skincare/essence",
        # no summary_short / bullet_points / usage_scenarios (L3 empty)
    }
    base.update(over)
    return base


def _readiness(result):
    return result["model_readiness_score"]


def test_structured_product_no_longer_scores_near_zero():
    """The core fix: a fully-structured product with an empty L3 overlay used to
    score ~0 for readiness. It must now score materially, from base structure."""
    result = preview_quality(_structured_payload())
    # base structure alone (desc+image+title+brand/category+price, no vertical, no
    # attributes) => 0.20*desc + 0.15*img + 0.10*title + 0.10*bc + 0.10*price.
    assert _readiness(result) > 40.0
    # content_quality unchanged and independent
    assert result["content_quality_score"] > 0.0
    assert any(c["name"] == "description" for c in result["readiness_components"])


def test_resolved_vertical_and_attributes_lift_readiness():
    bare = preview_quality(_structured_payload())
    with_vertical = preview_quality(_structured_payload(resolved_vertical="beauty"))
    with_depth = preview_quality(
        _structured_payload(resolved_vertical="beauty", llm_attribute_field_count=5)
    )
    assert _readiness(with_vertical) > _readiness(bare)
    assert _readiness(with_depth) > _readiness(with_vertical)
    # full structure + resolved vertical + saturated attribute depth -> high
    assert _readiness(with_depth) > 90.0


def test_vertical_other_does_not_count_as_resolved():
    other = preview_quality(_structured_payload(resolved_vertical="other"))
    beauty = preview_quality(_structured_payload(resolved_vertical="beauty"))
    assert _readiness(other) < _readiness(beauty)


def test_attribute_depth_saturates_at_five_fields():
    five = preview_quality(_structured_payload(llm_attribute_field_count=5))
    ten = preview_quality(_structured_payload(llm_attribute_field_count=10))
    assert _readiness(five) == _readiness(ten)  # min(n/5,1) saturates


def test_empty_product_still_scores_low():
    result = preview_quality({"title_local": "", "description_local": ""})
    assert _readiness(result) == 0.0


def test_content_quality_score_unchanged_by_readiness_change():
    """Guard: the readiness re-derivation must not perturb content_quality_score
    (a different, independent axis)."""
    result = preview_quality(_structured_payload(resolved_vertical="beauty",
                                                 llm_attribute_field_count=5))
    # content_quality ignores resolved_vertical/llm_attribute_field_count.
    baseline = preview_quality(_structured_payload())
    assert result["content_quality_score"] == baseline["content_quality_score"]


# --------------------------------------------------------------------------- #
# build_quality_payload threads the new signals
# --------------------------------------------------------------------------- #

def test_build_quality_payload_threads_vertical_and_attribute_count():
    payload = build_quality_payload({
        "title": "Serum", "description": "x" * 60, "vendor": "B",
        "product_type": "Serum",
        "resolved_vertical": "beauty",
        "llm_attributes": {
            "schema_version": "structural_depth.beauty.v1",
            "attributes": {"volume": "50 ml", "concerns": ["dryness"],
                           "key_ingredients": [{"label": "Niacinamide"}]},
        },
    })
    assert payload["resolved_vertical"] == "beauty"
    assert payload["llm_attribute_field_count"] == 3


# --------------------------------------------------------------------------- #
# _llm_attributes_field_count tolerance
# --------------------------------------------------------------------------- #

def test_field_count_versioned_envelope():
    env = {"schema_version": "structural_depth.beauty.v1", "vertical": "beauty",
           "generated_at": "t", "model": "m", "provenance": {"volume": "x"},
           "attributes": {"volume": "50 ml", "spf": 50, "concerns": [], "skin_type": None}}
    # volume + spf populated; concerns [] and skin_type None don't count
    assert _llm_attributes_field_count(env) == 2


def test_field_count_json_string():
    env = json.dumps({"attributes": {"volume": "50 ml", "texture": "gel"}})
    assert _llm_attributes_field_count(env) == 2


def test_field_count_legacy_grounded_span_cache():
    legacy = {"source_hash": "abc",
              "attributes": [{"class_name": "ingredient", "value": "IP68", "span": "..."},
                             {"class_name": "format", "value": "", "span": "x"}]}
    # one grounded span carries a value; the empty-value one doesn't count
    assert _llm_attributes_field_count(legacy) == 1


def test_field_count_empty_and_garbage():
    assert _llm_attributes_field_count(None) == 0
    assert _llm_attributes_field_count("not json") == 0
    assert _llm_attributes_field_count({}) == 0
    assert _llm_attributes_field_count({"attributes": {}}) == 0
