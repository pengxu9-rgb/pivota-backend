import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException


def _signed_wix_instance(module, payload: dict, secret: str) -> str:
    data_b64 = module._b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig_b64 = module._b64url(hmac.new(secret.encode("utf-8"), data_b64.encode("utf-8"), hashlib.sha256).digest())
    return f"{sig_b64}.{data_b64}"


@pytest.mark.asyncio
async def test_shopify_app_store_install_starts_public_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_store_connections as module

    captured_state = {}

    async def fake_ensure_shell_merchant(**kwargs):
        assert kwargs["platform"] == "shopify"
        assert kwargs["domain"] == "demo-shop.myshopify.com"
        return "merch_shopify_public"

    async def fake_insert_state(**kwargs):
        captured_state.update(kwargs)

    monkeypatch.setattr(module.settings, "shopify_client_id", "shopify_client_id")
    monkeypatch.setattr(module.settings, "shopify_client_secret", "shopify_secret")
    monkeypatch.setattr(module.settings, "shopify_redirect_uri", "https://api.example.com/integrations/shopify/oauth/callback")
    monkeypatch.setattr(module.settings, "shopify_scopes", "read_products,write_webhooks")
    monkeypatch.setattr(module.settings, "merchant_portal_base_url", "https://merchant.example.com")
    monkeypatch.setattr(module, "_ensure_marketplace_shell_merchant", fake_ensure_shell_merchant)
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


def test_decode_wix_signed_instance_verifies_hmac() -> None:
    import routes.merchant_store_connections as module

    secret = "wix_secret"
    instance = _signed_wix_instance(
        module,
        {
            "instanceId": "wix-instance-1",
            "uid": "site-owner",
            "siteOwnerId": "site-owner",
        },
        secret,
    )

    decoded = module._decode_wix_signed_instance(instance, secret)
    assert decoded["instanceId"] == "wix-instance-1"

    bad_instance = f"bad.{instance.split('.', 1)[1]}"
    with pytest.raises(HTTPException) as excinfo:
        module._decode_wix_signed_instance(bad_instance, secret)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_wix_instance_install_persists_oauth_store(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_store_connections as module

    secret = "wix_secret"
    instance = _signed_wix_instance(
        module,
        {
            "instanceId": "wix-instance-2",
            "uid": "site-owner",
            "siteOwnerId": "site-owner",
        },
        secret,
    )
    captured_store = {}

    async def fake_create_access_token(**kwargs):
        assert kwargs["client_id"] == "wix_client_id"
        assert kwargs["client_secret"] == secret
        assert kwargs["instance_id"] == "wix-instance-2"
        return {"access_token": "wix_access_token", "expires_in": 3600}

    async def fake_fetch_app_instance(access_token: str):
        assert access_token == "wix_access_token"
        return {
            "instance": {"appName": "Pivota"},
            "site": {
                "siteId": "wix-site-2",
                "siteDisplayName": "Demo Wix Store",
                "url": "https://demo.wixsite.com/store",
                "ownerEmail": "owner@example.com",
            },
        }

    async def fake_ensure_shell(**kwargs):
        assert kwargs["instance_id"] == "wix-instance-2"
        assert kwargs["site_id"] == "wix-site-2"
        assert kwargs["display_name"] == "Demo Wix Store"
        assert kwargs["owner_email"] == "owner@example.com"
        return "merch_wix_public"

    async def fake_upsert_store(**kwargs):
        captured_store.update(kwargs)
        return "store_wix_public"

    monkeypatch.setenv("WIX_APP_CLIENT_ID", "wix_client_id")
    monkeypatch.setenv("WIX_APP_CLIENT_SECRET", secret)
    monkeypatch.setattr(module, "_create_wix_app_access_token", fake_create_access_token)
    monkeypatch.setattr(module, "_fetch_wix_app_instance", fake_fetch_app_instance)
    monkeypatch.setattr(module, "_ensure_wix_marketplace_shell_merchant", fake_ensure_shell)
    monkeypatch.setattr(module, "_upsert_wix_oauth_store", fake_upsert_store)

    response = await module._complete_wix_instance_install(instance=instance, redirect=False)

    assert response == {
        "status": "success",
        "platform": "wix",
        "merchant_id": "merch_wix_public",
        "site_id": "wix-site-2",
        "instance_id": "wix-instance-2",
        "store_id": "store_wix_public",
        "store_name": "Demo Wix Store",
    }
    assert captured_store["merchant_id"] == "merch_wix_public"
    assert captured_store["site_id"] == "wix-site-2"
    assert captured_store["instance_id"] == "wix-instance-2"
    assert captured_store["access_token"] == "wix_access_token"
    assert captured_store["token_payload"]["expires_in"] == 3600
