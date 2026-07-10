import asyncio
import json
from datetime import datetime

from services.merchant_psp_config_service import (
    build_stripe_connect_provider_config,
    build_provider_connect_record,
    build_runtime_adapter_kwargs,
    evaluate_psp_readiness,
    fetch_active_runtime_merchant_psp,
    infer_runtime_provider,
    persist_canonical_merchant_psp,
    should_reset_stripe_webhook_config,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_build_provider_connect_record_normalizes_adyen_fields() -> None:
    record = build_provider_connect_record(
        "adyen",
        api_key="live_adyen_key",
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
        api_key="sk_test_checkout_key",
        account_id="pc_123",
        provider_config={"public_key": "pk_123"},
        environment="test",
    )

    assert kwargs["processing_channel_id"] == "pc_123"
    assert kwargs["public_key"] == "pk_123"
    assert kwargs["environment"] == "test"


def test_build_runtime_adapter_kwargs_prefers_key_environment_over_row_value() -> None:
    kwargs = build_runtime_adapter_kwargs(
        "checkout",
        api_key="sk_test_checkout_key",
        account_id="pc_123",
        provider_config={"public_key": "pk_live_123"},
        environment="live",
    )

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


def test_build_provider_connect_record_prefers_key_environment_over_explicit_live_flag() -> None:
    record = build_provider_connect_record(
        "stripe",
        api_key="sk_test_123",
        provider_config={"mode": "payment_intent"},
        environment="live",
        validation_status="valid",
    )

    assert record["environment"] == "test"


def test_build_provider_connect_record_accepts_publishable_key_alias_for_stripe() -> None:
    record = build_provider_connect_record(
        "stripe",
        api_key="sk_live_123",
        provider_config={
            "mode": "payment_intent",
            "publishable_key": "pk_live_alias_123",
            "webhook_endpoint_id": "we_123",
            "webhook_endpoint_secret": "whsec_123",
        },
        environment="live",
        validation_status="valid",
    )

    assert record["provider_config"]["public_key"] == "pk_live_alias_123"
    assert record["provider_summary"]["public_key_present"] is True


def test_infer_runtime_provider_prefers_explicit_psp_used() -> None:
    provider = infer_runtime_provider(
        psp_used="checkout",
        psp_id="psp_stripe_live_123",
        payment_reference="pi_test_123",
    )

    assert provider == "checkout"


def test_infer_runtime_provider_derives_from_psp_id_and_payment_reference() -> None:
    assert infer_runtime_provider(psp_id="psp_adyen_live_123") == "adyen"
    assert infer_runtime_provider(payment_reference="pay_checkout_123") == "checkout"


def test_evaluate_psp_readiness_marks_live_valid_stripe_as_ready() -> None:
    readiness = evaluate_psp_readiness(
        "stripe",
        status="active",
        api_key="sk_live_123",
        provider_config={
            "mode": "payment_intent",
            "public_key": "pk_live_123",
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
        provider_config={"mode": "payment_intent", "public_key": "pk_live_123"},
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


# --- P-T2.3.7 item 4: Adyen live URL prefix ---

def test_build_provider_connect_record_persists_adyen_live_url_prefix() -> None:
    record = build_provider_connect_record(
        "adyen",
        api_key="live_adyen_key",
        account_id="PivotaLiveECOM",
        provider_config={"client_key": "pub_client_key", "live_url_prefix": "1797a-Pivota"},
        environment="live",
        validation_status="valid",
    )

    assert record["provider_config"]["live_url_prefix"] == "1797a-Pivota"
    assert record["provider_summary"]["live_url_prefix_present"] is True


def test_normalize_adyen_accepts_camel_and_endpoint_prefix_aliases() -> None:
    for cfg in ({"liveUrlPrefix": "abc-Pivota"}, {"endpoint_prefix": "abc-Pivota"}):
        record = build_provider_connect_record(
            "adyen", api_key="live_adyen_key", account_id="ECOM",
            provider_config={"client_key": "k", **cfg}, environment="live",
        )
        assert record["provider_config"]["live_url_prefix"] == "abc-Pivota"


def test_evaluate_psp_readiness_not_gated_on_adyen_live_url_prefix() -> None:
    # The live_url_prefix is needed only by the ACP off-session MIT capture (enforced
    # in _AdyenCaptureAdapter). General live-charge readiness must NOT depend on it —
    # else existing Adyen dropin/routing merchants would be wrongly dropped.
    readiness = evaluate_psp_readiness(
        "adyen",
        status="active",
        api_key="live_adyen_key",
        account_id="PivotaLiveECOM",
        provider_config={"merchant_account": "PivotaLiveECOM", "client_key": "pub"},
        environment="live",
        validation_status="valid",
    )

    assert "Adyen live URL prefix is missing" not in readiness["readiness_blockers"]
    assert readiness["live_charge_ready"] is True


def test_evaluate_psp_readiness_adyen_live_with_prefix_is_ready() -> None:
    readiness = evaluate_psp_readiness(
        "adyen",
        status="active",
        api_key="live_adyen_key",
        account_id="PivotaLiveECOM",
        provider_config={
            "merchant_account": "PivotaLiveECOM",
            "client_key": "pub",
            "live_url_prefix": "1797a-Pivota",
        },
        environment="live",
        validation_status="valid",
    )

    assert "Adyen live URL prefix is missing" not in readiness["readiness_blockers"]
    assert readiness["live_charge_ready"] is True


def test_evaluate_psp_readiness_blocks_mismatched_live_flag_with_test_stripe_key() -> None:
    readiness = evaluate_psp_readiness(
        "stripe",
        status="active",
        api_key="sk_test_123",
        provider_config={
            "mode": "payment_intent",
            "public_key": "pk_test_123",
            "webhook_endpoint_id": "we_123",
            "webhook_endpoint_secret": "whsec_123",
        },
        environment="live",
        validation_status="valid",
    )

    assert readiness["environment"] == "test"
    assert readiness["live_charge_ready"] is False
    assert "Processor is configured for test, not live" in readiness["readiness_blockers"]


def test_build_provider_connect_record_preserves_stripe_webhook_fields() -> None:
    record = build_provider_connect_record(
        "stripe",
        api_key="sk_live_123",
        provider_config={
            "mode": "payment_intent",
            "public_key": "pk_live_123",
            "webhook_endpoint_id": "we_123",
            "webhook_endpoint_secret": "whsec_123",
            "webhook_url": "https://api.pivota.cc/webhooks/stripe/psp_stripe_123",
        },
        environment="live",
        validation_status="valid",
    )

    assert record["provider_config"]["webhook_endpoint_id"] == "we_123"
    assert record["provider_config"]["webhook_endpoint_secret"] == "whsec_123"
    assert record["provider_summary"]["public_key_present"] is True
    assert record["provider_summary"]["webhook_ready"] is True


def test_should_reset_stripe_webhook_config_when_key_changes() -> None:
    assert should_reset_stripe_webhook_config(
        previous_api_key="sk_test_old",
        previous_environment="test",
        next_api_key="sk_live_new",
        next_environment="live",
    ) is True


def test_build_stripe_connect_provider_config_preserves_webhook_when_identity_matches() -> None:
    config = build_stripe_connect_provider_config(
        existing_provider_config={
            "mode": "payment_intent",
            "public_key": "pk_live_existing",
            "webhook_endpoint_id": "we_existing",
            "webhook_endpoint_secret": "whsec_existing",
            "webhook_url": "https://api.pivota.cc/webhooks/stripe/psp_stripe_existing",
        },
        previous_api_key="sk_live_same",
        previous_account_id="acct_same",
        previous_environment="live",
        next_api_key="sk_live_same",
        next_account_id="acct_same",
        next_environment="live",
    )

    assert config["webhook_endpoint_id"] == "we_existing"
    assert config["webhook_endpoint_secret"] == "whsec_existing"
    assert config["public_key"] == "pk_live_existing"


def test_build_stripe_connect_provider_config_clears_webhook_when_identity_changes() -> None:
    config = build_stripe_connect_provider_config(
        existing_provider_config={
            "mode": "payment_intent",
            "public_key": "pk_test_existing",
            "webhook_endpoint_id": "we_existing",
            "webhook_endpoint_secret": "whsec_existing",
            "webhook_url": "https://api.pivota.cc/webhooks/stripe/psp_stripe_existing",
        },
        previous_api_key="sk_test_old",
        previous_account_id="acct_old",
        previous_environment="test",
        next_api_key="sk_live_new",
        next_account_id="acct_new",
        next_environment="live",
    )

    assert config == {"mode": "payment_intent"}


def test_evaluate_psp_readiness_blocks_missing_stripe_public_key() -> None:
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

    assert readiness["live_charge_ready"] is False
    assert "Stripe public key is missing" in readiness["readiness_blockers"]


def test_build_stripe_connect_provider_config_replaces_public_key_without_resetting_webhook() -> None:
    config = build_stripe_connect_provider_config(
        existing_provider_config={
            "mode": "payment_intent",
            "public_key": "pk_live_old",
            "webhook_endpoint_id": "we_existing",
            "webhook_endpoint_secret": "whsec_existing",
        },
        previous_api_key="sk_live_same",
        previous_account_id="acct_same",
        previous_environment="live",
        next_api_key="sk_live_same",
        next_account_id="acct_same",
        next_environment="live",
        public_key="pk_live_new",
    )

    assert config["public_key"] == "pk_live_new"
    assert config["webhook_endpoint_id"] == "we_existing"


def test_fetch_active_runtime_merchant_psp_uses_database_override() -> None:
    class _FakeDB:
        def __init__(self) -> None:
            self.fetch_one_calls = []

        async def fetch_one(self, query, values=None):
            self.fetch_one_calls.append((" ".join(str(query).split()), dict(values or {})))
            return {
                "psp_id": "psp_stripe_live_123",
                "merchant_id": "merch_123",
                "provider": "stripe",
                "api_key": "sk_live_123",
                "secret_key": None,
                "environment": "live",
                "provider_config": {"mode": "payment_intent"},
                "runtime_secret_key": "sk_live_123",
            }

        async def fetch_all(self, query, values=None):
            raise AssertionError("fetch_all should not be called when psp_id is provided")

    fake_db = _FakeDB()
    row = _run(
        fetch_active_runtime_merchant_psp(
            merchant_id="merch_123",
            psp_id="psp_stripe_live_123",
            database_override=fake_db,
        )
    )

    assert row is not None
    assert row["runtime_secret_key"] == "sk_live_123"
    assert fake_db.fetch_one_calls[0][1] == {
        "merchant_id": "merch_123",
        "psp_id": "psp_stripe_live_123",
    }


def test_persist_canonical_merchant_psp_resets_stripe_webhook_state_when_identity_changes(
    monkeypatch,
) -> None:
    import services.merchant_psp_config_service as module

    executed = []

    async def fake_execute(query, values=None):
        executed.append((" ".join(query.split()), dict(values or {})))

    monkeypatch.setattr(module.database, "execute", fake_execute)

    result = _run(
        persist_canonical_merchant_psp(
            merchant_id="merch_123",
            provider="stripe",
            api_key="sk_live_new",
            account_id="acct_live_new",
            environment="live",
            name="Stripe Account",
            capabilities=["payments"],
            status="active",
            psp_id="psp_stripe_existing",
            existing_row={
                "psp_id": "psp_stripe_existing",
                "merchant_id": "merch_123",
                "provider": "stripe",
                "name": "Stripe Account",
                "api_key": "sk_test_old",
                "account_id": "acct_test_old",
                "secret_key": None,
                "capabilities": "payments",
                "status": "active",
                "connected_at": datetime(2026, 4, 1, 12, 0, 0),
                "environment": "test",
                "provider_config": {
                    "mode": "payment_intent",
                    "webhook_endpoint_id": "we_old",
                    "webhook_endpoint_secret": "whsec_old",
                    "webhook_url": "https://api.pivota.cc/webhooks/stripe/psp_stripe_existing",
                },
                "validation_status": "valid",
                "validation_error": None,
                "last_validated_at": datetime(2026, 4, 1, 12, 5, 0),
            },
        )
    )

    update_values = next(
        values for query, values in executed if query.startswith("UPDATE merchant_psps SET merchant_id = :merchant_id")
    )
    provider_config = json.loads(update_values["provider_config"])

    assert provider_config == {
        "mode": "payment_intent",
        "account_id": "acct_live_new",
    }
    assert update_values["validation_status"] == "unknown"
    assert update_values["validation_error"] is None
    assert update_values["last_validated_at"] is None
    assert result["truth_changed"] is True


def test_persist_canonical_merchant_psp_preserves_validation_when_truth_is_unchanged(
    monkeypatch,
) -> None:
    import services.merchant_psp_config_service as module

    executed = []
    validated_at = datetime(2026, 4, 1, 12, 5, 0)

    async def fake_execute(query, values=None):
        executed.append((" ".join(query.split()), dict(values or {})))

    monkeypatch.setattr(module.database, "execute", fake_execute)

    result = _run(
        persist_canonical_merchant_psp(
            merchant_id="merch_123",
            provider="stripe",
            api_key="sk_live_same",
            account_id="acct_same",
            environment="live",
            provider_config={
                "mode": "payment_intent",
                "webhook_endpoint_id": "we_same",
                "webhook_endpoint_secret": "whsec_same",
            },
            name="Stripe Account",
            capabilities=["payments"],
            status="inactive",
            psp_id="psp_stripe_existing",
            existing_row={
                "psp_id": "psp_stripe_existing",
                "merchant_id": "merch_123",
                "provider": "stripe",
                "name": "Stripe Account",
                "api_key": "sk_live_same",
                "account_id": "acct_same",
                "secret_key": None,
                "capabilities": "payments",
                "status": "active",
                "connected_at": datetime(2026, 4, 1, 12, 0, 0),
                "environment": "live",
                "provider_config": {
                    "mode": "payment_intent",
                    "webhook_endpoint_id": "we_same",
                    "webhook_endpoint_secret": "whsec_same",
                },
                "validation_status": "valid",
                "validation_error": None,
                "last_validated_at": validated_at,
            },
        )
    )

    update_values = next(
        values for query, values in executed if query.startswith("UPDATE merchant_psps SET merchant_id = :merchant_id")
    )

    assert update_values["validation_status"] == "valid"
    assert update_values["validation_error"] is None
    assert update_values["last_validated_at"] == validated_at
    assert result["truth_changed"] is False
