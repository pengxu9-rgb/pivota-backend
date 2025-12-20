import json

from jobs.catalog_import_worker import _build_shopify_cache_payload


def test_build_shopify_cache_payload_includes_price_sku_inventory_images():
    raw = {
        "id": 123,
        "title": "Test Product",
        "handle": "test-product",
        "status": "active",
        "published_at": "2025-01-01T00:00:00Z",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-02T00:00:00Z",
        "tags": "cat:brush, use:blush",
        "images": [{"src": "https://example.com/a.jpg"}, {"src": "https://example.com/b.jpg"}],
        "variants": [
            {
                "id": 999,
                "title": "Default Title",
                "sku": "SKU-1",
                "barcode": "BAR-1",
                "price": "12.34",
                "compare_at_price": None,
                "inventory_quantity": 7,
                "weight": None,
                "weight_unit": None,
            }
        ],
        "options": [{"name": "Title"}],
    }

    platform_product_id, payload, standard = _build_shopify_cache_payload(
        merchant_id="merch_test",
        raw_shopify_product=raw,
    )

    assert platform_product_id == "123"
    assert payload["shopify_id"] == "123"
    assert payload["handle"] == "test-product"

    # StandardProduct-shaped fields expected by downstream readers
    assert payload["id"] == "123"
    assert payload["platform"] == "shopify"
    assert payload["merchant_id"] == "merch_test"
    assert payload["price"] == 12.34
    assert payload["sku"] == "SKU-1"
    assert payload["inventory_quantity"] == 7
    assert payload["image_url"] == "https://example.com/a.jpg"
    assert payload["images"] == ["https://example.com/a.jpg", "https://example.com/b.jpg"]

    # Extra keys are additive and JSON serializable
    assert payload["raw"]["id"] == 123
    json.dumps(payload)

    assert standard.id == "123"

