"""URL-audit synthetic SKU-context registry.

The make-or-break invariant for routing URL audits through the per-SKU
pipeline: a synthetic context (built from a pasted URL, no synced catalog)
must survive run_brand_report's reset_sku_context_cache() so the report
assembly loop — which re-reads each ctx AFTER that reset — sees the same
context the probe fan-out used. If it doesn't, every per-product report
renders null scores + zero citations even though probes ran (and cost money).
"""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

MID = "merch_test"
ITEM = {
    "sku_key": "urlwedge:abc123",
    "product_key": "urlwedge:abc123",
    "title": "Triple Shine Grape Serum",
    "raw_title": "Triple Shine Grape Serum 30ml",
    "vendor": "Chydan",
    "product_type": "Face Serum",
    "pdp_url": "https://chydan.com/products/triple-shine-grape",
    "attributes_raw": {"ingredients": ["grape", "niacinamide"]},
}


def _fresh():
    import services.agent_center_bd_report_service as svc
    svc._SYNTHETIC_SKU_CONTEXTS.clear()
    svc.reset_sku_context_cache()
    return svc


def test_build_synthetic_sku_context_shape():
    svc = _fresh()
    ctx = svc.build_synthetic_sku_context(ITEM, MID)
    assert ctx["sku_key"] == "urlwedge:abc123"
    assert ctx["missing_inputs"] == ["catalog_skus"]
    assert ctx["synthetic_url_audit"] is True
    product = ctx["product"]
    # product_key MUST be set, else build_per_sku_report's null-guard skips
    # citation scoring entirely.
    assert product["product_key"] == "urlwedge:abc123"
    # Probe anchor reads canonical_url (not pdp_url) — both point at the URL.
    assert product["canonical_url"] == ITEM["pdp_url"]
    assert product["pdp_url"] == ITEM["pdp_url"]
    assert product["title"] == "Triple Shine Grape Serum"
    assert product["vendor"] == "Chydan"
    assert product["product_type"] == "Face Serum"


def test_synthetic_context_survives_cache_reset():
    svc = _fresh()
    keys = svc.register_synthetic_sku_contexts([ITEM], MID)
    assert keys == ["urlwedge:abc123"]

    async def _check():
        # 1. Resolves from the registry (no catalog row needed).
        ctx1 = await svc.load_sku_context("urlwedge:abc123", MID)
        assert ctx1["product"]["product_key"] == "urlwedge:abc123"
        assert ctx1["product"]["canonical_url"] == ITEM["pdp_url"]

        # 2. THE invariant: after the per_sku branch wipes the ctx cache, the
        #    assembly loop's re-read STILL returns the synthetic ctx.
        svc.reset_sku_context_cache()
        ctx2 = await svc.load_sku_context("urlwedge:abc123", MID)
        assert ctx2["product"]["product_key"] == "urlwedge:abc123"
        assert ctx2["missing_inputs"] == ["catalog_skus"]
        assert ctx2.get("synthetic_url_audit") is True

    asyncio.run(_check())


def test_clear_synthetic_contexts():
    svc = _fresh()
    svc.register_synthetic_sku_contexts([ITEM], MID)
    svc.clear_synthetic_sku_contexts(["urlwedge:abc123"], MID)
    assert ("urlwedge:abc123", MID) not in svc._SYNTHETIC_SKU_CONTEXTS


def test_non_null_guard_path_for_synthetic():
    """A synthetic ctx has missing_inputs BUT a product_key, so
    build_per_sku_report's all-null guard (missing_inputs AND not product_key)
    must NOT trigger — citations stay scorable."""
    svc = _fresh()
    ctx = svc.build_synthetic_sku_context(ITEM, MID)
    product = svc._get_product(ctx)
    guard_triggered = bool(ctx.get("missing_inputs")) and not product.get("product_key")
    assert guard_triggered is False


def test_synthetic_enrichment_from_page():
    """Lever 2: the synthetic ctx is enriched from the fetched page —
    product.description + product_enrichment (summary_short/bullet_points/
    topic_tags) that the base query specs + content scoring read."""
    svc = _fresh()
    item = {
        "sku_key": "urlwedge:e", "product_key": "urlwedge:e",
        "title": "Glow Serum", "pdp_url": "https://x.com/p",
        "attributes_raw": {
            "description": "A vitamin C serum for glowing skin. Fragrance-free and vegan.",
            "body_html": "<ul><li>Vitamin C 10%</li><li>Fragrance-free</li></ul>",
            "tags": ["serum", "vitamin-c", "vegan"],
        },
    }
    ctx = svc.build_synthetic_sku_context(item, MID)
    assert ctx["product"]["description"].startswith("A vitamin C serum")
    enr = ctx["product_enrichment"]
    assert enr["topic_tags"] == ["serum", "vitamin-c", "vegan"]
    assert "Vitamin C 10%" in enr["bullet_points"]
    assert "Fragrance-free" in enr["bullet_points"]
    assert enr["summary_short"].startswith("A vitamin C serum")


def test_synthetic_enrichment_empty_when_no_attrs():
    svc = _fresh()
    ctx = svc.build_synthetic_sku_context(
        {"sku_key": "urlwedge:f", "product_key": "urlwedge:f", "title": "X",
         "pdp_url": "https://x.com/p"}, MID)
    assert "product_enrichment" not in ctx  # nothing to enrich -> omitted
