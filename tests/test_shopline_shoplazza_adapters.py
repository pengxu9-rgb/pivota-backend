import json

import pytest


def _shopline_product():
    return {
        "id": "sl-product-1",
        "title": "SHOPLINE Serum",
        "body_html": "Hydrating serum",
        "vendor": "Brand SL",
        "product_category": "Skincare",
        "tags": "serum, hydrating",
        "status": "active",
        "handle": "shopline-serum",
        "path": "/products/shopline-serum",
        "published_at": "2026-08-01T10:00:00+00:00",
        "media": [{"content_type": "IMAGE", "src": "https://img.example/sl.jpg"}],
        "variants": [
            {
                "id": "sl-variant-1",
                "title": "30ml",
                "sku": "SL-30",
                "price": "24.00",
                "compare_at_price": "30.00",
                "inventory_quantity": 4,
                "inventory_tracker": True,
                "inventory_policy": "deny",
                "option1": "30ml",
            }
        ],
    }


def _shoplazza_product():
    return {
        "id": "sz-product-1",
        "title": "Shoplazza Cream",
        "description": "Barrier cream",
        "vendor": "Brand SZ",
        "product_type": "Moisturizer",
        "tags": ["cream", "barrier"],
        "published": True,
        "available": True,
        "inventory_tracking": True,
        "inventory_policy": "deny",
        "handle": "shoplazza-cream",
        "url": "/products/shoplazza-cream",
        "primary_image": {"src": "https://img.example/sz.jpg"},
        "variants": [
            {
                "id": "sz-variant-1",
                "title": "50ml",
                "sku": "SZ-50",
                "price": 32.5,
                "inventory_quantity": 6,
                "option1": "50ml",
            }
        ],
    }


def test_shopline_product_maps_native_variants_and_storefront_path():
    from adapters.shopline_adapter import ShoplineProductAdapter

    product = ShoplineProductAdapter.convert_product(
        _shopline_product(),
        merchant_id="merchant-1",
        handle="demo",
        currency="sgd",
    )

    assert product.platform == "shopline"
    assert product.price == 24
    assert product.currency == "SGD"
    assert product.inventory_quantity == 4
    assert product.orderable is True
    assert product.online_store_url == "https://demo.myshopline.com/products/shopline-serum"
    assert product.variants[0].sku == "SL-30"


def test_shoplazza_product_maps_native_variants_and_relative_url():
    from adapters.shoplazza_adapter import ShoplazzaProductAdapter

    product = ShoplazzaProductAdapter.convert_product(
        _shoplazza_product(),
        merchant_id="merchant-1",
        store_url="https://demo.myshoplaza.com",
        currency="usd",
    )

    assert product.platform == "shoplazza"
    assert product.price == 32.5
    assert product.inventory_quantity == 6
    assert product.orderable is True
    assert product.image_url == "https://img.example/sz.jpg"
    assert product.online_store_url == "https://demo.myshoplaza.com/products/shoplazza-cream"


def test_shoplazza_store_url_forces_https_before_sending_admin_token():
    from adapters.shoplazza_adapter import normalize_shoplazza_store_url

    assert normalize_shoplazza_store_url("http://DEMO.myshoplaza.com/products") == (
        "https://demo.myshoplaza.com"
    )


@pytest.mark.asyncio
async def test_shopline_fetch_uses_bearer_and_link_page_info(monkeypatch):
    from adapters import shopline_adapter as module

    calls = []

    class Response:
        status_code = 200
        text = ""
        headers = {
            "link": '<https://demo.myshopline.com/admin/openapi/v20260601/products/products.json?limit=1&page_info=next-token>; rel="next"'
        }

        def json(self):
            return {"products": [_shopline_product()]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    products, cursor, error = await module.ShoplineProductAdapter.fetch_products(
        handle="demo.myshopline.com",
        access_token="sl-token",
        merchant_id="merchant-1",
        limit=1,
    )

    assert error is None
    assert len(products) == 1
    assert cursor == "next-token"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer sl-token"
    assert calls[0][0].endswith("/v20260601/products/products.json")


@pytest.mark.asyncio
async def test_shoplazza_fetch_uses_access_token_and_cursor(monkeypatch):
    from adapters import shoplazza_adapter as module

    calls = []

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"data": {"products": [_shoplazza_product()], "cursor": "sz-next"}}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    products, cursor, error = await module.ShoplazzaProductAdapter.fetch_products(
        store_url="demo.myshoplaza.com/path",
        access_token="sz-token",
        merchant_id="merchant-1",
        limit=200,
    )

    assert error is None
    assert len(products) == 1
    assert cursor == "sz-next"
    assert calls[0][1]["headers"]["Access-Token"] == "sz-token"
    assert calls[0][1]["params"]["per_page"] == 200
    assert calls[0][0] == "https://demo.myshoplaza.com/openapi/2026-01/products"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["shopline", "shoplazza"])
async def test_catalog_page_fails_closed_when_every_native_product_is_invalid(monkeypatch, platform):
    if platform == "shopline":
        from adapters import shopline_adapter as module

        payload = {"products": [{"id": "broken", "variants": []}]}
        fetch = module.ShoplineProductAdapter.fetch_products
        kwargs = {"handle": "demo", "access_token": "token", "merchant_id": "merchant-1"}
    else:
        from adapters import shoplazza_adapter as module

        payload = {"data": {"products": [{"id": "broken", "variants": []}]}}
        fetch = module.ShoplazzaProductAdapter.fetch_products
        kwargs = {
            "store_url": "demo.myshoplaza.com",
            "access_token": "token",
            "merchant_id": "merchant-1",
        }

    class Response:
        status_code = 200
        text = ""
        headers = {}

        def json(self):
            return payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    products, cursor, error = await fetch(**kwargs)

    assert products == []
    assert cursor is None
    assert error and "no products could be mapped" in error


@pytest.mark.parametrize(
    ("platform", "store", "expected"),
    [
        (
            "shopline",
            {
                "domain": "demo.myshopline.com",
                "api_key": json.dumps(
                    {"access_token": "sl", "handle": "demo", "api_version": "v20260601", "currency": "SGD"}
                ),
            },
            {"access_token": "sl", "api_version": "v20260601", "currency": "SGD", "handle": "demo"},
        ),
        (
            "shoplazza",
            {
                "domain": "demo.myshoplaza.com",
                "api_key": json.dumps(
                    {"access_token": "sz", "api_version": "2026-01", "currency": "USD"}
                ),
            },
            {
                "access_token": "sz",
                "api_version": "2026-01",
                "currency": "USD",
                "store_url": "https://demo.myshoplaza.com",
            },
        ),
    ],
)
def test_universal_credentials_support_shopline_family(platform, store, expected):
    from routes.universal_product_sync import prepare_platform_credentials

    assert prepare_platform_credentials(platform, store) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["shopline", "shoplazza"])
async def test_shopline_family_connect_persists_token_without_returning_it(monkeypatch, platform):
    from routes import shopline_integrations as route

    writes = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return None

        async def execute(self, query, values):
            writes.append(values)

    class ShoplineFake:
        def __init__(self, config):
            self.handle = "demo"
            self.api_version = "v20260601"

        def validate_config(self):
            return True, None

        async def test_connection(self):
            return {"success": True, "store_name": "Demo SL"}

    class ShoplazzaFake:
        def __init__(self, config):
            self.store_url = "https://demo.myshoplaza.com"
            self.api_version = "2026-01"

        def validate_config(self):
            return True, None

        async def test_connection(self):
            return {"success": True, "store_name": "Demo SZ"}

    lifecycle = []

    async def fake_lifecycle(merchant_id, reason):
        lifecycle.append((merchant_id, reason))

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ShoplineAdapter", ShoplineFake)
    monkeypatch.setattr(route, "ShoplazzaAdapter", ShoplazzaFake)
    monkeypatch.setattr(route, "sync_catalog_merchant_status", fake_lifecycle)
    user = {"role": "merchant", "merchant_id": "merchant-1"}
    if platform == "shopline":
        result = await route.connect_shopline(
            route.ShoplineConnectRequest(
                merchant_id="merchant-1",
                handle="demo",
                access_token="secret",
                app_secret="signing-secret",
            ),
            current_user=user,
        )
    else:
        result = await route.connect_shoplazza(
            route.ShoplazzaConnectRequest(
                merchant_id="merchant-1",
                store_url="demo.myshoplaza.com",
                access_token="secret",
                app_secret="signing-secret",
            ),
            current_user=user,
        )
    persisted = json.loads(writes[0]["api_key"])
    assert persisted["access_token"] == "secret"
    assert persisted["app_secret"] == "signing-secret"
    assert "signing-secret" not in json.dumps(result)
    assert result["platform"] == platform
    assert result["webhook_path"] == f"/webhooks/{platform}/{result['store_id']}"
    assert result["required_webhook_topics"]
    assert lifecycle == [("merchant-1", f"{platform}_connect")]


def test_shopline_family_registry_is_catalog_pull_only():
    from services.commerce_source_registry import get_commerce_source

    for platform in ("shopline", "shoplazza"):
        source = get_commerce_source(platform)
        assert source is not None
        assert source.capabilities.catalog_pull is True
        assert source.capabilities.catalog_events is False
        assert source.capabilities.checkout is False
