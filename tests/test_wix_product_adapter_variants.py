from adapters import product_adapters
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
                "gtin": "1234567890123",
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
    assert v1.barcode == "1234567890123"
    assert v1.platform_metadata == {"raw_wix_variant": wp["variants"][0]}

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
                        "upc": "123456789012",
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
    assert product.variants[0].barcode == "123456789012"
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


def _wix_product(product_id: int) -> dict:
    return {
        "id": f"prod_{product_id}",
        "name": f"Product {product_id}",
        "visible": True,
        "priceData": {"price": 10.0, "currency": "USD"},
        "stock": {"quantity": 5, "inStock": True, "trackQuantity": True},
    }


def _dummy_httpx_response(payload: dict):
    class DummyResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return payload

    return DummyResponse()


def _dummy_httpx_status_response(status_code: int, text: str):
    class DummyResponse:
        def __init__(self):
            self.status_code = status_code
            self.text = text

        def json(self):
            return {}

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


def _paginated_httpx_client_factory(captured: dict, responses: list):
    """Fake client for the Wix PRODUCT paging tests.

    Since 2026-07-29 a sync also issues one `collections/query` call to resolve
    categories (see tests/test_wix_category_mapping.py). This fake is therefore
    URL-AWARE, for two independent reasons:

    1. An unrouted collections call consumes a queued product page, so the
       paging under test never happens.
    2. `captured["requests"]` must keep meaning *product* requests only. Every
       assertion in this file reads product paging offsets out of that list,
       and the collections call also carries `paging.offset: 0` -- letting it
       in would turn those comparisons into passing-but-meaningless ones.

    Collections are recorded under `captured["collection_requests"]` rather
    than dropped, so the extra call stays visible instead of being swallowed by
    the harness.
    """

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            self._index = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None):
            if str(url).endswith("/collections/query"):
                captured.setdefault("collection_requests", []).append(
                    {"url": url, "json": json, "headers": headers}
                )
                return _dummy_httpx_response({"collections": []})
            captured.setdefault("requests", []).append(
                {"url": url, "json": json, "headers": headers}
            )
            response = responses[self._index]
            self._index += 1
            return response

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
    assert captured["url"].endswith("/stores-reader/v1/products/query")
    assert captured["json"]["includeVariants"] is True
    assert captured["json"]["query"]["paging"]["offset"] == 0


async def test_wix_fetch_products_pages_until_last_page(monkeypatch):
    import httpx

    captured = {}
    responses = [
        _dummy_httpx_response(
            {"products": [_wix_product(i) for i in range(100)], "totalResults": 150}
        ),
        _dummy_httpx_response(
            {"products": [_wix_product(i) for i in range(100, 150)], "totalResults": 150}
        ),
    ]
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _paginated_httpx_client_factory(captured, responses),
    )

    products, next_token, err = await WixProductAdapter.fetch_products(
        site_id="site_1",
        api_key="api_key_1",
        merchant_id="merch_test",
        limit=100,
        page_token=None,
    )

    assert err is None
    assert next_token is None
    assert len(products) == 150
    assert [req["json"]["query"]["paging"]["offset"] for req in captured["requests"]] == [
        0,
        100,
    ]


async def test_wix_fetch_products_stops_at_max_products_and_flags_truncation(monkeypatch):
    import httpx

    captured = {}
    monkeypatch.setattr(product_adapters, "WIX_MAX_PRODUCTS", 200)
    responses = [
        _dummy_httpx_response(
            {"products": [_wix_product(i) for i in range(100)], "totalResults": 250}
        ),
        _dummy_httpx_response(
            {"products": [_wix_product(i) for i in range(100, 200)], "totalResults": 250}
        ),
    ]
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _paginated_httpx_client_factory(captured, responses),
    )

    products, next_token, err = await WixProductAdapter.fetch_products(
        site_id="site_1",
        api_key="api_key_1",
        merchant_id="merch_test",
        limit=100,
        page_token=None,
    )

    assert err is None
    assert next_token == "200"
    assert len(products) == 200
    assert [req["json"]["query"]["paging"]["offset"] for req in captured["requests"]] == [
        0,
        100,
    ]


async def test_wix_fetch_products_returns_partial_results_on_mid_page_error(monkeypatch):
    import httpx

    captured = {}
    responses = [
        _dummy_httpx_response(
            {"products": [_wix_product(i) for i in range(100)], "totalResults": 150}
        ),
        _dummy_httpx_status_response(500, "upstream error"),
    ]
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _paginated_httpx_client_factory(captured, responses),
    )

    products, next_token, err = await WixProductAdapter.fetch_products(
        site_id="site_1",
        api_key="api_key_1",
        merchant_id="merch_test",
        limit=100,
        page_token=None,
    )

    assert len(products) == 100
    assert next_token is None
    assert "Wix API error: 500" in err
    assert [req["json"]["query"]["paging"]["offset"] for req in captured["requests"]] == [
        0,
        100,
    ]


async def test_wix_max_pages_one_reverts_to_single_page(monkeypatch):
    import httpx

    captured = {}
    monkeypatch.setenv("WIX_MAX_PAGES", "1")
    responses = [
        _dummy_httpx_response(
            {"products": [_wix_product(i) for i in range(100)], "totalResults": 150}
        ),
    ]
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _paginated_httpx_client_factory(captured, responses),
    )

    products, next_token, err = await WixProductAdapter.fetch_products(
        site_id="site_1",
        api_key="api_key_1",
        merchant_id="merch_test",
        limit=100,
        page_token=None,
    )

    assert err is None
    assert next_token is None
    assert len(products) == 100
    assert len(captured["requests"]) == 1


def test_wix_convert_product_emits_explicit_empty_tags():
    """Phase O-1 followup. Wix v3 API does not expose a clean tags field
    so the adapter sets tags=[] explicitly. Downstream catalog_products
    tags column then carries `[]` (not NULL) — semantic: "we looked, no
    tags from Wix" — same convention as Shopify/Woo/BigCommerce paths.
    Phase O-3 LabelAgent will fill from content for Wix rows."""

    wp = {
        "id": "prod_wix_1",
        "name": "Sample Product",
        "description": "desc",
        "visible": True,
        "priceData": {"price": 10.0, "currency": "USD"},
        "stock": {"quantity": 5, "inStock": True, "trackQuantity": True},
    }

    product = WixProductAdapter._convert_product(wp, merchant_id="merch_test")
    assert product is not None
    assert product.platform == "wix"
    # Crucial: tags is the empty list, not None / missing. The
    # ingest_standard_products write into catalog_products.tags relies
    # on this being a list.
    assert product.tags == []


def _brand_fixture(brand_value):
    wp = {
        "id": "prod_brand",
        "name": "Arolinne Calming Dog Harness",
        "description": "A padded, breathable harness for small dogs.",
        "visible": True,
        "priceData": {"price": 24.0, "currency": "USD"},
        "stock": {"quantity": 5, "inStock": True, "trackQuantity": True},
    }
    if brand_value is not None:
        wp["brand"] = brand_value
    return wp


def test_wix_convert_product_maps_brand_to_vendor():
    """Wix stores-reader v1 carries `brand` top-level; dropping it starved the
    identity layer of the ONE field it cannot live without.

    catalog_sync derives brand from `product.vendor or metadata.brand`, and
    `make_content_key(brand, title, barcode)` yields NO content_key for a
    brandless row — which cascades: no index_pipeline_state row possible (its
    PK is content_key), no eligibility, gateway gate 1 fails closed. Measured
    on the 2026-07-29 Wix pilot: 20/20 synced rows arrived brandless with
    content_key NULL and could never serve. Shopify has always mapped `vendor`.
    """
    product = WixProductAdapter._convert_product(
        _brand_fixture("Arolinne"), merchant_id="merch_test"
    )
    assert product.vendor == "Arolinne"


def test_wix_convert_product_brand_absent_or_blank_is_none():
    # Blank must behave like absent: `""` would otherwise thread through
    # catalog_sync's `str(product.vendor or ...)` and still produce no
    # content_key, but as an empty-string brand rather than an honest None.
    for value in (None, "", "   "):
        product = WixProductAdapter._convert_product(
            _brand_fixture(value), merchant_id="merch_test"
        )
        assert product.vendor is None, f"brand={value!r} must map to vendor=None"
