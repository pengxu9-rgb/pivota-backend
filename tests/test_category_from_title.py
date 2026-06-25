"""Title-based category extraction — so store-less brands (no product_type, no
catalog, no attribute graph) still get NON-BRANDED discovery queries instead of
only branded name queries."""
from __future__ import annotations

from services.agent_center_bd_report_service import (
    _category_from_title,
    _category_for_unbranded_prompts,
)


def test_extracts_hair_category_from_descriptive_title():
    assert _category_from_title("Anuko Bond & Repair Hair Oil", "Anuko") == "hair oil"
    assert (
        _category_from_title(
            "Anuko Nourishing Hair Butter Damaged Hair Treatment #Shea Butter & Green Tea 200ml",
            "Anuko",
        )
        == "hair butter"
    )


def test_strips_size_and_variant_noise_and_brand():
    # size tokens + after-# variant + brand are dropped before head-noun search.
    assert _category_from_title("Glow Recipe Watermelon Glow Toner 150ml", "Glow Recipe") == "toner"


def test_returns_empty_for_no_confident_category():
    # Non-beauty / no head noun -> "" (caller emits no discovery queries; same as
    # before — no regression, no garbage category).
    assert _category_from_title("Acme Triple Shine Grape", "Acme") == ""
    assert _category_from_title("Some Random Gadget Pro Max", "Acme") == ""


def test_resolver_uses_title_when_no_product_type_or_graph():
    # The Anuko gap: product_type=None, attribute_graph empty -> previously "" ->
    # only branded queries. Now the title yields a category -> discovery queries.
    cat = _category_for_unbranded_prompts(
        {"title": "Anuko Bond & Repair Hair Oil", "vendor": "Anuko"}, "", {}
    )
    assert cat == "hair oil"


def test_resolver_prefers_real_product_type_over_title():
    # A clean structured product_type still wins; the title fallback is last.
    cat = _category_for_unbranded_prompts(
        {"title": "Anuko Bond & Repair Hair Oil", "vendor": "Anuko"},
        "hair serum",
        {},
    )
    assert cat == "hair serum"
