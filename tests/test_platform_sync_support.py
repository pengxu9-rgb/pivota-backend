from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adapters.product_adapters import BigCommerceProductAdapter, WooCommerceProductAdapter
from routes.universal_product_sync import prepare_platform_credentials


def test_prepare_platform_credentials_parses_woocommerce_json() -> None:
    credentials = prepare_platform_credentials(
        "woocommerce",
        {
            "domain": "shop.example.com/",
            "api_key": '{"consumer_key":"ck_test","consumer_secret":"cs_test"}',
        },
    )

    assert credentials == {
        "store_url": "https://shop.example.com",
        "consumer_key": "ck_test",
        "consumer_secret": "cs_test",
    }


def test_prepare_platform_credentials_supports_woocommerce_legacy_colon_format() -> None:
    credentials = prepare_platform_credentials(
        "woocommerce",
        {
            "domain": "https://shop.example.com/",
            "api_key": "ck_legacy:cs_legacy",
        },
    )

    assert credentials == {
        "store_url": "https://shop.example.com",
        "consumer_key": "ck_legacy",
        "consumer_secret": "cs_legacy",
    }


def test_prepare_platform_credentials_extracts_bigcommerce_store_hash_from_domain() -> None:
    credentials = prepare_platform_credentials(
        "bigcommerce",
        {
            "domain": "abc123.mybigcommerce.com",
            "api_key": '{"access_token":"token_1","client_id":"client_1"}',
        },
    )

    assert credentials == {
        "store_hash": "abc123",
        "access_token": "token_1",
        "client_id": "client_1",
    }


def test_prepare_platform_credentials_extracts_wix_blob_site_id() -> None:
    credentials = prepare_platform_credentials(
        "wix",
        {
            "domain": "https://example.wixsite.com/store",
            "api_key": '{"site_id":"site_123","api_key":"token_123"}',
        },
    )

    assert credentials == {
        "site_id": "site_123",
        "api_key": "token_123",
    }


async def test_woocommerce_fetch_products_parses_variations(monkeypatch):
    import httpx

    requests = []

    class DummyResponse:
        def __init__(self, status_code, payload, headers=None):
            self.status_code = status_code
            self._payload = payload
            self.headers = headers or {}
            self.text = "{}"

        def json(self):
            return self._payload

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None, headers=None):
            requests.append({"url": url, "params": params, "headers": headers})
            if url.endswith("/wp-json/wc/v3/products"):
                return DummyResponse(
                    200,
                    [
                        {
                            "id": 10,
                            "name": "Hydration Serum",
                            "description": "desc",
                            "type": "variable",
                            "variations": [101],
                            "images": [{"src": "https://cdn.example.com/p.jpg"}],
                            "status": "publish",
                            "price": "0",
                            "regular_price": "0",
                            "stock_status": "instock",
                            "manage_stock": False,
                            "catalog_visibility": "visible",
                        }
                    ],
                    headers={"X-WP-TotalPages": "2"},
                )
            if url.endswith("/wp-json/wc/v3/products/10/variations"):
                return DummyResponse(
                    200,
                    [
                        {
                            "id": 101,
                            "price": "12.00",
                            "regular_price": "15.00",
                            "stock_quantity": 3,
                            "stock_status": "instock",
                            "manage_stock": True,
                            "sku": "SERUM-M",
                            "attributes": [{"name": "Size", "option": "30ml"}],
                        }
                    ],
                )
            raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(httpx, "AsyncClient", DummyAsyncClient)

    products, next_token, error = await WooCommerceProductAdapter.fetch_products(
        store_url="shop.example.com",
        consumer_key="ck_test",
        consumer_secret="cs_test",
        merchant_id="merch_test",
        limit=50,
        page_token="1",
    )

    assert error is None
    assert next_token == "2"
    assert len(products) == 1
    assert products[0].platform == "woocommerce"
    assert products[0].price == 12.0
    assert products[0].inventory_quantity == 3
    assert len(products[0].variants) == 1
    assert requests[0]["url"].startswith("https://shop.example.com/wp-json/wc/v3/products")
    assert requests[1]["url"].endswith("/wp-json/wc/v3/products/10/variations")


async def test_bigcommerce_fetch_products_uses_store_hash(monkeypatch):
    import httpx

    requests = []

    class DummyResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.headers = {}
            self.text = "{}"

        def json(self):
            return self._payload

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None, headers=None):
            requests.append({"url": url, "params": params, "headers": headers})
            return DummyResponse(
                200,
                {
                    "data": [
                        {
                            "id": 77,
                            "name": "Glow Cream",
                            "description": "desc",
                            "brand_name": "Alpha",
                            "price": 20,
                            "retail_price": 25,
                            "inventory_level": 0,
                            "inventory_tracking": "variant",
                            "availability": "available",
                            "is_visible": True,
                            "images": [{"url_standard": "https://cdn.example.com/g.jpg"}],
                            "variants": [
                                {
                                    "id": 701,
                                    "sku": "GLOW-30",
                                    "price": 21,
                                    "retail_price": 26,
                                    "inventory_level": 4,
                                    "inventory_tracking": "variant",
                                    "option_values": [
                                        {
                                            "option_display_name": "Size",
                                            "label": "30ml",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                    "meta": {"pagination": {"current_page": 1, "total_pages": 2}},
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", DummyAsyncClient)

    products, next_token, error = await BigCommerceProductAdapter.fetch_products(
        store_hash="abc123.mybigcommerce.com",
        access_token="token_1",
        client_id="client_1",
        merchant_id="merch_test",
        limit=50,
        page_token="1",
    )

    assert error is None
    assert next_token == "2"
    assert len(products) == 1
    assert products[0].platform == "bigcommerce"
    assert products[0].price == 21.0
    assert products[0].inventory_quantity == 4
    assert len(products[0].variants) == 1
    assert requests[0]["url"] == "https://api.bigcommerce.com/stores/abc123/v3/catalog/products"
    assert requests[0]["params"]["include"] == "images,variants"


def _build_sync_client():
    import routes.wix_sync as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return {"role": "merchant", "merchant_id": "merch_test"}

    app.dependency_overrides[module.get_current_user] = fake_current_user
    return TestClient(app), module


def test_sync_woocommerce_route_delegates_to_universal_sync(monkeypatch) -> None:
    client, module = _build_sync_client()

    async def fake_fetch_one(query, values=None):
        assert values["platform"] == "woocommerce"
        return {
            "store_id": "store_woo_1",
            "merchant_id": "merch_test",
            "platform": "woocommerce",
            "name": "Woo Store",
        }

    async def fake_sync_products(request, background_tasks, current_user):
        assert request.platform == "woocommerce"
        assert request.merchant_id == "merch_test"
        return SimpleNamespace(
            status="success",
            message="Successfully synced 7 products from WooCommerce",
            products_synced=7,
            platform="woocommerce",
            sync_time="2026-03-31T00:00:00Z",
        )

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr("routes.product_sync.sync_products", fake_sync_products)

    # Default is now BACKGROUND (the inline form died at the edge timeout for
    # any real store — see routes/wix_sync.py). The immediate response carries
    # no product_count: claiming one before the sync ran would be a fabricated
    # success. ?wait=true below still exercises the legacy inline contract.
    response = client.post("/merchant/integrations/woocommerce/sync")

    assert response.status_code == 200
    body = response.json()
    assert body["platform"] == "woocommerce"
    assert body["status"] == "started"
    assert body["started_at"]
    assert "product_count" not in body

    response = client.post("/merchant/integrations/woocommerce/sync?wait=true")

    assert response.status_code == 200
    body = response.json()
    assert body["platform"] == "woocommerce"
    assert body["status"] == "success"
    assert body["product_count"] == 7


def test_sync_bigcommerce_route_delegates_to_universal_sync(monkeypatch) -> None:
    client, module = _build_sync_client()

    async def fake_fetch_one(query, values=None):
        assert values["platform"] == "bigcommerce"
        return {
            "store_id": "store_big_1",
            "merchant_id": "merch_test",
            "platform": "bigcommerce",
            "name": "Big Store",
        }

    async def fake_sync_products(request, background_tasks, current_user):
        assert request.platform == "bigcommerce"
        return SimpleNamespace(
            status="success",
            message="Successfully synced 5 products from BigCommerce",
            products_synced=5,
            platform="bigcommerce",
            sync_time="2026-03-31T00:00:00Z",
        )

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr("routes.product_sync.sync_products", fake_sync_products)

    response = client.post("/merchant/integrations/bigcommerce/sync?wait=true")

    assert response.status_code == 200
    body = response.json()
    assert body["platform"] == "bigcommerce"
    assert body["status"] == "success"
    assert body["product_count"] == 5
