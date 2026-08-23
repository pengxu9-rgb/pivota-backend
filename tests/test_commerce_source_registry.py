from services.commerce_source_registry import catalog_sync_blocker, get_commerce_source
from services.merchant_psp_config_service import (
    build_provider_summary,
    build_runtime_adapter_kwargs,
    evaluate_psp_readiness,
    normalize_provider_config,
)


def test_storefront_source_is_catalog_enabled() -> None:
    shopify = get_commerce_source("Shopify")

    assert shopify is not None
    assert shopify.capabilities.catalog_pull is True
    assert catalog_sync_blocker("shopify") is None


def test_antom_ucp_is_payment_only_until_authorized_catalog_feed_exists() -> None:
    antom = get_commerce_source("antom")

    assert antom is not None
    assert antom.provider == "antom_ucp"
    assert antom.source_kind == "payment_orchestration"
    assert antom.integration_layer == "payment"
    assert antom.capabilities.catalog_pull is False
    assert antom.capabilities.payment_webhooks is True
    assert "merchant-authorized" in (catalog_sync_blocker("antom") or "")


def test_antom_catalog_is_a_separate_not_yet_enabled_catalogue_connector() -> None:
    antom_catalog = get_commerce_source("antom_catalog")

    assert antom_catalog is not None
    assert antom_catalog.source_kind == "catalog_feed"
    assert antom_catalog.integration_layer == "catalog"
    assert antom_catalog.capabilities.catalog_pull is False
    assert "separate merchant-authorized feed" in (catalog_sync_blocker("antom_catalog") or "")


def test_antom_configuration_never_claims_live_payment_execution() -> None:
    config = normalize_provider_config(
        "antom",
        account_id="merchant_123",
        environment="sandbox",
        provider_config={"client_id": "client_123", "webhook_url": "https://example.com/webhooks/antom"},
    )

    assert config == {
        "merchant_id": "merchant_123",
        "client_id": "client_123",
        "webhook_url": "https://example.com/webhooks/antom",
        "environment": "test",
    }
    summary = build_provider_summary(
        "antom", api_key="merchant_api_key", account_id="merchant_123", provider_config=config, environment="sandbox"
    )
    assert summary["client_id_present"] is True
    assert build_runtime_adapter_kwargs("antom", account_id="merchant_123", provider_config=config, environment="sandbox") == {
        "merchant_id": "merchant_123",
        "client_id": "client_123",
        "environment": "test",
    }
    readiness = evaluate_psp_readiness(
        "antom",
        status="active",
        api_key="merchant_api_key",
        account_id="merchant_123",
        provider_config=config,
        environment="sandbox",
        validation_status="valid",
    )
    assert readiness["live_charge_ready"] is False
    assert "Antom payment execution is not enabled for this merchant" in readiness["readiness_blockers"]
