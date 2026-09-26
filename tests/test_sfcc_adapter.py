import json

import pytest


def _sfcc_product():
    return {
        "id": "master-1",
        "name": "SFCC Serum",
        "longDescription": "A site-scoped serum",
        "brand": "Cloud Beauty",
        "primaryCategoryId": "skin-serums",
        "currency": "USD",
        "price": 45,
        "orderable": True,
        "inventory": {"stockLevel": 8, "orderable": True},
        "link": "/products/master-1.html",
        "imageGroups": [
            {"viewType": "large", "images": [{"link": "https://img.example/master-1.jpg"}]}
        ],
        "variants": [
            {
                "productId": "variant-30",
                "price": 45,
                "orderable": True,
                "inventory": {"stockLevel": 3, "orderable": True},
                "variationValues": {"size": "30ml"},
            },
            {
                "productId": "variant-50",
                "price": 60,
                "orderable": True,
                "inventory": {"stockLevel": 5, "orderable": True},
                "variationValues": {"size": "50ml"},
            },
        ],
    }


def test_sfcc_product_maps_scapi_details_variants_and_availability():
    from adapters.sfcc_adapter import SalesforceCommerceCloudProductAdapter

    product = SalesforceCommerceCloudProductAdapter.convert_product(
        _sfcc_product(),
        merchant_id="merchant-1",
        storefront_url="store.example.com",
    )

    assert product.platform == "salesforce_commerce_cloud"
    assert product.title == "SFCC Serum"
    assert product.price == 45
    assert product.inventory_quantity == 8
    assert product.orderable is True
    assert product.online_store_url == "https://store.example.com/products/master-1.html"
    assert product.image_url == "https://img.example/master-1.jpg"
    assert [variant.id for variant in product.variants] == ["variant-30", "variant-50"]


def test_sfcc_master_is_sellable_when_native_variants_are_orderable():
    from adapters.sfcc_adapter import SalesforceCommerceCloudProductAdapter

    raw = _sfcc_product()
    raw["orderable"] = False
    raw["inventory"] = {"stockLevel": 0, "orderable": False}
    product = SalesforceCommerceCloudProductAdapter.convert_product(
        raw,
        merchant_id="merchant-1",
    )

    assert product.orderable is True
    assert product.inventory_quantity == 8


def test_sfcc_orderable_without_exact_ats_uses_explicit_sentinel():
    from adapters.sfcc_adapter import SalesforceCommerceCloudProductAdapter

    raw = _sfcc_product()
    raw.pop("inventory")
    raw["variants"] = []
    product = SalesforceCommerceCloudProductAdapter.convert_product(
        raw,
        merchant_id="merchant-1",
    )

    assert product.inventory_quantity == 1
    assert product.orderable is True
    assert product.variants[0].platform_metadata["inventory_quantity_is_sentinel"] is True


@pytest.mark.asyncio
async def test_sfcc_fetch_gets_slas_token_searches_and_hydrates(monkeypatch):
    from adapters import sfcc_adapter as module

    module._SLAS_TOKEN_CACHE.clear()
    calls = []

    class Response:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code
            self.text = ""

        def json(self):
            return self._payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            return Response({"access_token": "slas-token", "expires_in": 1800})

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            if url.endswith("/product-search"):
                return Response(
                    {
                        "hits": [{"productId": f"master-{index}"} for index in range(25)],
                        "total": 26,
                    }
                )
            products = []
            for product_id in kwargs["params"]["ids"].split(","):
                raw = _sfcc_product()
                raw["id"] = product_id
                raw["variants"] = []
                products.append(raw)
            return Response({"data": products})

    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    products, next_token, error = await module.SalesforceCommerceCloudProductAdapter.fetch_products(
        short_code="kv7kzm70",
        organization_id="f_ecom_abcd_dev",
        site_id="RefArchGlobal",
        client_id="client-123",
        client_secret="secret",
        merchant_id="merchant-1",
        limit=100,
        storefront_url="store.example.com",
    )

    assert error is None
    assert len(products) == 25
    assert next_token == "25"
    token_call = calls[0]
    assert token_call[2]["data"] == {
        "grant_type": "client_credentials",
        "channel_id": "RefArchGlobal",
    }
    search_call = calls[1]
    assert search_call[2]["params"]["limit"] == 100
    assert search_call[2]["headers"]["Authorization"] == "Bearer slas-token"
    first_detail_call = calls[2]
    second_detail_call = calls[3]
    assert len(first_detail_call[2]["params"]["ids"].split(",")) == 24
    assert second_detail_call[2]["params"]["ids"] == "master-24"
    assert first_detail_call[2]["params"]["expand"] == "availability,images,prices,variations"


def test_universal_credentials_support_sfcc():
    from routes.universal_product_sync import prepare_platform_credentials

    credentials = {
        "short_code": "kv7kzm70",
        "organization_id": "f_ecom_abcd_dev",
        "site_id": "RefArchGlobal",
        "client_id": "client-123",
        "client_secret": "secret",
        "currency": "EUR",
        "locale": "de-DE",
        "storefront_url": "http://store.example.com/path",
    }
    result = prepare_platform_credentials(
        "salesforce_commerce_cloud",
        {
            "domain": "https://kv7kzm70.api.commercecloud.salesforce.com",
            "api_key": json.dumps(credentials),
        },
    )

    assert result == {
        **credentials,
        "storefront_url": "https://store.example.com",
    }


@pytest.mark.asyncio
async def test_sfcc_connect_persists_secret_but_never_returns_it(monkeypatch):
    from routes import sfcc_integration as route

    writes = []

    class FakeDB:
        async def fetch_all(self, *args, **kwargs):
            return []

        async def execute(self, query, values):
            writes.append(values)

    class Adapter:
        def __init__(self, config):
            self.short_code = "kv7kzm70"
            self.organization_id = "f_ecom_abcd_dev"
            self.site_id = "RefArchGlobal"
            self.client_id = "client-123"

        def validate_config(self):
            return True, None

        async def test_connection(self):
            return {"success": True, "store_name": "RefArchGlobal"}

    lifecycle = []

    async def fake_lifecycle(merchant_id, reason):
        lifecycle.append((merchant_id, reason))

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "SalesforceCommerceCloudAdapter", Adapter)
    monkeypatch.setattr(route, "sync_catalog_merchant_status", fake_lifecycle)
    result = await route.connect_salesforce_commerce_cloud(
        route.SalesforceCommerceCloudConnectRequest(
            merchant_id="merchant-1",
            short_code="kv7kzm70",
            organization_id="f_ecom_abcd_dev",
            site_id="RefArchGlobal",
            client_id="client-123",
            client_secret="secret",
            storefront_url="store.example.com",
        ),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    persisted = json.loads(writes[0]["api_key"])
    assert persisted["client_secret"] == "secret"
    assert "secret" not in json.dumps(result)
    assert result["platform"] == "salesforce_commerce_cloud"
    assert lifecycle == [("merchant-1", "salesforce_commerce_cloud_connect")]


@pytest.mark.asyncio
async def test_sfcc_reconnect_preserves_telemetry_secret(monkeypatch):
    from routes import sfcc_integration as route

    writes = []

    class FakeDB:
        async def fetch_all(self, *args, **kwargs):
            return [{
                "store_id": "store-sfcc",
                "api_key": json.dumps(
                    {
                        "organization_id": "f_ecom_abcd_dev",
                        "site_id": "RefArchGlobal",
                        "telemetry_signing_secret": "keep-me",
                    }
                ),
            }]

        async def fetch_one(self, query, values):
            if query.lstrip().startswith("UPDATE"):
                writes.append(values)
                return {"store_id": "store-sfcc"}
            return None

    class Adapter:
        def __init__(self, config):
            self.short_code = "kv7kzm70"
            self.organization_id = "f_ecom_abcd_dev"
            self.site_id = "RefArchGlobal"
            self.client_id = "client-123"

        def validate_config(self):
            return True, None

        async def test_connection(self):
            return {"success": True}

    async def fake_lifecycle(*args, **kwargs):
        return None

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "SalesforceCommerceCloudAdapter", Adapter)
    monkeypatch.setattr(route, "sync_catalog_merchant_status", fake_lifecycle)
    result = await route.connect_salesforce_commerce_cloud(
        route.SalesforceCommerceCloudConnectRequest(
            merchant_id="merchant-1",
            short_code="kv7kzm70",
            organization_id="f_ecom_abcd_dev",
            site_id="RefArchGlobal",
            client_id="client-123",
            client_secret="new-slas-secret",
        ),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    persisted = json.loads(writes[0]["api_key"])
    assert persisted["telemetry_signing_secret"] == "keep-me"
    assert persisted["client_secret"] == "new-slas-secret"
    assert result["telemetry_configured"] is True
    assert "keep-me" not in json.dumps(result)


@pytest.mark.asyncio
async def test_sfcc_reconnect_retries_cas_and_preserves_concurrently_provisioned_secret(
    monkeypatch,
):
    from routes import sfcc_integration as route

    stale = json.dumps(
        {"organization_id": "f_ecom_abcd_dev", "site_id": "RefArchGlobal"}
    )
    concurrent = json.dumps(
        {
            "organization_id": "f_ecom_abcd_dev",
            "site_id": "RefArchGlobal",
            "telemetry_signing_secret": "concurrent-secret",
        }
    )
    update_attempts = []

    class FakeDB:
        async def fetch_all(self, *args, **kwargs):
            return [{"store_id": "store-sfcc", "api_key": stale}]

        async def fetch_one(self, query, values):
            if query.lstrip().startswith("SELECT"):
                return {"api_key": concurrent}
            update_attempts.append(values)
            if len(update_attempts) == 1:
                return None
            return {"store_id": "store-sfcc"}

    class Adapter:
        def __init__(self, config):
            self.short_code = "kv7kzm70"
            self.organization_id = "f_ecom_abcd_dev"
            self.site_id = "RefArchGlobal"
            self.client_id = "client-123"

        def validate_config(self):
            return True, None

        async def test_connection(self):
            return {"success": True}

    async def fake_lifecycle(*args, **kwargs):
        return None

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "SalesforceCommerceCloudAdapter", Adapter)
    monkeypatch.setattr(route, "sync_catalog_merchant_status", fake_lifecycle)
    result = await route.connect_salesforce_commerce_cloud(
        route.SalesforceCommerceCloudConnectRequest(
            merchant_id="merchant-1",
            short_code="kv7kzm70",
            organization_id="f_ecom_abcd_dev",
            site_id="RefArchGlobal",
            client_id="client-123",
            client_secret="new-slas-secret",
        ),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    persisted = json.loads(update_attempts[-1]["api_key"])
    assert persisted["telemetry_signing_secret"] == "concurrent-secret"
    assert result["telemetry_configured"] is True


@pytest.mark.asyncio
async def test_sfcc_connect_keeps_same_origin_sites_as_separate_stores(monkeypatch):
    from routes import sfcc_integration as route

    writes = []

    class FakeDB:
        async def fetch_all(self, query, *args, **kwargs):
            assert "LIMIT 1" not in query.upper()
            return [
                {
                    "store_id": "store-site-a",
                    "api_key": json.dumps(
                        {
                            "organization_id": "f_ecom_abcd_dev",
                            "site_id": "SiteA",
                            "telemetry_signing_secret": "site-a-secret",
                        }
                    ),
                }
            ]

        async def execute(self, query, values):
            writes.append((query, values))

    class Adapter:
        def __init__(self, config):
            self.short_code = "kv7kzm70"
            self.organization_id = "f_ecom_abcd_dev"
            self.site_id = "SiteB"
            self.client_id = "client-123"

        def validate_config(self):
            return True, None

        async def test_connection(self):
            return {"success": True}

    async def fake_lifecycle(*args, **kwargs):
        return None

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "SalesforceCommerceCloudAdapter", Adapter)
    monkeypatch.setattr(route, "sync_catalog_merchant_status", fake_lifecycle)
    result = await route.connect_salesforce_commerce_cloud(
        route.SalesforceCommerceCloudConnectRequest(
            merchant_id="merchant-1",
            short_code="kv7kzm70",
            organization_id="f_ecom_abcd_dev",
            site_id="SiteB",
            client_id="client-123",
            client_secret="site-b-secret",
        ),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    assert "INSERT INTO merchant_stores" in writes[0][0]
    assert writes[0][1]["store_id"] != "store-site-a"
    assert len(writes[0][1]["store_id"]) <= 50
    assert result["telemetry_configured"] is False
    assert "site-a-secret" not in json.dumps(writes[0][1])


def test_sfcc_registry_is_catalog_pull_only():
    from services.commerce_source_registry import get_commerce_source

    source = get_commerce_source("salesforce_commerce_cloud")
    assert source is not None
    assert source.capabilities.catalog_pull is True
    assert source.capabilities.catalog_events is False
    assert source.capabilities.live_quote is False
    assert source.capabilities.checkout is False
