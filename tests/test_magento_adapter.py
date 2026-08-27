import json

import pytest


def _configurable_parent():
    return {
        "id": 100,
        "sku": "TEE",
        "name": "Configurable Tee",
        "price": 0,
        "status": 1,
        "visibility": 4,
        "type_id": "configurable",
        "created_at": "2026-01-01 10:00:00",
        "updated_at": "2026-01-02 10:00:00",
        "custom_attributes": [
            {"attribute_code": "description", "value": "A useful tee"},
            {"attribute_code": "url_key", "value": "configurable-tee"},
            {"attribute_code": "image", "value": "/t/e/tee.jpg"},
        ],
        "extension_attributes": {
            "configurable_product_options": [
                {
                    "attribute_code": "color",
                    "values": [{"value_index": 52, "label": "Blue"}],
                }
            ]
        },
    }


def _configurable_child():
    return {
        "id": 101,
        "sku": "TEE-BLUE",
        "name": "Blue Tee",
        "price": 25,
        "status": 1,
        "visibility": 1,
        "type_id": "simple",
        "custom_attributes": [{"attribute_code": "color", "value": "52"}],
        "extension_attributes": {
            "stock_item": {"qty": 3, "is_in_stock": True, "manage_stock": True}
        },
    }


def test_magento_product_adapter_maps_configurable_children_conservatively():
    from adapters.magento_adapter import MagentoProductAdapter

    product = MagentoProductAdapter.convert_product(
        _configurable_parent(),
        merchant_id="merchant-1",
        store_url="https://shop.example",
        currency="usd",
        children=[_configurable_child()],
        product_url_suffix=".html",
    )

    assert product.platform == "magento"
    assert product.id == "100"
    assert product.price == 25
    assert product.currency == "USD"
    assert product.inventory_quantity == 3
    assert product.orderable is True
    assert product.handle == "configurable-tee"
    assert product.online_store_url == "https://shop.example/configurable-tee.html"
    assert product.image_url == "https://shop.example/media/catalog/product/t/e/tee.jpg"
    assert product.variants[0].sku == "TEE-BLUE"
    assert product.variants[0].options == {"Color": "Blue"}


def test_magento_product_without_stock_state_is_not_claimed_orderable():
    from adapters.magento_adapter import MagentoProductAdapter

    product = MagentoProductAdapter.convert_product(
        {
            "id": 5,
            "sku": "NO-STOCK-FACT",
            "name": "Unknown inventory",
            "price": 10,
            "status": 1,
            "visibility": 4,
            "type_id": "simple",
        },
        merchant_id="merchant-1",
        store_url="https://shop.example",
    )

    assert product.inventory_quantity == 0
    assert product.orderable is False
    assert product.in_stock is False


@pytest.mark.asyncio
async def test_magento_connection_test_reads_catalog_and_store_configuration(monkeypatch):
    from adapters import magento_adapter as module

    class FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            if url.endswith("/products"):
                return FakeResponse({"items": [], "total_count": 12})
            return FakeResponse(
                [
                    {
                        "code": "default",
                        "name": "Primary Store",
                        "base_currency_code": "CAD",
                        "product_url_suffix": ".htm",
                    }
                ]
            )

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)

    result = await module.MagentoAdapter(
        {
            "store_url": "shop.example",
            "access_token": "integration-token",
            "store_view_code": "default",
        }
    ).test_connection()

    assert result == {
        "success": True,
        "store_name": "Primary Store",
        "product_count": 12,
        "currency": "CAD",
        "product_url_suffix": ".htm",
    }


@pytest.mark.asyncio
async def test_magento_fetch_products_uses_search_pagination_and_children(monkeypatch):
    from adapters import magento_adapter as module

    requests = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            requests.append((url, kwargs))
            if url.endswith("/products"):
                return FakeResponse(
                    200,
                    {"items": [_configurable_parent()], "total_count": 3},
                )
            return FakeResponse(200, [_configurable_child()])

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)

    products, next_page, error = await module.MagentoProductAdapter.fetch_products(
        store_url="shop.example/",
        access_token="secret-token",
        merchant_id="merchant-1",
        limit=1,
        page_token="1",
        store_view_code="default",
        currency="USD",
    )

    assert error is None
    assert next_page == "2"
    assert len(products) == 1
    assert requests[0][0] == "https://shop.example/rest/default/V1/products"
    assert requests[0][1]["params"]["searchCriteria[pageSize]"] == 1
    assert requests[0][1]["headers"]["Authorization"] == "Bearer secret-token"
    assert requests[1][0].endswith("/configurable-products/TEE/children")


def test_prepare_platform_credentials_parses_magento_blob():
    from routes.universal_product_sync import prepare_platform_credentials

    credentials = prepare_platform_credentials(
        "magento",
        {
            "domain": "commerce.example/",
            "api_key": json.dumps(
                {
                    "access_token": "integration-token",
                    "store_view_code": "us_en",
                    "currency": "eur",
                }
            ),
        },
    )

    assert credentials == {
        "store_url": "https://commerce.example",
        "access_token": "integration-token",
        "store_view_code": "us_en",
        "currency": "EUR",
        "product_url_suffix": None,
    }


@pytest.mark.asyncio
async def test_magento_connect_persists_secret_but_does_not_return_it(monkeypatch):
    from routes import magento_integration as route

    writes = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return None

        async def execute(self, query, values):
            writes.append((query, values))

    class FakeAdapter:
        def __init__(self, config):
            self.store_url = "https://shop.example"

        def validate_config(self):
            return True, None

        async def test_connection(self):
            return {"success": True, "store_name": "Example", "product_count": 8}

    lifecycle_calls = []

    async def fake_sync(merchant_id, reason):
        lifecycle_calls.append((merchant_id, reason))

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "MagentoAdapter", FakeAdapter)
    monkeypatch.setattr(route, "sync_catalog_merchant_status", fake_sync)

    result = await route.connect_magento(
        route.MagentoConnectRequest(
            merchant_id="merchant-1",
            store_url="shop.example",
            access_token="top-secret",
            store_view_code="default",
            currency="USD",
        ),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    persisted = json.loads(writes[0][1]["api_key"])
    assert persisted["access_token"] == "top-secret"
    assert "top-secret" not in json.dumps(result)
    assert result["platform"] == "magento"
    assert result["product_count"] == 8
    assert lifecycle_calls == [("merchant-1", "magento_connect")]


def test_magento_is_registered_as_native_catalog_source():
    from adapters.product_adapters import PLATFORM_ADAPTERS
    from services.commerce_source_registry import get_commerce_source

    assert "magento" in PLATFORM_ADAPTERS
    source = get_commerce_source("magento")
    assert source is not None
    assert source.capabilities.catalog_pull is True
    assert source.capabilities.catalog_events is False
    assert source.capabilities.live_quote is False
    assert source.capabilities.checkout is False
