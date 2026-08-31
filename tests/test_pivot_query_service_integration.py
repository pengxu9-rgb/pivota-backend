from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

import services.pivot_query_service as module
from models.catalog import PivotPaymentContext, PivotQueryRequest, PivotQuoteItem, PivotQuoteRequest


@pytest.mark.asyncio
async def test_build_canonical_items_skips_incentives_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_fetch_offer_incentives(*_args, **_kwargs):
        raise AssertionError("offer incentives should not be fetched when include_incentives is false")

    monkeypatch.setattr(module, "_fetch_offer_incentives", fail_fetch_offer_incentives)

    rows = [
        {
            "merchant_id": "merch_1",
            "merchant_name": "Demo Merchant",
            "merchant_primary_platform": "shopify",
            "product_key": "prod::1",
            "source_product_id": "111",
            "product_title": "Vitamin C Serum",
            "product_description": "Brightening serum",
            "brand": "Demo",
            "product_type": "serum",
            "category": "serum",
            "canonical_url": "https://merchant.example/products/111",
            "product_image_url": "https://merchant.example/image.jpg",
            "catalog_track": "internal_merchant",
            "truth_tier": "primary",
            "readiness_tier": "knowledge_ready",
            "source_system": "shopify_products_sync",
            "freshness_json": {"updated_at": "2026-03-28T00:00:00Z"},
            "product_updated_at": datetime.now(timezone.utc),
            "sku_key": "sku::1",
            "source_variant_id": "var_1",
            "sku": "SKU-1",
            "barcode": "BAR-1",
            "sku_title": "Vitamin C Serum 30ml",
            "visible_attributes": {"size": ["30ml"]},
            "visible_option_labels": [],
            "ingredient_ids": ["vitamin_c"],
            "sku_image_url": "https://merchant.example/image.jpg",
            "offer_id": "offer::1",
            "offer_catalog_track": "internal_merchant",
            "offer_truth_tier": "primary",
            "offer_readiness_tier": "knowledge_ready",
            "offer_mode": "merchant_checkout",
            "availability": "in_stock",
            "inventory_quantity": 12,
            "currency": "USD",
            "list_price": Decimal("32.00"),
            "merchant_effective_price": Decimal("28.00"),
            "estimated_best_price": Decimal("26.00"),
            "price_confidence": Decimal("1.0"),
            "offer_source_system": "shopify_products_sync",
            "offer_payload": {"product_id": "111", "variant_id": "var_1"},
        }
    ]

    items = await module._build_canonical_items(
        rows,
        query="vitamin c",
        payment_context=None,
        include_vertical_payload=False,
        include_incentives=False,
    )

    assert len(items) == 1
    assert items[0].offers[0].incentives == []
    assert items[0].offers[0].pricing.estimated_best_price == Decimal("26.00")


@pytest.mark.asyncio
async def test_search_pivot_catalog_skips_external_fetch_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_rows(**_kwargs):
        return []

    async def fake_build_items(*_args, **_kwargs):
        return []

    async def fail_fetch_external_seed_rows(**_kwargs):
        raise AssertionError("external rows should not be fetched when include_external is false")

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_fetch_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "fetch_external_seed_rows", fail_fetch_external_seed_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="vitamin c",
            limit=5,
            include_external=False,
            include_incentives=False,
        )
    )

    assert result.total == 0
    assert result.items == []


@pytest.mark.asyncio
async def test_canonical_entities_only_requires_sig_and_disables_direct_seed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    async def fake_fetch_rows(**kwargs):
        observed.update(kwargs)
        return []

    async def fake_build_items(*_args, **_kwargs):
        return []

    async def fail_fetch_external_seed_rows(**_kwargs):
        raise AssertionError("canonical catalog mode must not read direct external seeds")

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_fetch_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "fetch_external_seed_rows", fail_fetch_external_seed_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="ordinary",
            limit=5,
            include_external=True,
            canonical_entities_only=True,
            include_incentives=False,
        )
    )

    assert observed["require_signature"] is True
    assert result.items == []


@pytest.mark.asyncio
async def test_search_pivot_catalog_skips_external_fetch_when_canonical_results_fill_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_rows(**_kwargs):
        return [{"sku_key": "sku::1"}]

    async def fake_build_items(*_args, **_kwargs):
        return [
            module.PivotResultItem(
                merchant=module.MerchantNode(merchant_id="merch_1", merchant_name="Demo"),
                product=module.ProductNode(product_key="prod::1", title="Vitamin C Serum"),
                sku=module.SkuNode(sku_key="sku::1"),
                offers=[],
                catalog_track="internal_merchant",
                truth_tier="primary",
                readiness_tier="commerce_ready",
                freshness={},
                source_system="test",
                match_explanation={},
            )
            for _ in range(5)
        ]

    async def fail_fetch_external_seed_rows(**_kwargs):
        raise AssertionError("external rows should not be fetched when canonical items already fill the limit")

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_fetch_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "fetch_external_seed_rows", fail_fetch_external_seed_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="vitamin c",
            limit=5,
            include_external=True,
            include_incentives=False,
        )
    )

    assert result.total == 5


@pytest.mark.asyncio
async def test_search_pivot_catalog_uses_lightweight_external_fallback_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    async def fake_fetch_rows(**_kwargs):
        return []

    async def fake_build_items(*_args, **_kwargs):
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        observed.update(kwargs)
        return {
            "rows": [
                {
                    "external_product_id": "ext_1",
                    "title": "Vitamin C Complex Serum",
                    "domain": "naturium.com",
                    "canonical_url": "https://naturium.com/products/vitamin-c-complex-serum",
                    "destination_url": "https://naturium.com/products/vitamin-c-complex-serum",
                    "seed_data": {"brand": "Naturium"},
                }
            ]
        }

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_fetch_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "fetch_external_seed_rows", fake_fetch_external_seed_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="vitamin c serum",
            limit=5,
            include_external=True,
            include_incentives=False,
        )
    )

    assert result.total == 1
    assert observed["include_seed_data_text_match"] is False
    assert observed["include_total_count"] is False
    assert observed["use_required_terms_filter"] is False
    assert observed["required_terms"] is None
    assert observed["prefer_terms"] == ["vitamin", "serum"]


@pytest.mark.asyncio
async def test_search_pivot_catalog_skips_external_fallback_for_default_generic_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_rows(**_kwargs):
        return []

    async def fake_build_items(*_args, **_kwargs):
        return []

    async def fail_fetch_external_seed_rows(**_kwargs):
        raise AssertionError("default generic queries should not trigger beauty external fallback")

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_fetch_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "fetch_external_seed_rows", fail_fetch_external_seed_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="linen sheet set",
            limit=5,
            include_external=True,
            include_incentives=False,
        )
    )

    assert result.total == 0
    assert result.items == []


@pytest.mark.asyncio
async def test_search_pivot_catalog_keeps_external_fallback_for_fragrance_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    async def fake_fetch_rows(**_kwargs):
        return []

    async def fake_build_items(*_args, **_kwargs):
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        observed.update(kwargs)
        return {
            "rows": [
                {
                    "external_product_id": "ext_frag_1",
                    "title": "Café Rose Eau de Parfum",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/cafe-rose-edp",
                    "destination_url": "https://example.com/products/cafe-rose-edp",
                    "seed_data": {"brand": "Example"},
                }
            ]
        }

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_fetch_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "fetch_external_seed_rows", fake_fetch_external_seed_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="rose eau de parfum",
            limit=5,
            include_external=True,
            include_incentives=False,
        )
    )

    assert result.total == 1
    assert observed["include_seed_data_text_match"] is False
    assert observed["prefer_terms"] == ["rose", "eau", "de", "parfum"]


@pytest.mark.asyncio
async def test_search_pivot_catalog_keeps_external_fallback_for_sunscreen_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    async def fake_fetch_rows(**_kwargs):
        return []

    async def fake_build_items(*_args, **_kwargs):
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        observed.update(kwargs)
        return {
            "rows": [
                {
                    "external_product_id": "ext_sun_1",
                    "title": "Ultra Light Liquid Mineral Sunscreen SPF 30",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/mineral-sunscreen-spf-30",
                    "destination_url": "https://example.com/products/mineral-sunscreen-spf-30",
                    "seed_data": {"brand": "Example"},
                }
            ]
        }

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_fetch_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "fetch_external_seed_rows", fake_fetch_external_seed_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="sunscreen",
            limit=5,
            include_external=True,
            include_incentives=False,
        )
    )

    assert result.total == 1
    assert observed["include_seed_data_text_match"] is False
    assert observed["prefer_terms"] == ["sunscreen"]


@pytest.mark.asyncio
async def test_search_pivot_catalog_uses_broad_external_text_scan_when_lightweight_stage_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_calls = []

    async def fake_fetch_rows(**_kwargs):
        return []

    async def fake_build_items(*_args, **_kwargs):
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        observed_calls.append(dict(kwargs))
        if len(observed_calls) == 1:
            return {"rows": []}
        return {
            "rows": [
                {
                    "external_product_id": "ext_2",
                    "title": "Barrier Moisturizer",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/barrier-moisturizer",
                    "destination_url": "https://example.com/products/barrier-moisturizer",
                    "seed_data": {"brand": "Example"},
                    "brand_term_hit": 2,
                }
            ]
        }

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_fetch_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "fetch_external_seed_rows", fake_fetch_external_seed_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="barrier moisturizer fragrance free",
            limit=5,
            include_external=True,
            include_incentives=False,
        )
    )

    assert result.total == 1
    assert len(observed_calls) == 2
    assert observed_calls[0]["include_seed_data_text_match"] is False
    assert observed_calls[0]["use_required_terms_filter"] is False
    assert observed_calls[1]["include_seed_data_text_match"] is True
    assert observed_calls[1]["use_required_terms_filter"] is False


@pytest.mark.asyncio
async def test_sort_items_prefers_external_relevance_before_price() -> None:
    def build_item(
        title: str,
        price: str,
        relevance: float,
        *,
        source_order: int = 999999,
    ) -> module.PivotResultItem:
        return module.PivotResultItem(
            merchant=module.MerchantNode(merchant_name="Demo"),
            product=module.ProductNode(title=title),
            sku=module.SkuNode(),
            offers=[
                module.OfferNode(
                    offer_id=f"offer::{title}",
                    catalog_track="external_referral",
                    truth_tier="fallback",
                    readiness_tier="commerce_ready",
                    offer_mode="redirect",
                    source_system="external_product_seeds",
                    pricing=module.PivotPricing(
                        currency="USD",
                        merchant_effective_price=Decimal(price),
                        estimated_best_price=Decimal(price),
                    ),
                    incentives=[],
                )
            ],
            catalog_track="external_referral",
            truth_tier="fallback",
            readiness_tier="commerce_ready",
            freshness={},
            source_system="external_product_seeds",
            match_explanation={
                "lane": "external_fallback",
                "relevance_score": relevance,
                "source_order": source_order,
            },
        )

    lower_price = build_item("Cheaper But Less Relevant", "20.00", 1)
    higher_relevance = build_item("More Relevant", "30.00", 3)

    items = module._sort_items([lower_price, higher_relevance])

    assert items[0].product.title == "More Relevant"


@pytest.mark.asyncio
async def test_sort_items_prefers_external_source_order_before_price_when_relevance_ties() -> None:
    def build_item(title: str, price: str, source_order: int) -> module.PivotResultItem:
        return module.PivotResultItem(
            merchant=module.MerchantNode(merchant_name="Demo"),
            product=module.ProductNode(title=title),
            sku=module.SkuNode(),
            offers=[
                module.OfferNode(
                    offer_id=f"offer::{title}",
                    catalog_track="external_referral",
                    truth_tier="fallback",
                    readiness_tier="commerce_ready",
                    offer_mode="redirect",
                    source_system="external_product_seeds",
                    pricing=module.PivotPricing(
                        currency="USD",
                        merchant_effective_price=Decimal(price),
                        estimated_best_price=Decimal(price),
                    ),
                    incentives=[],
                )
            ],
            catalog_track="external_referral",
            truth_tier="fallback",
            readiness_tier="commerce_ready",
            freshness={},
            source_system="external_product_seeds",
            match_explanation={
                "lane": "external_fallback",
                "relevance_score": 0.9,
                "source_order": source_order,
            },
        )

    earlier_seed_row = build_item("Earlier Seed Row", "40.00", 1)
    later_seed_row = build_item("Later Seed Row", "20.00", 9)

    items = module._sort_items([later_seed_row, earlier_seed_row])

    assert items[0].product.title == "Earlier Seed Row"


@pytest.mark.asyncio
async def test_sort_items_preserves_external_zero_source_order() -> None:
    def build_item(title: str, source_order: int) -> module.PivotResultItem:
        return module.PivotResultItem(
            merchant=module.MerchantNode(merchant_name="Demo"),
            product=module.ProductNode(title=title),
            sku=module.SkuNode(),
            offers=[
                module.OfferNode(
                    offer_id=f"offer::{title}",
                    catalog_track="external_referral",
                    truth_tier="fallback",
                    readiness_tier="commerce_ready",
                    offer_mode="redirect",
                    source_system="external_product_seeds",
                    pricing=module.PivotPricing(
                        currency="USD",
                        merchant_effective_price=Decimal("20.00"),
                        estimated_best_price=Decimal("20.00"),
                    ),
                    incentives=[],
                )
            ],
            catalog_track="external_referral",
            truth_tier="fallback",
            readiness_tier="commerce_ready",
            freshness={},
            source_system="external_product_seeds",
            match_explanation={
                "lane": "external_fallback",
                "relevance_score": 0.9,
                "source_order": source_order,
            },
        )

    first_seed_row = build_item("First Seed Row", 0)
    later_seed_row = build_item("Later Seed Row", 1)

    items = module._sort_items([later_seed_row, first_seed_row])

    assert items[0].product.title == "First Seed Row"


@pytest.mark.asyncio
async def test_build_external_item_uses_text_relevance_as_primary_signal() -> None:
    more_relevant = module._build_external_item(
        {
            "external_product_id": "ext_1",
            "title": "Après Skin Rich Rescue Barrier Moisturizer with Ceramides",
            "domain": "olehenriksen.com",
            "canonical_url": "https://olehenriksen.com/products/apres-skin-multi-use-rich-rescue-cream",
            "destination_url": "https://olehenriksen.com/products/apres-skin-multi-use-rich-rescue-cream",
            "brand_term_hit": 2,
            "seed_data": {"brand": "Olehenriksen"},
        },
        "barrier moisturizer",
        source_order=5,
    )
    less_relevant = module._build_external_item(
        {
            "external_product_id": "ext_2",
            "title": "Barrier Repair Eye Cream",
            "domain": "byoma.com",
            "canonical_url": "https://byoma.com/products/barrier-repair-eye-cream",
            "destination_url": "https://byoma.com/products/barrier-repair-eye-cream",
            "brand_term_hit": 9,
            "seed_data": {"brand": "BYOMA"},
        },
        "barrier moisturizer",
        source_order=1,
    )

    assert more_relevant.match_explanation["text_relevance_score"] > less_relevant.match_explanation["text_relevance_score"]
    assert more_relevant.match_explanation["relevance_score"] > less_relevant.match_explanation["relevance_score"]
    assert more_relevant.match_explanation["source_order"] == 5


@pytest.mark.asyncio
async def test_build_external_item_preserves_structured_beauty_fields() -> None:
    item = module._build_external_item(
        {
            "external_product_id": "ext_spf_50",
            "title": "Mineral Sunscreen SPF 50",
            "domain": "example.com",
            "canonical_url": "https://example.com/products/mineral-sunscreen-spf-50",
            "destination_url": "https://example.com/products/mineral-sunscreen-spf-50",
            "category": "Sunscreen",
            "seed_data": {
                "brand": "Example",
                "category": "Sunscreen",
                "reviewed_ingredient_ids": ["zinc_oxide"],
                "visible_attributes": {
                    "product_category": ["sunscreen"],
                    "formula_constraint": ["fragrance_free"],
                },
                "variants": [
                    {
                        "id": "variant_1",
                        "title": "SPF 50",
                        "price": 24.0,
                        "options": {"SPF": "50"},
                    }
                ],
            },
        },
        "spf 50 sunscreen",
        source_order=0,
    )

    assert item.sku.visible_attributes["product_category"] == ["sunscreen"]
    assert item.sku.visible_attributes["formula_constraint"] == ["fragrance_free"]
    assert item.sku.ingredient_ids == ["zinc_oxide"]
    assert "spf_50" in item.sku.visible_option_labels
    assert item.match_explanation["ranking_audit_version"] == "beauty_external_ranking_v1"
    assert item.match_explanation["candidate_source"] == "external_seed"


def test_external_text_relevance_prefers_title_term_hits_and_penalizes_eye_cream() -> None:
    gentle_cleanser = module._external_text_relevance_score(
        {
            "title": "Ultra Gentle Cream-to-Foam Face Cleanser",
            "canonical_url": "https://example.com/products/face-cleanser",
            "destination_url": "https://example.com/products/face-cleanser",
            "seed_data": {},
        },
        "gentle cleanser",
    )
    generic_cleanser = module._external_text_relevance_score(
        {
            "title": "Milky Moisture Cleanser",
            "canonical_url": "https://example.com/products/milky-moisture-cleanser",
            "destination_url": "https://example.com/products/milky-moisture-cleanser",
            "seed_data": {"description": "A creamy cleanser for daily use"},
        },
        "gentle cleanser",
    )
    eye_cream = module._external_text_relevance_score(
        {
            "title": "Barrier Repair Eye Cream",
            "canonical_url": "https://example.com/products/barrier-repair-eye-cream",
            "destination_url": "https://example.com/products/barrier-repair-eye-cream",
            "seed_data": {},
        },
        "hydrating barrier moisturizer fragrance free",
    )
    barrier_moisturizer = module._external_text_relevance_score(
        {
            "title": "Après Skin Rich Rescue Barrier Moisturizer with Ceramides",
            "canonical_url": "https://example.com/products/apres-skin-rich-rescue-barrier-moisturizer",
            "destination_url": "https://example.com/products/apres-skin-rich-rescue-barrier-moisturizer",
            "seed_data": {},
        },
        "hydrating barrier moisturizer fragrance free",
    )
    gentle_serum = module._external_text_relevance_score(
        {
            "title": "Gentle Glycolic Acid Resurfacing Serum",
            "canonical_url": "https://example.com/products/gentle-glycolic-acid-resurfacing-serum",
            "destination_url": "https://example.com/products/gentle-glycolic-acid-resurfacing-serum",
            "seed_data": {},
        },
        "gentle cleanser",
    )

    assert gentle_cleanser > generic_cleanser
    assert gentle_cleanser > gentle_serum
    assert barrier_moisturizer > eye_cream


def test_external_text_relevance_uses_slug_tokens_for_acne_queries() -> None:
    slug_with_acne = module._external_text_relevance_score(
        {
            "title": "Clarifying Cleanser Larger Size",
            "canonical_url": "https://example.com/products/clarifying-acne-cleanser-larger-size",
            "destination_url": "https://example.com/products/clarifying-acne-cleanser-larger-size",
            "seed_data": {},
        },
        "acne cleanser",
    )
    slug_without_acne = module._external_text_relevance_score(
        {
            "title": "Clarifying Cleanser Larger Size",
            "canonical_url": "https://example.com/products/clarifying-cleanser-larger-size",
            "destination_url": "https://example.com/products/clarifying-cleanser-larger-size",
            "seed_data": {},
        },
        "acne cleanser",
    )

    assert slug_with_acne > slug_without_acne


def test_external_text_relevance_prefers_concern_match_for_active_ingredient_query() -> None:
    acne_mist = module._external_text_relevance_score(
        {
            "title": "Body Acne Clearing Mist with 2% Salicylic Acid",
            "canonical_url": "https://example.com/products/body-acne-clearing-mist-salicylic-acid",
            "destination_url": "https://example.com/products/body-acne-clearing-mist-salicylic-acid",
            "seed_data": {},
        },
        "salicylic acid serum for acne and pores",
    )
    plain_serum = module._external_text_relevance_score(
        {
            "title": "Salicylic Acid Serum 2%",
            "canonical_url": "https://example.com/products/salicylic-acid-serum",
            "destination_url": "https://example.com/products/salicylic-acid-serum",
            "seed_data": {},
        },
        "salicylic acid serum for acne and pores",
    )

    assert acne_mist > plain_serum


def test_sort_items_prefers_gentle_cleanser_anchor_over_serum_and_generic_cleanser() -> None:
    items = module._sort_items(
        [
            module._build_external_item(
                {
                    "external_product_id": "ext_gentle_serum",
                    "title": "Gentle Glycolic Acid Resurfacing Serum",
                    "domain": "murad.com",
                    "canonical_url": "https://murad.com/products/gentle-glycolic-acid-resurfacing-serum",
                    "destination_url": "https://murad.com/products/gentle-glycolic-acid-resurfacing-serum",
                    "brand_term_hit": 1,
                    "seed_data": {"brand": "Murad"},
                },
                "gentle cleanser",
                source_order=0,
            ),
            module._build_external_item(
                {
                    "external_product_id": "ext_milky_cleanser",
                    "title": "Milky Moisture Cleanser",
                    "domain": "byoma.com",
                    "canonical_url": "https://byoma.com/products/milky-moisture-cleanser",
                    "destination_url": "https://byoma.com/products/milky-moisture-cleanser",
                    "brand_term_hit": 1,
                    "seed_data": {"brand": "BYOMA"},
                },
                "gentle cleanser",
                source_order=1,
            ),
            module._build_external_item(
                {
                    "external_product_id": "ext_ultra_gentle_cleanser_jumbo",
                    "title": "Ultra Gentle Cream-to-Foam Face Cleanser Jumbo",
                    "domain": "firstaidbeauty.com",
                    "canonical_url": "https://firstaidbeauty.com/products/face-cleanser-jumbo",
                    "destination_url": "https://firstaidbeauty.com/products/face-cleanser-jumbo",
                    "brand_term_hit": 2,
                    "seed_data": {"brand": "First Aid Beauty"},
                },
                "gentle cleanser",
                source_order=1,
            ),
            module._build_external_item(
                {
                    "external_product_id": "ext_ultra_gentle_cleanser",
                    "title": "Ultra Gentle Cream-to-Foam Face Cleanser with Colloidal Oatmeal + Glycerin Travel Size",
                    "domain": "firstaidbeauty.com",
                    "canonical_url": "https://firstaidbeauty.com/products/face-cleanser-travel-size",
                    "destination_url": "https://firstaidbeauty.com/products/face-cleanser-travel-size",
                    "brand_term_hit": 2,
                    "seed_data": {"brand": "First Aid Beauty"},
                },
                "gentle cleanser",
                source_order=2,
            ),
        ]
    )

    assert items[0].product.title == "Ultra Gentle Cream-to-Foam Face Cleanser Jumbo"
    assert items[1].match_explanation["quality_penalties"]["travel_size_without_intent"] > 0


def test_sort_items_prefers_exact_salicylic_serum_over_mist_when_category_matches() -> None:
    items = module._sort_items(
        [
            module._build_external_item(
                {
                    "external_product_id": "ext_salicylic_mist",
                    "title": "Body Acne Clearing Mist with 2% Salicylic Acid",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/body-acne-clearing-mist-salicylic-acid",
                    "destination_url": "https://example.com/products/body-acne-clearing-mist-salicylic-acid",
                    "brand_term_hit": 2,
                    "seed_data": {
                        "brand": "Example",
                        "category": "Treatment",
                        "visible_attributes": {"skin_concern": ["acne", "pores"]},
                        "reviewed_ingredient_ids": ["salicylic_acid"],
                    },
                },
                "salicylic acid serum for acne and pores",
                source_order=0,
            ),
            module._build_external_item(
                {
                    "external_product_id": "ext_salicylic_serum",
                    "title": "Salicylic Acid Serum 2%",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/salicylic-acid-serum-2",
                    "destination_url": "https://example.com/products/salicylic-acid-serum-2",
                    "brand_term_hit": 3,
                    "seed_data": {
                        "brand": "Example",
                        "category": "Serum",
                        "visible_attributes": {
                            "product_category": ["serum"],
                            "skin_concern": ["acne", "pores"],
                        },
                        "reviewed_ingredient_ids": ["salicylic_acid"],
                    },
                },
                "salicylic acid serum for acne and pores",
                source_order=1,
            ),
        ]
    )

    assert items[0].product.title == "Salicylic Acid Serum 2%"
    assert items[1].match_explanation["quality_penalties"]["missing_category_anchor"] > 0


def test_sort_items_prefers_barrier_moisturizer_over_spf_for_fragrance_free_query() -> None:
    items = module._sort_items(
        [
            module._build_external_item(
                {
                    "external_product_id": "ext_spf_hydrating",
                    "title": "Superactive Moisturizer SPF 50: Hydrating",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/superactive-moisturizer-spf-50-hydrating",
                    "destination_url": "https://example.com/products/superactive-moisturizer-spf-50-hydrating",
                    "brand_term_hit": 2,
                    "seed_data": {
                        "brand": "Example",
                        "category": "Moisturizer",
                        "visible_attributes": {
                            "product_category": ["moisturizer", "sunscreen"],
                            "skin_concern": ["hydrating"],
                        },
                    },
                },
                "hydrating barrier moisturizer fragrance free",
                source_order=0,
            ),
            module._build_external_item(
                {
                    "external_product_id": "ext_barrier_moisturizer",
                    "title": "Cellular Hydration Barrier Repair Cream Moisturizer",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/barrier-repair-cream-moisturizer",
                    "destination_url": "https://example.com/products/barrier-repair-cream-moisturizer",
                    "brand_term_hit": 2,
                    "seed_data": {
                        "brand": "Example",
                        "category": "Moisturizer",
                        "visible_attributes": {
                            "product_category": ["moisturizer"],
                            "skin_concern": ["hydrating"],
                            "formula_constraint": ["fragrance_free"],
                        },
                    },
                },
                "hydrating barrier moisturizer fragrance free",
                source_order=1,
            ),
        ]
    )

    assert items[0].product.title == "Cellular Hydration Barrier Repair Cream Moisturizer"
    penalties = items[1].match_explanation["quality_penalties"]
    assert penalties["sun_protection_without_intent"] > 0
    assert penalties["missing_formula_constraint"] > 0


def test_sort_items_prefers_sunscreen_category_for_sunscreen_query() -> None:
    items = module._sort_items(
        [
            module._build_external_item(
                {
                    "external_product_id": "ext_spf_moisturizer",
                    "title": "Daily Moisturizer SPF 50",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/daily-moisturizer-spf-50",
                    "destination_url": "https://example.com/products/daily-moisturizer-spf-50",
                    "brand_term_hit": 2,
                    "seed_data": {
                        "brand": "Example",
                        "category": "Moisturizer",
                        "visible_attributes": {"product_category": ["moisturizer"]},
                    },
                },
                "sunscreen",
                source_order=0,
            ),
            module._build_external_item(
                {
                    "external_product_id": "ext_sunscreen",
                    "title": "Mineral Sunscreen SPF 50",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/mineral-sunscreen-spf-50",
                    "destination_url": "https://example.com/products/mineral-sunscreen-spf-50",
                    "brand_term_hit": 2,
                    "seed_data": {
                        "brand": "Example",
                        "category": "Sunscreen",
                        "visible_attributes": {"product_category": ["sunscreen"]},
                    },
                },
                "sunscreen",
                source_order=1,
            ),
        ]
    )

    assert items[0].product.title == "Mineral Sunscreen SPF 50"
    assert items[1].match_explanation["quality_penalties"]["missing_sunscreen_category"] > 0


def test_sort_items_prefers_gel_moisturizer_for_acne_prone_query() -> None:
    items = module._sort_items(
        [
            module._build_external_item(
                {
                    "external_product_id": "ext_cream_moisturizer",
                    "title": "Heartleaf Calming Cream Moisturizer for Sensitive Skin and Eczema-Prone Skin",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/heartleaf-calming-cream-moisturizer",
                    "destination_url": "https://example.com/products/heartleaf-calming-cream-moisturizer",
                    "brand_term_hit": 3,
                    "seed_data": {
                        "brand": "Example",
                        "category": "Moisturizer",
                        "visible_attributes": {"product_category": ["moisturizer"]},
                    },
                },
                "lightweight gel moisturizer for acne-prone skin",
                source_order=0,
            ),
            module._build_external_item(
                {
                    "external_product_id": "ext_gel_moisturizer",
                    "title": "Calm + Restore Oat Gel Moisturizer, Sensitive Skin",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/calm-restore-oat-gel-moisturizer",
                    "destination_url": "https://example.com/products/calm-restore-oat-gel-moisturizer",
                    "brand_term_hit": 2,
                    "seed_data": {
                        "brand": "Example",
                        "category": "Moisturizer",
                        "visible_attributes": {
                            "product_category": ["moisturizer"],
                            "skin_concern": ["acne"],
                        },
                    },
                },
                "lightweight gel moisturizer for acne-prone skin",
                source_order=1,
            ),
            module._build_external_item(
                {
                    "external_product_id": "ext_acne_serum",
                    "title": "Rapid Clear Stubborn Acne Spot Gel",
                    "domain": "example.com",
                    "canonical_url": "https://example.com/products/rapid-clear-stubborn-acne-spot-gel",
                    "destination_url": "https://example.com/products/rapid-clear-stubborn-acne-spot-gel",
                    "brand_term_hit": 2,
                    "seed_data": {
                        "brand": "Example",
                        "category": "Serum",
                        "visible_attributes": {
                            "product_category": ["serum"],
                            "skin_concern": ["acne"],
                        },
                    },
                },
                "lightweight gel moisturizer for acne-prone skin",
                source_order=2,
            ),
        ]
    )

    assert items[0].product.title == "Calm + Restore Oat Gel Moisturizer, Sensitive Skin"
    assert items[2].match_explanation["quality_penalties"]["missing_category_anchor"] > 0


def test_sort_items_penalizes_eye_cream_and_routine_for_barrier_moisturizer_query() -> None:
    items = module._sort_items(
        [
            module._build_external_item(
                {
                    "external_product_id": "ext_eye_cream",
                    "title": "Barrier Repair Eye Cream",
                    "domain": "byoma.com",
                    "canonical_url": "https://byoma.com/products/barrier-repair-eye-cream",
                    "destination_url": "https://byoma.com/products/barrier-repair-eye-cream",
                    "brand_term_hit": 1,
                    "seed_data": {"brand": "BYOMA"},
                },
                "hydrating barrier moisturizer fragrance free",
                source_order=0,
            ),
            module._build_external_item(
                {
                    "external_product_id": "ext_routine",
                    "title": "Cult Fragrance-Free Skincare Routine",
                    "domain": "embryolisse.com",
                    "canonical_url": "https://embryolisse.com/products/natural-beauty-set",
                    "destination_url": "https://embryolisse.com/products/natural-beauty-set",
                    "brand_term_hit": 2,
                    "seed_data": {"brand": "Embryolisse"},
                },
                "hydrating barrier moisturizer fragrance free",
                source_order=1,
            ),
            module._build_external_item(
                {
                    "external_product_id": "ext_barrier_moisturizer",
                    "title": "Après Skin Rich Rescue Barrier Moisturizer with Ceramides",
                    "domain": "olehenriksen.com",
                    "canonical_url": "https://olehenriksen.com/products/apres-skin-multi-use-rich-rescue-cream",
                    "destination_url": "https://olehenriksen.com/products/apres-skin-multi-use-rich-rescue-cream",
                    "brand_term_hit": 2,
                    "seed_data": {"brand": "Olehenriksen"},
                },
                "hydrating barrier moisturizer fragrance free",
                source_order=2,
            ),
        ]
    )

    assert items[0].product.title == "Après Skin Rich Rescue Barrier Moisturizer with Ceramides"


@pytest.mark.asyncio
async def test_fetch_canonical_search_rows_uses_candidate_cte_and_avoids_json_search_for_non_vertical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    async def fake_fetch_all(query: str, params: dict):
        observed["query"] = query
        observed["params"] = params
        return []

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    rows = await module._fetch_canonical_search_rows(
        query="vitamin c serum",
        merchant_id="merch_1",
        limit=5,
    )

    assert rows == []
    assert "WITH candidate_skus AS" in observed["query"]
    assert "JOIN catalog_offers o" in observed["query"]
    assert "ON o.sku_key = c.sku_key" in observed["query"]
    assert "CAST(s.visible_option_labels AS TEXT)" not in observed["query"]
    assert "CAST(s.ingredient_ids AS TEXT)" not in observed["query"]
    assert observed["params"]["candidate_limit"] >= 20
    assert observed["params"]["row_limit"] >= 30


@pytest.mark.asyncio
async def test_fetch_canonical_search_rows_includes_json_search_for_vertical_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    async def fake_fetch_all(query: str, params: dict):
        observed["query"] = query
        observed["params"] = params
        return []

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    rows = await module._fetch_canonical_search_rows(
        query="vitamin c ingredients",
        merchant_id=None,
        limit=5,
    )

    assert rows == []
    assert "CAST(s.visible_option_labels AS TEXT)" in observed["query"]
    assert "CAST(s.ingredient_ids AS TEXT)" in observed["query"]


@pytest.mark.asyncio
async def test_preview_pivot_quote_stores_snapshot_and_estimates_incentives(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_offer_row(_offer_id: str):
        return {
            "offer_id": "offer::1",
            "sku_key": "sku::1",
            "product_key": "prod::1",
            "merchant_id": "merch_1",
            "currency": "USD",
            "list_price": Decimal("32.00"),
            "merchant_effective_price": Decimal("28.00"),
            "estimated_best_price": Decimal("28.00"),
            "offer_payload": {"product_id": "111", "variant_id": "var_1"},
            "source_product_id": "111",
            "source_variant_id": "var_1",
        }

    class FakeQuoteService:
        async def preview_quote(self, **_kwargs):
            return {
                "quote_id": "quote_123",
                "currency": "USD",
                "pricing": {
                    "subtotal": "28.00",
                    "discount_total": "0.00",
                    "shipping_fee": "0.00",
                    "tax": "0.00",
                    "total": "28.00",
                },
                "payment_offer_evidence": {
                    "pricing_confidence": "context_matched",
                    "offers": [
                        {
                            "payment_offer_id": "inc_1",
                            "label": "Mastercard 5% Off",
                            "estimated_savings": "1.40",
                            "estimated_total_after_payment_offer": "26.60",
                        }
                    ],
                    "decisions": [],
                },
                "expires_at": datetime.now(timezone.utc),
            }

    async def fake_fetch_offer_incentives(_offer_ids, *, payment_context):
        assert isinstance(payment_context, PivotPaymentContext)
        return {
            "offer::1": [
                module.IncentiveNode(
                    incentive_id="inc_1",
                    label="Mastercard 5% Off",
                    incentive_type="payment_incentive",
                    benefit_kind="percentage_off",
                    benefit_value=Decimal("5.00"),
                    benefit_currency="USD",
                    card_network="mastercard",
                    eligibility_confidence=Decimal("0.8"),
                    source_system="merchant_config",
                )
            ]
        }

    observed_snapshots = []

    async def fake_store_catalog_quote_snapshot(**kwargs):
        observed_snapshots.append(kwargs)

    monkeypatch.setattr(module, "_fetch_offer_row", fake_fetch_offer_row)
    monkeypatch.setattr(module, "QuoteService", FakeQuoteService)
    monkeypatch.setattr(module, "_fetch_offer_incentives", fake_fetch_offer_incentives)
    monkeypatch.setattr(module, "store_catalog_quote_snapshot", fake_store_catalog_quote_snapshot)

    result = await module.preview_pivot_quote(
        PivotQuoteRequest(
            merchant_id="merch_1",
            items=[PivotQuoteItem(offer_id="offer::1", quantity=1)],
            payment_context=PivotPaymentContext(card_network="mastercard"),
        )
    )

    assert result.quote_id == "quote_123"
    assert result.pricing.exact_quote_price == Decimal("28.00")
    assert result.pricing.estimated_best_price == Decimal("28.00")
    assert result.payment_offer_evidence["offers"][0]["estimated_total_after_payment_offer"] == "26.60"
    assert len(result.incentives) == 1
    assert len(observed_snapshots) == 1
    assert observed_snapshots[0]["offer_id"] == "offer::1"
