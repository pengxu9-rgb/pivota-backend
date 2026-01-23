from adapters.product_adapters import WixProductAdapter


def test_wix_convert_product_parses_variants_list():
    wp = {
        "id": "prod_1",
        "name": "Rope Dog Leash",
        "description": "desc",
        "visible": True,
        "priceData": {"price": 10.0, "currency": "USD"},
        "stock": {"quantity": 0, "inStock": True, "trackQuantity": False},
        "media": {"items": [{"image": {"url": "https://example.com/p.jpg"}}]},
        "variants": [
            {
                "id": "var_1",
                "choices": {"Size": "S", "Color": "Red"},
                "sku": "SKU-S-RED",
                "priceData": {"price": 11.0},
                "stock": {"quantity": 2, "inStock": True, "trackQuantity": True},
                "media": {"items": [{"image": {"url": "https://example.com/v1.jpg"}}]},
            },
            {
                "id": "var_2",
                "choices": {"Size": "M", "Color": "Blue"},
                "sku": "SKU-M-BLUE",
                "priceData": {"price": 12.0},
                "stock": {"quantity": 3, "inStock": True, "trackQuantity": True},
            },
        ],
    }

    product = WixProductAdapter._convert_product(wp, merchant_id="merch_test")
    assert product is not None
    assert product.platform == "wix"
    assert product.id == "prod_1"
    assert len(product.variants) == 2

    ids = {v.id for v in product.variants}
    assert ids == {"var_1", "var_2"}

    v1 = next(v for v in product.variants if v.id == "var_1")
    assert v1.sku == "SKU-S-RED"
    assert v1.inventory_quantity == 2
    assert v1.options == {"Size": "S", "Color": "Red"}
    assert v1.title == "S / Red"
    assert v1.image_url == "https://example.com/v1.jpg"

    # When variants exist, product inventory is derived from variants.
    assert product.inventory_quantity == 5


def test_wix_convert_product_handles_nested_variant_payload_and_price_fallback():
    wp = {
        "id": "prod_2",
        "name": "Variant-only pricing",
        "visible": True,
        "priceData": {"price": 0, "currency": "USD"},
        "variants": {
            "variants": [
                {
                    "id": "v_1",
                    "choices": {"Size": "M"},
                    "variant": {
                        "priceData": {"price": 12.0},
                        "stock": {"quantity": 1, "inStock": True, "trackQuantity": True},
                        "sku": "SKU-M",
                    },
                }
            ]
        },
    }

    product = WixProductAdapter._convert_product(wp, merchant_id="merch_test")
    assert product is not None
    assert product.id == "prod_2"
    assert product.price == 12.0
    assert product.orderable is True
    assert len(product.variants) == 1
    assert product.variants[0].id == "v_1"
    assert product.variants[0].sku == "SKU-M"
    assert product.variants[0].options == {"Size": "M"}
