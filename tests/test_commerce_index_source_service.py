import pytest

from services import commerce_index_source_service as module


def test_source_id_normalizes_legacy_antom_to_ucp() -> None:
    assert module.commerce_index_source_id("merchant_123", "antom", "payment") == module.commerce_index_source_id(
        "merchant_123", "antom_ucp", "payment"
    )


@pytest.mark.asyncio
async def test_active_catalog_source_records_authority_and_refresh_policy(monkeypatch) -> None:
    writes = []

    async def fake_execute(query, values=None):
        writes.append(str(query))

    monkeypatch.setattr(module.database, "execute", fake_execute)
    result = await module.register_commerce_index_source(
        merchant_id="merchant_123",
        provider="shopify",
        status="active",
        consent_ref="shopify-oauth-installation:abc",
        source_metadata={"store_id": "shop_123"},
    )

    assert result["provider"] == "shopify"
    assert result["integration_layer"] == "catalog"
    assert result["capabilities_json"]["catalog_events"] is True
    assert result["refresh_policy_json"]["mode"] == "events_plus_pull"
    assert len(writes) == 1
    assert "ON CONFLICT" in writes[0]


@pytest.mark.asyncio
async def test_antom_ucp_can_be_authorized_without_catalog_capability(monkeypatch) -> None:
    async def fake_execute(*_args, **_kwargs):
        return None

    monkeypatch.setattr(module.database, "execute", fake_execute)
    result = await module.register_commerce_index_source(
        merchant_id="merchant_123",
        provider="antom",
        status="active",
        consent_ref="antom-merchant-agreement:abc",
    )

    assert result["provider"] == "antom_ucp"
    assert result["integration_layer"] == "payment"
    assert result["capabilities_json"]["catalog_pull"] is False
    assert result["refresh_policy_json"]["catalog_refresh"] == "not_applicable"


@pytest.mark.asyncio
async def test_antom_catalog_stays_pending_until_a_feed_adapter_exists() -> None:
    with pytest.raises(ValueError, match="contracted feed adapter"):
        await module.register_commerce_index_source(
            merchant_id="merchant_123",
            provider="antom_catalog",
            status="active",
            consent_ref="antom-catalog-agreement:abc",
        )


@pytest.mark.asyncio
async def test_source_metadata_rejects_credentials() -> None:
    with pytest.raises(ValueError, match="must not contain credentials"):
        await module.register_commerce_index_source(
            merchant_id="merchant_123",
            provider="shopify",
            source_metadata={"api_key": "do-not-store"},
        )
