from services.merchant_psp_config_service import (
    build_provider_connect_record,
    build_runtime_adapter_kwargs,
    evaluate_psp_readiness,
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


def test_build_provider_connect_record_infers_environment_from_key_when_row_is_unknown() -> None:
    record = build_provider_connect_record(
        "checkout",
        api_key="sk_live_checkout_secret",
        account_id="pc_live_123",
        provider_config={"public_key": "pk_live_123"},
        environment="unknown",
        validation_status="unknown",
    )

    assert record["environment"] == "live"


def test_build_provider_connect_record_infers_test_environment_from_sandbox_checkout_key() -> None:
    record = build_provider_connect_record(
        "checkout",
        api_key="sk_sbox_example_checkout_validation_key",
        account_id="pc_test_123",
        provider_config={"public_key": "pk_sbox_123"},
        environment="unknown",
        validation_status="unknown",
    )

    assert record["environment"] == "test"


def test_evaluate_psp_readiness_marks_live_valid_stripe_as_ready() -> None:
    readiness = evaluate_psp_readiness(
        "stripe",
        status="active",
        api_key="sk_live_123",
        provider_config={
            "mode": "payment_intent",
            "webhook_endpoint_id": "we_123",
            "webhook_endpoint_secret": "whsec_123",
        },
        environment="live",
        validation_status="valid",
    )

    assert readiness["live_charge_ready"] is True
    assert readiness["readiness_blockers"] == []


def test_evaluate_psp_readiness_blocks_live_stripe_without_webhook_endpoint() -> None:
    readiness = evaluate_psp_readiness(
        "stripe",
        status="active",
        api_key="sk_live_123",
        provider_config={"mode": "payment_intent"},
        environment="live",
        validation_status="valid",
    )

    assert readiness["live_charge_ready"] is False
    assert "Stripe webhook endpoint is not configured" in readiness["readiness_blockers"]


def test_evaluate_psp_readiness_blocks_missing_adyen_client_key() -> None:
    readiness = evaluate_psp_readiness(
        "adyen",
        status="active",
        api_key="live_adyen_key",
        account_id="WoopayECOM",
        provider_config={"merchant_account": "WoopayECOM"},
        environment="live",
        validation_status="unknown",
    )

    assert readiness["live_charge_ready"] is False
    assert "Adyen client key is missing" in readiness["readiness_blockers"]
    assert "Processor validation has not been run" in readiness["readiness_blockers"]


def test_build_provider_connect_record_preserves_stripe_webhook_fields() -> None:
    record = build_provider_connect_record(
        "stripe",
        api_key="sk_live_123",
        provider_config={
            "mode": "payment_intent",
            "webhook_endpoint_id": "we_123",
            "webhook_endpoint_secret": "whsec_123",
            "webhook_url": "https://api.pivota.cc/webhooks/stripe/psp_stripe_123",
        },
        environment="live",
        validation_status="valid",
    )

    assert record["provider_config"]["webhook_endpoint_id"] == "we_123"
    assert record["provider_config"]["webhook_endpoint_secret"] == "whsec_123"
    assert record["provider_summary"]["webhook_ready"] is True
