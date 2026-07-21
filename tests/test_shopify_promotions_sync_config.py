import pytest

from services.shopify_promotions_sync import (
    ShopifyStoreConfig,
    get_shopify_config_for_merchant,
)


@pytest.mark.asyncio
async def test_discount_sync_config_uses_merchant_store_custom_app_credentials(monkeypatch):
    calls = {}

    async def fake_connector_credentials(_merchant_id, _connector):
        return None

    async def fake_primary_store(merchant_id):
        calls["merchant_id"] = merchant_id
        return {
            "store_id": "store_1",
            "platform": "shopify",
            "domain": "example.myshopify.com",
            "api_key_raw": '{"client_id":"cid","client_secret":"secret"}',
            "api_key": "",
        }

    async def fake_resolve_token(*, shop_domain, api_key_raw, store_id):
        calls["token_args"] = {
            "shop_domain": shop_domain,
            "api_key_raw": api_key_raw,
            "store_id": store_id,
        }
        return "shpat_from_custom_app", {"refreshed": True}

    monkeypatch.setattr(
        "services.shopify_promotions_sync.get_latest_connector_credential_for_merchant",
        fake_connector_credentials,
    )
    monkeypatch.setattr("services.shopify_promotions_sync.get_primary_store", fake_primary_store)
    monkeypatch.setattr(
        "services.shopify_promotions_sync.resolve_shopify_admin_access_token",
        fake_resolve_token,
    )

    cfg = await get_shopify_config_for_merchant("merch_1")

    assert cfg == ShopifyStoreConfig(
        shop_domain="example.myshopify.com",
        access_token="shpat_from_custom_app",
    )
    assert calls["merchant_id"] == "merch_1"
    assert calls["token_args"] == {
        "shop_domain": "example.myshopify.com",
        "api_key_raw": '{"client_id":"cid","client_secret":"secret"}',
        "store_id": "store_1",
    }


@pytest.mark.asyncio
async def test_discount_sync_config_falls_back_to_env_when_merchant_store_has_no_token(monkeypatch):
    async def fake_connector_credentials(_merchant_id, _connector):
        return None

    async def fake_primary_store(_merchant_id):
        return {
            "store_id": "store_1",
            "platform": "shopify",
            "domain": "example.myshopify.com",
            "api_key_raw": "{}",
            "api_key": "",
        }

    async def fake_resolve_token(**_kwargs):
        return None, {"refreshed": False}

    async def fake_env_config():
        return ShopifyStoreConfig(
            shop_domain="env-shop.myshopify.com",
            access_token="shpat_env",
        )

    monkeypatch.setattr(
        "services.shopify_promotions_sync.get_latest_connector_credential_for_merchant",
        fake_connector_credentials,
    )
    monkeypatch.setattr("services.shopify_promotions_sync.get_primary_store", fake_primary_store)
    monkeypatch.setattr(
        "services.shopify_promotions_sync.resolve_shopify_admin_access_token",
        fake_resolve_token,
    )
    monkeypatch.setattr("services.shopify_promotions_sync._get_shopify_config_from_env", fake_env_config)

    cfg = await get_shopify_config_for_merchant("merch_1")

    assert cfg == ShopifyStoreConfig(
        shop_domain="env-shop.myshopify.com",
        access_token="shpat_env",
    )


@pytest.mark.asyncio
async def test_discount_sync_config_prefers_connector_credentials(monkeypatch):
    calls = {"store": 0}

    async def fake_connector_credentials(_merchant_id, _connector):
        return {"id": "cred_1", "credentials_encrypted": "encrypted"}

    def fake_decrypt(_encrypted):
        return {
            "shop_domain": "connector.myshopify.com",
            "access_token": "shpat_connector",
        }

    async def fake_mark_used(_credential_id):
        calls["mark_used"] = _credential_id

    async def fake_primary_store(_merchant_id):
        calls["store"] += 1
        return None

    monkeypatch.setattr(
        "services.shopify_promotions_sync.get_latest_connector_credential_for_merchant",
        fake_connector_credentials,
    )
    monkeypatch.setattr("services.shopify_promotions_sync.crypto_service.decrypt_json_secret", fake_decrypt)
    monkeypatch.setattr("services.shopify_promotions_sync.mark_credential_used", fake_mark_used)
    monkeypatch.setattr("services.shopify_promotions_sync.get_primary_store", fake_primary_store)

    cfg = await get_shopify_config_for_merchant("merch_1")

    assert cfg == ShopifyStoreConfig(
        shop_domain="connector.myshopify.com",
        access_token="shpat_connector",
    )
    assert calls["mark_used"] == "cred_1"
    assert calls["store"] == 0
