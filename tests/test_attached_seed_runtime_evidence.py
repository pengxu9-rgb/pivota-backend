from __future__ import annotations

import pytest

from services.attached_seed_runtime_evidence import (
    apply_attached_seed_runtime_evidence_to_product_data,
    build_attached_seed_runtime_evidence_by_product_key,
    hydrate_product_payloads_from_attached_seed_runtime_evidence,
)


def test_build_attached_seed_runtime_evidence_maps_reviewed_ingredients_by_source_ref() -> None:
    evidence_by_key = build_attached_seed_runtime_evidence_by_product_key(
        seed_rows=[
            {
                "attached_product_key": "merch_1|shopify|prod_1",
                "canonical_url": "https://brand.example/products/serum",
                "destination_url": "https://brand.example/products/serum",
                "attached_variant_id": "∅",
                "seed_data": {
                    "snapshot": {
                        "canonical_url": "https://brand.example/products/serum",
                        "variants": [],
                    }
                },
            }
        ],
        kb_rows=[
            {
                "sku_key": "opaque_1",
                "source_ref": "https://brand.example/products/serum",
                "inci_list_json": ["Niacinamide", "Panthenol"],
            }
        ],
    )

    assert evidence_by_key["merch_1|shopify|prod_1"]["reviewed_ingredient_ids"] == [
        "Niacinamide",
        "Panthenol",
    ]


def test_build_attached_seed_runtime_evidence_projects_variant_shade_from_attached_seed() -> None:
    evidence_by_key = build_attached_seed_runtime_evidence_by_product_key(
        seed_rows=[
            {
                "attached_product_key": "merch_1|shopify|prod_foundation",
                "attached_variant_id": "4242",
                "title": "Silk Finish Foundation",
                "seed_data": {
                    "snapshot": {
                        "title": "Silk Finish Foundation",
                        "variants": [
                            {
                                "variant_id": "4242",
                                "option_name": "Shade",
                                "option_value": "210",
                                "url": "https://brand.example/products/foundation?variant=4242",
                            }
                        ],
                    }
                },
            }
        ],
        kb_rows=[],
    )

    assert evidence_by_key["merch_1|shopify|prod_foundation"]["variant_shades"] == {
        "4242": ["210"]
    }


def test_apply_attached_seed_runtime_evidence_merges_reviewed_ingredients_and_shade() -> None:
    product_data = {
        "id": "prod_foundation",
        "product_id": "prod_foundation",
        "platform": "shopify",
        "merchant_id": "merch_1",
        "title": "Silk Finish Foundation",
        "product_type": "Foundation",
        "price": 36,
        "currency": "USD",
        "variants": [
            {
                "id": "4242",
                "variant_id": "4242",
                "title": "Default",
                "price": 36,
                "currency": "USD",
            }
        ],
    }
    hydrated, changed = apply_attached_seed_runtime_evidence_to_product_data(
        product_data=product_data,
        evidence={
            "reviewed_ingredient_ids": ["Niacinamide", "Panthenol"],
            "variant_shades": {"4242": ["210"]},
        },
    )

    assert changed is True
    assert hydrated["platform_metadata"]["reviewed_ingredient_ids"] == ["Niacinamide", "Panthenol"]
    assert hydrated["variants"][0]["platform_metadata"]["shade_name_text"] == "210"
    assert hydrated["variants"][0]["platform_metadata"]["shade_code"] == "210"


@pytest.mark.asyncio
async def test_hydrate_product_payloads_from_attached_seed_runtime_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.attached_seed_runtime_evidence as module

    async def fake_fetch_all(query, values):
        assert values["product_key_prefix"] == "merch_1|shopify|%"
        return [
            {
                "attached_product_key": "merch_1|shopify|prod_1",
                "attached_variant_id": "∅",
                "canonical_url": "https://brand.example/products/serum",
                "destination_url": "https://brand.example/products/serum",
                "seed_data": {"snapshot": {"canonical_url": "https://brand.example/products/serum", "variants": []}},
            }
        ]

    def fake_fetch_runtime_evidence(source_refs):
        assert "brand.example/products/serum" in source_refs
        return [
            {
                "sku_key": "opaque_1",
                "source_ref": "https://brand.example/products/serum",
                "inci_list_json": ["Niacinamide"],
            }
        ]

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(module, "fetch_pci_kb_runtime_evidence_rows_by_source_refs_sync", fake_fetch_runtime_evidence)

    hydrated = await hydrate_product_payloads_from_attached_seed_runtime_evidence(
        merchant_id="merch_1",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_1",
                "product_id": "prod_1",
                "platform": "shopify",
                "merchant_id": "merch_1",
                "title": "Serum",
                "price": 20,
                "currency": "USD",
                "variants": [],
            }
        ],
    )

    assert hydrated[0]["platform_metadata"]["reviewed_ingredient_ids"] == ["Niacinamide"]
