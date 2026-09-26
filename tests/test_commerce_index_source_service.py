import pytest

from services import commerce_index_source_service as module


def test_source_id_normalizes_legacy_antom_to_ucp() -> None:
    assert module.commerce_index_source_id("merchant_123", "antom", "payment") == module.commerce_index_source_id(
        "merchant_123", "antom_ucp", "payment"
    )


def test_source_timestamps_are_naive_utc_for_v2_timestamp_columns() -> None:
    assert module._utcnow().tzinfo is None


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
async def test_public_web_is_active_evidence_only_without_merchant_consent(monkeypatch) -> None:
    async def fake_execute(*_args, **_kwargs):
        return None

    monkeypatch.setattr(module.database, "execute", fake_execute)
    result = await module.register_commerce_index_source(
        merchant_id="agent_seed::brand",
        provider="public_web",
        status="active",
        source_metadata={"base_url": "https://brand.example", "crawl_policy": {"evidence_only": True, "robots_checked": True}},
    )
    assert result["integration_layer"] == "evidence"
    assert result["capabilities_json"]["catalog_pull"] is False
    assert result["refresh_policy_json"]["catalog_refresh"] == "forbidden"


@pytest.mark.asyncio
async def test_public_web_requires_robots_checked() -> None:
    with pytest.raises(ValueError, match="robots_checked"):
        await module.register_commerce_index_source(
            merchant_id="agent_seed::brand", provider="public_web", status="active",
            source_metadata={"base_url": "https://brand.example", "crawl_policy": {"evidence_only": True}},
        )


@pytest.mark.asyncio
async def test_source_metadata_rejects_credentials() -> None:
    with pytest.raises(ValueError, match="must not contain credentials"):
        await module.register_commerce_index_source(
            merchant_id="merchant_123",
            provider="shopify",
            source_metadata={"api_key": "do-not-store"},
        )


@pytest.mark.asyncio
async def test_source_metadata_rejects_credentials_nested_in_lists() -> None:
    with pytest.raises(ValueError, match="must not contain credentials"):
        await module.register_commerce_index_source(
            merchant_id="merchant_123",
            provider="shopify",
            source_metadata={"connections": [{"api_key": "do-not-store"}]},
        )


@pytest.mark.asyncio
async def test_resolve_active_catalog_source_requires_active_consent(monkeypatch) -> None:
    async def fake_fetch_one(_query):
        return {
            "source_id": "ci_source_shopify",
            "merchant_id": "merchant_123",
            "provider": "shopify",
            "integration_layer": "catalog",
            "status": "active",
            "consent_ref": "shopify-oauth:abc",
        }

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    source = await module.resolve_active_catalog_source(merchant_id="merchant_123", provider="shopify")

    assert source is not None
    assert source["source_id"] == "ci_source_shopify"
    assert source["field_source_kind"] == "merchant_api"


@pytest.mark.asyncio
async def test_payment_provider_cannot_resolve_as_catalog_source(monkeypatch) -> None:
    async def should_not_query(_query):
        raise AssertionError("payment source must not be queried as a catalog authority")

    monkeypatch.setattr(module.database, "fetch_one", should_not_query)
    assert await module.resolve_active_catalog_source(merchant_id="merchant_123", provider="antom") is None
