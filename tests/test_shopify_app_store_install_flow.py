import hashlib
import hmac
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from starlette.requests import Request


@pytest.mark.asyncio
async def test_shopify_app_store_install_starts_public_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_store_connections as module

    captured_state = {}

    async def fake_ensure_shell_merchant(domain: str) -> str:
        assert domain == "demo-shop.myshopify.com"
        return "merch_shopify_public"

    async def fake_insert_state(**kwargs):
        captured_state.update(kwargs)

    monkeypatch.setattr(module.settings, "shopify_client_id", "shopify_client_id")
    monkeypatch.setattr(module.settings, "shopify_client_secret", "shopify_secret")
    monkeypatch.setattr(module.settings, "shopify_redirect_uri", "https://api.example.com/integrations/shopify/oauth/callback")
    monkeypatch.setattr(module.settings, "shopify_scopes", "read_products,write_webhooks")
    monkeypatch.setattr(module.settings, "merchant_portal_base_url", "https://merchant.example.com")
    monkeypatch.setattr(module, "_ensure_shopify_marketplace_shell_merchant", fake_ensure_shell_merchant)
    monkeypatch.setattr(module, "_insert_shopify_oauth_state", fake_insert_state)

    response = await module.shopify_app_store_install(
        request=object(),
        shop="https://demo-shop.myshopify.com/admin",
        host="admin-host",
        embedded="1",
        redirect=False,
    )

    assert response["status"] == "success"
    assert response["merchant_id"] == "merch_shopify_public"
    assert response["shop_domain"] == "demo-shop.myshopify.com"
    assert response["install_source"] == "app_store"
    assert response["authorization_url"].startswith("https://demo-shop.myshopify.com/admin/oauth/authorize?")
    assert captured_state["merchant_id"] == "merch_shopify_public"
    assert captured_state["shop_domain"] == "demo-shop.myshopify.com"
    assert captured_state["install_source"] == "app_store"
    assert captured_state["return_to"] == "https://merchant.example.com/app/install/success"
    assert captured_state["host"] == "admin-host"


@pytest.mark.asyncio
async def test_shopify_app_store_callback_redirect_includes_merchant_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.merchant_store_connections as module
    import services.shopify_integration_verify as verify_module

    secret = "shopify_secret"
    state = "oauth-state"
    state_sha = hashlib.sha256(state.encode("utf-8")).hexdigest()
    state_row = {
        "merchant_id": "merch_shopify_public",
        "shop_domain": "demo-shop.myshopify.com",
        "expires_at": module.datetime.now(module.timezone.utc) + module.timedelta(minutes=10),
        "used_at": None,
        "install_source": "app_store",
        "return_to": "https://merchant.example.com/app/install/success",
        "host": "admin-host",
    }

    class FakeDatabase:
        async def fetch_one(self, query: str, values: dict):
            assert values["state_sha256"] == state_sha
            if "SELECT merchant_id" in query:
                return state_row
            if "UPDATE shopify_oauth_states" in query:
                return {"merchant_id": "merch_shopify_public"}
            raise AssertionError(f"unexpected query: {query}")

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url: str, json: dict):
            assert url == "https://demo-shop.myshopify.com/admin/oauth/access_token"
            assert json["code"] == "oauth-code"
            return FakeResponse(200, {"access_token": "admin-token"})

        async def get(self, url: str, headers: dict):
            assert url == "https://demo-shop.myshopify.com/admin/api/2024-07/shop.json"
            assert headers["X-Shopify-Access-Token"] == "admin-token"
            return FakeResponse(
                200,
                {"shop": {"myshopify_domain": "demo-shop.myshopify.com", "name": "Demo Shop"}},
            )

    async def fake_ensure_tables():
        return None

    async def fake_create_storefront_token(**kwargs):
        return "storefront-token"

    async def fake_upsert_store(**kwargs):
        assert kwargs["merchant_id"] == "merch_shopify_public"
        assert kwargs["myshopify_domain"] == "demo-shop.myshopify.com"
        return "store_demo"

    async def fake_register_webhooks_best_effort(**kwargs):
        return {"created": [], "existing": []}

    monkeypatch.setattr(module.settings, "shopify_client_id", "shopify_client_id")
    monkeypatch.setattr(module.settings, "shopify_client_secret", secret)
    monkeypatch.setattr(module.settings, "shopify_redirect_uri", "https://api.example.com/integrations/shopify/oauth/callback")
    monkeypatch.setattr(module, "database", FakeDatabase())
    monkeypatch.setattr(module, "_ensure_shopify_oauth_tables", fake_ensure_tables)
    monkeypatch.setattr(module, "_create_storefront_access_token_best_effort", fake_create_storefront_token)
    monkeypatch.setattr(module, "_upsert_shopify_store_credentials", fake_upsert_store)
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(verify_module, "register_webhooks_best_effort", fake_register_webhooks_best_effort)

    params = {
        "shop": "demo-shop.myshopify.com",
        "code": "oauth-code",
        "state": state,
    }
    message = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    params["hmac"] = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/integrations/shopify/oauth/callback",
            "headers": [],
            "query_string": urlencode(params).encode("utf-8"),
            "server": ("api.example.com", 443),
            "scheme": "https",
        }
    )

    response = await module.shopify_oauth_callback(request)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://merchant.example.com/app/install/success?")
    query = parse_qs(urlparse(location).query)
    assert query["installed"] == ["shopify"]
    assert query["merchant_id"] == ["merch_shopify_public"]
    assert query["shop"] == ["demo-shop.myshopify.com"]
    assert query["store_id"] == ["store_demo"]
    assert query["status"] == ["success"]


@pytest.mark.asyncio
async def test_shopify_store_credential_upsert_handles_mapping_rows_without_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.merchant_store_connections as module

    class RowWithoutCallableGet(dict):
        get = None

    executed = {}

    class FakeDatabase:
        async def fetch_one(self, query: str, values: dict):
            assert values == {
                "merchant_id": "merch_shopify_public",
                "domain": "demo-shop.myshopify.com",
            }
            return RowWithoutCallableGet(
                {
                    "store_id": "store_demo",
                    "api_key": '{"storefront_access_token":"existing-storefront-token"}',
                }
            )

        async def execute(self, query: str, values: dict):
            executed.update(values)

    monkeypatch.setattr(module, "database", FakeDatabase())

    store_id = await module._upsert_shopify_store_credentials(
        merchant_id="merch_shopify_public",
        myshopify_domain="demo-shop.myshopify.com",
        shop_name="Demo Shop",
        access_token="new-admin-token",
        storefront_token=None,
        install_source="app_store",
    )

    assert store_id == "store_demo"
    assert executed["store_id"] == "store_demo"
    token_blob = module.json.loads(executed["api_key"])
    assert token_blob["access_token"] == "new-admin-token"
    assert token_blob["storefront_access_token"] == "existing-storefront-token"
    assert token_blob["install_source"] == "app_store"


def test_resolve_shopify_app_routes_by_install_source(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_store_connections as module

    s = module.settings
    monkeypatch.setattr(s, "shopify_appstore_client_id", "A_id")
    monkeypatch.setattr(s, "shopify_appstore_client_secret", "A_secret")
    monkeypatch.setattr(s, "shopify_appstore_redirect_uri", "https://api.pivota.cc/cb")
    monkeypatch.setattr(
        s, "shopify_appstore_scopes",
        "read_products,read_orders,read_fulfillments,read_discounts,write_webhooks",
    )
    monkeypatch.setattr(s, "shopify_headless_client_id", "B_id")
    monkeypatch.setattr(s, "shopify_headless_client_secret", "B_secret")
    monkeypatch.setattr(s, "shopify_headless_redirect_uri", "https://api.pivota.cc/cb")
    monkeypatch.setattr(
        s, "shopify_headless_scopes",
        "read_products,read_orders,read_fulfillments,read_discounts,write_webhooks,write_orders",
    )

    # Every OAuth install source resolves to App A (public, read-only). The
    # write-scoped headless app must never be reachable over OAuth.
    for src in ("app_store", "merchant_portal"):
        a = module.resolve_shopify_app(src)
        assert a.label == "appstore"
        assert a.client_id == "A_id"
        assert a.client_secret == "A_secret"
        assert "write_orders" not in a.scopes

    # Non-OAuth / unknown sources fall through to the custom-token headless app.
    for src in ("", None, "whatever"):
        b = module.resolve_shopify_app(src)
        assert b.label == "headless"
        assert b.client_id == "B_id"
        assert b.client_secret == "B_secret"
        assert "write_orders" in b.scopes


def test_resolve_shopify_app_falls_back_to_single_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_store_connections as module

    s = module.settings
    for field in (
        "shopify_appstore_client_id", "shopify_appstore_client_secret", "shopify_appstore_redirect_uri",
        "shopify_headless_client_id", "shopify_headless_client_secret", "shopify_headless_redirect_uri",
    ):
        monkeypatch.setattr(s, field, None)
    monkeypatch.setattr(s, "shopify_client_id", "single_id")
    monkeypatch.setattr(s, "shopify_client_secret", "single_secret")
    monkeypatch.setattr(s, "shopify_redirect_uri", "https://api.pivota.cc/cb")

    a = module.resolve_shopify_app("app_store")
    assert a.client_id == "single_id" and a.client_secret == "single_secret"
    b = module.resolve_shopify_app("merchant_portal")
    assert b.client_id == "single_id" and b.client_secret == "single_secret"
