import pytest


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
