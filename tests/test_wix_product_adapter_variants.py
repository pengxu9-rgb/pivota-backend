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


def _dummy_wix_products_query_response() -> dict:
    return {
        "products": [
            {
                "id": "prod_1",
                "name": "Rope Dog Leash",
                "visible": True,
                "priceData": {"price": 10.0, "currency": "USD"},
                "variants": [
                    {
                        "id": "var_1",
                        "choices": {"Size": "S"},
                        "priceData": {"price": 11.0},
                        "stock": {"quantity": 2, "inStock": True, "trackQuantity": True},
                    }
                ],
            }
        ],
        "totalResults": 1,
    }


def _dummy_httpx_response(payload: dict):
    class DummyResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return payload

    return DummyResponse()


def _dummy_httpx_client_factory(captured: dict, response_payload: dict):
    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _dummy_httpx_response(response_payload)

    return DummyAsyncClient


async def test_wix_fetch_products_includes_variants(monkeypatch):
    import httpx

    captured = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _dummy_httpx_client_factory(captured, _dummy_wix_products_query_response()),
    )

    products, next_token, err = await WixProductAdapter.fetch_products(
        site_id="site_1",
        api_key="api_key_1",
        merchant_id="merch_test",
        limit=50,
        page_token=None,
    )

    assert err is None
    assert next_token is None
    assert len(products) == 1
    assert len(products[0].variants) == 1
    assert captured["url"].endswith("/stores/v1/products/query")
    assert captured["json"]["includeVariants"] is True
