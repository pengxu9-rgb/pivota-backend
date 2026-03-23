from services.merchant_psp_config_service import (
    build_provider_connect_record,
    build_runtime_adapter_kwargs,
)


def test_build_provider_connect_record_normalizes_adyen_fields() -> None:
    record = build_provider_connect_record(
        "adyen",
        api_key="test_adyen_key",
        account_id="WoopayECOM",
        provider_config={"client_key": "pub_client_key"},
        environment="live",
        validation_status="valid",
    )

    assert record["environment"] == "live"
    assert record["provider_config"]["merchant_account"] == "WoopayECOM"
    assert record["provider_config"]["client_key"] == "pub_client_key"
    assert record["provider_summary"]["client_key_present"] is True


def test_build_runtime_adapter_kwargs_checkout_uses_processing_channel() -> None:
    kwargs = build_runtime_adapter_kwargs(
        "checkout",
        account_id="pc_123",
        provider_config={"public_key": "pk_123"},
        environment="test",
    )

    assert kwargs["processing_channel_id"] == "pc_123"
    assert kwargs["public_key"] == "pk_123"
    assert kwargs["environment"] == "test"
