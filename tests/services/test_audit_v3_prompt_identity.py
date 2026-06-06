"""Quality regression: per-SKU probe prompts must name the product identity.

Background
----------
The second live Ownist run produced prompts built from the bare variant/SKU
label ("14 Servings, 2-Week Routine") instead of the brand + product identity
("Ownist Triple Shine Grape"), so Gemini returned sparse/irrelevant citations.
`_build_per_sku_audit_query_specs` preferred `sku.title` (variant label) over
`product.title` and never prefixed the brand. This test pins the corrected
behavior.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.agent_center_bd_report_service import _build_per_sku_audit_query_specs


def _queries(sku_ctx, n=40):
    return [q for q, _axis in _build_per_sku_audit_query_specs(sku_ctx, n)]


def test_prompts_use_enrichment_title_override_when_present() -> None:
    # Increment C: when the enrichment pipeline has curated a clean name, probes
    # must use it over the (worse) catalog product/sku titles.
    sku_ctx = {
        "product": {"title": "TС-GRD-30", "brand": "Ownist", "product_type": "beauty supplement"},
        "sku": {"title": "Garden Gift Set", "sku": "v9"},
        "product_enrichment": {"title_override": "Ownist Triple Collagen Garden Edition"},
    }
    qs = _queries(sku_ctx)
    assert any("Ownist Triple Collagen Garden Edition" in q for q in qs)
    # The poor catalog title and the variant label are not the probe identity.
    assert not any("TС-GRD-30" in q for q in qs)
    assert "where can I buy Garden Gift Set" not in qs


def test_prompts_name_brand_product_identity_not_variant_label() -> None:
    sku_ctx = {
        "product": {"title": "Triple Shine Grape", "brand": "Ownist",
                    "product_type": "beauty supplement"},
        "sku": {"title": "14 Servings, 2-Week Routine", "sku": "42327845699767",
                "source_variant_id": "42327845699767"},
    }
    qs = _queries(sku_ctx)

    # Generic shopper prompts now name the full identity.
    assert "where can I buy Ownist Triple Shine Grape" in qs
    assert "best beauty supplement" in qs
    assert not any("shoppers considering Ownist Triple Shine Grape" in q for q in qs)

    # The opaque variant id must not leak into prompts.
    assert not any("42327845699767" in q for q in qs)

    # The human variant label is used WITH the identity (not standalone).
    assert any(
        "Ownist Triple Shine Grape" in q and "14 Servings, 2-Week Routine" in q
        for q in qs
    )

    # No prompt is just the bare variant label as the product name.
    assert "where can I buy 14 Servings, 2-Week Routine" not in qs


def test_identity_does_not_double_brand_when_title_already_branded() -> None:
    sku_ctx = {
        "product": {"title": "Ownist Triple Collagen Orange", "brand": "Ownist",
                    "product_type": "beauty supplement"},
        "sku": {"title": "30 Servings", "sku": "v2"},
    }
    qs = _queries(sku_ctx)
    assert not any("Ownist Ownist" in q for q in qs)
    assert "where can I buy Ownist Triple Collagen Orange" in qs


def test_falls_back_to_product_then_sku_when_no_product_title() -> None:
    # No product title -> fall back chain still yields a usable identity, never
    # crashes, and prefixes brand when available.
    sku_ctx = {
        "product": {"brand": "Ownist", "product_type": "supplement"},
        "sku": {"title": "Garden Gift Set"},
    }
    qs = _queries(sku_ctx)
    assert any("Ownist Garden Gift Set" in q for q in qs)
