import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client():
    import routes.merchant_dashboard_routes as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return {
            "role": "merchant",
            "merchant_id": "merch_test_integrations",
            "email": "merchant@example.com",
        }

    app.dependency_overrides[module.get_current_user] = fake_current_user
    return TestClient(app), module


def test_get_api_credentials_returns_real_key(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_fetch_one(query, values=None):
        assert values["merchant_id"] == "merch_test_integrations"
        return {
            "merchant_id": "merch_test_integrations",
            "api_key": "pk_live_realmerchantkey",
            "api_key_hash": "hash",
            "updated_at": None,
            "created_at": None,
        }

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    response = client.get("/merchant/api-credentials")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["issued"] is True
    assert body["data"]["api_key"] == "pk_live_realmerchantkey"
    assert body["data"]["api_key_last4"] == "tkey"
    assert body["data"]["header_name"] == "X-Merchant-API-Key"
    assert body["data"]["sample_endpoint"] == "/payment/execute"


def test_rotate_api_credentials_persists_new_key(monkeypatch) -> None:
    client, module = _build_client()
    executed = {}

    async def fake_fetch_one(query, values=None):
        return {"merchant_id": "merch_test_integrations"}

    async def fake_execute(query, values=None):
        executed.update(values or {})

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "execute", fake_execute)

    response = client.post("/merchant/api-credentials/rotate")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["api_key"].startswith("pk_live_")
    assert executed["merchant_id"] == "merch_test_integrations"
    assert executed["api_key"] == body["data"]["api_key"]
    assert len(executed["api_key_hash"]) == 64


def test_webhook_routes_use_real_service_contract(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_get_config(merchant_id: str):
        assert merchant_id == "merch_test_integrations"
        return {
            "url": "https://merchant.example/webhooks/pivota",
            "events": ["order.created", "payment.completed"],
            "enabled": True,
            "signing_secret_last4": "abcd",
            "last_test_at": None,
            "last_test_status": "delivered",
            "delivery_summary_24h": {"total": 1, "succeeded": 1, "failed": 0, "retrying": 0},
        }

    async def fake_update(merchant_id: str, *, enabled: bool, destination_url: str, subscribed_events):
        assert merchant_id == "merch_test_integrations"
        assert enabled is True
        assert destination_url == "https://merchant.example/webhooks/pivota"
        assert subscribed_events == ["order.created", "payment.completed"]
        return await fake_get_config(merchant_id)

    async def fake_get_secret(merchant_id: str):
        return {
            "status": "success",
            "signing_secret": "whsec_test_secret",
            "signing_secret_last4": "cret",
        }

    async def fake_rotate(merchant_id: str):
        return {
            "status": "success",
            "new_signing_secret": "whsec_rotated_secret",
            "signing_secret_last4": "cret",
        }

    async def fake_test(merchant_id: str, *, event_type: str, request_id=None):
        assert event_type == "payment.completed"
        return {
            "delivery_id": "mwh_123",
            "event_type": event_type,
            "status": "delivered",
        }

    async def fake_deliveries(merchant_id: str, *, limit: int = 25, status=None):
        assert limit == 20
        return {
            "status": "success",
            "deliveries": [
                {
                    "delivery_id": "mwh_123",
                    "event_type": "payment.completed",
                    "status": "delivered",
                }
            ],
            "summary_24h": {"total": 1, "succeeded": 1, "failed": 0, "retrying": 0},
        }

    monkeypatch.setattr(module, "get_merchant_webhook_config", fake_get_config)
    monkeypatch.setattr(module, "update_merchant_webhook_config", fake_update)
    monkeypatch.setattr(module, "get_merchant_webhook_signing_secret", fake_get_secret)
    monkeypatch.setattr(module, "rotate_merchant_webhook_signing_secret", fake_rotate)
    monkeypatch.setattr(module, "send_merchant_test_webhook", fake_test)
    monkeypatch.setattr(module, "list_merchant_webhook_deliveries", fake_deliveries)

    get_response = client.get("/merchant/webhooks/config")
    put_response = client.put(
        "/merchant/webhooks/config",
        json={
            "url": "https://merchant.example/webhooks/pivota",
            "events": ["order.created", "payment.completed"],
            "enabled": True,
        },
    )
    secret_response = client.get("/merchant/webhooks/secret")
    rotate_response = client.post("/merchant/webhooks/secret/rotate")
    test_response = client.post("/merchant/webhooks/test", json={"event_type": "payment.completed"})
    deliveries_response = client.get("/merchant/webhooks/deliveries?limit=20")

    assert get_response.status_code == 200
    assert get_response.json()["data"]["url"] == "https://merchant.example/webhooks/pivota"
    assert put_response.status_code == 200
    assert put_response.json()["data"]["enabled"] is True
    assert secret_response.status_code == 200
    assert secret_response.json()["signing_secret"] == "whsec_test_secret"
    assert rotate_response.status_code == 200
    assert rotate_response.json()["new_signing_secret"] == "whsec_rotated_secret"
    assert test_response.status_code == 200
    assert test_response.json()["data"]["delivery_id"] == "mwh_123"
    assert deliveries_response.status_code == 200
    assert deliveries_response.json()["data"]["deliveries"][0]["event_type"] == "payment.completed"


def test_get_merchant_psps_returns_environment_and_provider_summary(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_fetch_all(query, values=None):
        query_norm = " ".join(query.split())
        if "payment_attempts" in query_norm:
            return [
                {
                    "psp_id": "psp_adyen_1",
                    "provider": "adyen",
                    "total_count": 0,
                    "success_count": 0,
                    "total_volume": 0,
                }
            ]
        if "LEFT JOIN orders" in query_norm:
            return [
                {
                    "psp_id": "psp_adyen_1",
                    "provider": "adyen",
                    "total_count": 0,
                    "success_count": 0,
                    "total_volume": 0,
                }
            ]
        if "FROM merchant_psps" in query_norm:
            return [
                {
                    "psp_id": "psp_adyen_1",
                    "provider": "adyen",
                    "name": "Adyen Account",
                    "account_id": "WoopayECOM",
                    "status": "active",
                    "connected_at": None,
                    "capabilities": "card,bank_transfer",
                    "api_key": "live_adyen_key",
                    "environment": "live",
                    "provider_config": {"merchant_account": "WoopayECOM", "client_key": "pub_123"},
                    "validation_status": "valid",
                    "validation_error": None,
                    "last_validated_at": None,
                }
            ]
        if "FROM orders" in query_norm:
            return []
        raise AssertionError(f"Unexpected query: {query_norm}")

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    response = client.get("/merchant/merch_test_integrations/psps")

    assert response.status_code == 200
    payload = response.json()["data"]["psps"][0]
    assert payload["environment"] == "live"
    assert payload["validation_status"] == "valid"
    assert payload["provider_summary"]["merchant_account"] == "WoopayECOM"
    assert payload["provider_summary"]["client_key_present"] is True
    assert payload["live_charge_ready"] is True
    assert payload["readiness_blockers"] == []
    assert payload["payment_telemetry_reported"] is True
    assert payload["payment_telemetry_state"] == "no_activity"
    assert payload["success_rate"] is None
    assert payload["volume_today"] == 0
    assert payload["transaction_count"] == 0


def test_get_merchant_psps_reports_real_attempt_telemetry(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_fetch_all(query, values=None):
        query_norm = " ".join(query.split())
        if "payment_attempts" in query_norm:
            return [
                {
                    "psp_id": "psp_stripe_1",
                    "provider": "stripe",
                    "total_count": 4,
                    "success_count": 3,
                    "total_volume": 125.5,
                }
            ]
        if "FROM merchant_psps" in query_norm:
            return [
                {
                    "psp_id": "psp_stripe_1",
                    "provider": "stripe",
                    "name": "Stripe Account",
                    "account_id": "acct_live_123",
                    "status": "active",
                    "connected_at": None,
                    "capabilities": "card",
                    "api_key": "sk_live_stripe_secret",
                    "environment": "live",
                    "provider_config": {
                        "mode": "payment_intent",
                        "public_key": "pk_live_stripe_public",
                        "webhook_endpoint_id": "we_live_123",
                        "webhook_endpoint_secret": "whsec_live_123",
                    },
                    "validation_status": "valid",
                    "validation_error": None,
                    "last_validated_at": None,
                }
            ]
        raise AssertionError(f"Unexpected query: {query_norm}")

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    response = client.get("/merchant/merch_test_integrations/psps")

    assert response.status_code == 200
    payload = response.json()["data"]["psps"][0]
    assert payload["payment_telemetry_reported"] is True
    assert payload["payment_telemetry_source"] == "payment_attempts"
    assert payload["payment_telemetry_window"] == "utc_day"
    assert payload["success_rate"] == 75.0
    assert payload["volume_today"] == 125.5
    assert payload["transaction_count"] == 4


def test_test_psp_connection_persists_environment_and_validation_truth(monkeypatch) -> None:
    client, module = _build_client()
    executed = []
    adapter_calls = []

    async def fake_fetch_one(query, values=None):
        assert values["psp_id"] == "psp_checkout_1"
        return {
            "provider": "checkout",
            "api_key": "sk_live_checkout_secret",
            "secret_key": None,
            "account_id": "pc_live_123",
            "merchant_id": "merch_test_integrations",
            "status": "active",
            "environment": "unknown",
            "provider_config": {
                "processing_channel_id": "pc_live_123",
                "public_key": "pk_live_123",
            },
            "validation_status": "unknown",
            "validation_error": None,
        }

    async def fake_execute(query, values=None):
        executed.append({"query": " ".join(query.split()), "values": dict(values or {})})

    class FakeCheckoutAdapter:
        async def create_payment_intent(self, amount, currency, metadata):
            adapter_calls.append(
                {
                    "amount": str(amount),
                    "currency": currency,
                    "metadata": dict(metadata),
                }
            )
            return True, object(), None

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module, "get_psp_adapter", lambda provider, api_key, **kwargs: FakeCheckoutAdapter())

    response = client.post("/merchant/psp/psp_checkout_1/test")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["environment"] == "live"
    assert body["data"]["validation_status"] == "valid"
    assert body["data"]["live_charge_ready"] is True
    assert len(adapter_calls) == 1
    assert adapter_calls[0]["amount"] == "0.01"
    assert adapter_calls[0]["currency"] == "USD"
    assert adapter_calls[0]["metadata"]["order_id"] == "checkout_validation_psp_checkout_1"
    assert executed
    assert executed[0]["values"]["environment"] == "live"
    assert executed[0]["values"]["validation_status"] == "valid"


def test_test_psp_connection_provisions_stripe_webhook_and_persists_truth(monkeypatch) -> None:
    client, module = _build_client()
    executed = []

    async def fake_fetch_one(query, values=None):
        assert values["psp_id"] == "psp_stripe_live_1"
        return {
            "provider": "stripe",
            "api_key": "sk_live_stripe_secret",
            "secret_key": None,
            "account_id": None,
            "merchant_id": "merch_test_integrations",
            "status": "active",
            "environment": "live",
            "provider_config": {"mode": "payment_intent", "public_key": "pk_live_stripe_public"},
            "validation_status": "unknown",
            "validation_error": None,
        }

    async def fake_execute(query, values=None):
        executed.append({"query": " ".join(query.split()), "values": dict(values or {})})

    async def fake_ensure_stripe_webhook_endpoint(**kwargs):
        assert kwargs["psp_id"] == "psp_stripe_live_1"
        assert kwargs["environment"] == "live"
        return (
            {
                "mode": "payment_intent",
                "public_key": "pk_live_stripe_public",
                "webhook_endpoint_id": "we_live_123",
                "webhook_endpoint_secret": "whsec_live_123",
                "webhook_url": "https://api.pivota.cc/webhooks/stripe/psp_stripe_live_1",
            },
            True,
        )

    import stripe as stripe_sdk

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module, "_ensure_stripe_webhook_endpoint", fake_ensure_stripe_webhook_endpoint)
    monkeypatch.setattr(stripe_sdk.Balance, "retrieve", staticmethod(lambda **kwargs: {"object": "balance"}))

    response = client.post("/merchant/psp/psp_stripe_live_1/test")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["environment"] == "live"
    assert body["data"]["validation_status"] == "valid"
    assert body["data"]["live_charge_ready"] is True
    assert executed
    provider_config = executed[0]["values"]["provider_config"]
    assert '"webhook_endpoint_id": "we_live_123"' in provider_config
    assert executed[0]["values"]["validation_status"] == "valid"


def test_ensure_stripe_webhook_endpoint_handles_object_style_stripe_responses(monkeypatch) -> None:
    _, module = _build_client()

    class FakeStripeObject:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeWebhookEndpoint:
        @staticmethod
        def create(**kwargs):
            return FakeStripeObject(id="we_live_obj", secret="whsec_live_obj")

    class FakeStripeSDK:
        api_key = None
        WebhookEndpoint = FakeWebhookEndpoint

    monkeypatch.setitem(sys.modules, "stripe", FakeStripeSDK)

    provider_config, created = module.asyncio.run(
        module._ensure_stripe_webhook_endpoint(
            psp_id="psp_stripe_live_obj",
            api_key="sk_live_stripe_secret",
            provider_config={"mode": "payment_intent"},
            account_id=None,
            environment="live",
        )
    )

    assert created is True
    assert provider_config["webhook_endpoint_id"] == "we_live_obj"
    assert provider_config["webhook_endpoint_secret"] == "whsec_live_obj"
    assert provider_config["webhook_url"].endswith("/webhooks/stripe/psp_stripe_live_obj")


def test_ensure_stripe_webhook_endpoint_handles_getattr_keyerror(monkeypatch) -> None:
    _, module = _build_client()

    class FakeStripeObject:
        def __init__(self, **kwargs):
            self._values = dict(kwargs)

        def __getattr__(self, name):
            if name in self._values:
                return self._values[name]
            raise KeyError(name)

    class FakeWebhookEndpoint:
        @staticmethod
        def create(**kwargs):
            return FakeStripeObject(id="we_live_keyerror", secret="whsec_live_keyerror")

    class FakeStripeSDK:
        api_key = None
        WebhookEndpoint = FakeWebhookEndpoint

    monkeypatch.setitem(sys.modules, "stripe", FakeStripeSDK)

    provider_config, created = module.asyncio.run(
        module._ensure_stripe_webhook_endpoint(
            psp_id="psp_stripe_live_keyerror",
            api_key="sk_live_stripe_secret",
            provider_config={"mode": "payment_intent"},
            account_id=None,
            environment="live",
        )
    )

    assert created is True
    assert provider_config["webhook_endpoint_id"] == "we_live_keyerror"
    assert provider_config["webhook_endpoint_secret"] == "whsec_live_keyerror"
    assert provider_config["webhook_url"].endswith("/webhooks/stripe/psp_stripe_live_keyerror")


def test_ensure_stripe_webhook_endpoint_disables_duplicate_url_endpoints(monkeypatch) -> None:
    _, module = _build_client()

    target_url = module._stripe_webhook_target_url("psp_stripe_dedupe")
    disabled_endpoint_ids = []

    class FakeStripeObject:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeWebhookEndpoint:
        @staticmethod
        def create(**kwargs):
            return FakeStripeObject(
                id="we_new",
                secret="whsec_new",
                url=kwargs["url"],
                status="enabled",
            )

        @staticmethod
        def list(**kwargs):
            return FakeStripeObject(
                data=[
                    FakeStripeObject(id="we_old_enabled", url=target_url, status="enabled"),
                    FakeStripeObject(id="we_new", url=target_url, status="enabled"),
                    FakeStripeObject(id="we_old_disabled", url=target_url, status="disabled"),
                    FakeStripeObject(
                        id="we_other",
                        url="https://api.pivota.cc/webhooks/stripe/other",
                        status="enabled",
                    ),
                ]
            )

        @staticmethod
        def modify(endpoint_id, **kwargs):
            disabled_endpoint_ids.append((endpoint_id, kwargs))
            return FakeStripeObject(id=endpoint_id, disabled=kwargs.get("disabled"))

    class FakeStripeSDK:
        api_key = None
        WebhookEndpoint = FakeWebhookEndpoint

    monkeypatch.setitem(sys.modules, "stripe", FakeStripeSDK)

    provider_config, created = module.asyncio.run(
        module._ensure_stripe_webhook_endpoint(
            psp_id="psp_stripe_dedupe",
            api_key="sk_live_stripe_secret",
            provider_config={"mode": "payment_intent"},
            account_id=None,
            environment="live",
        )
    )

    assert created is True
    assert provider_config["webhook_endpoint_id"] == "we_new"
    assert disabled_endpoint_ids == [("we_old_enabled", {"disabled": True})]


def test_merchant_order_backed_canary_route_uses_authenticated_merchant(monkeypatch) -> None:
    client, module = _build_client()

    captured = {}

    async def fake_execute(*, merchant, payment_request, source):
        captured["merchant"] = merchant
        captured["payment_request"] = payment_request
        captured["source"] = source
        return {
            "success": True,
            "payment_id": "cs_test_hidden_canary",
            "order_id": "ORD_HIDDEN_CANARY",
            "amount": 100,
            "currency": "USD",
            "psp_used": "stripe",
            "status": "requires_action",
            "transaction_id": "cs_test_hidden_canary",
            "requires_customer_action": True,
            "payment_action": {
                "type": "redirect_url",
                "url": "https://checkout.stripe.test/session",
                "raw": {},
            },
            "error_message": None,
            "timestamp": "2026-03-24T00:00:00Z",
        }

    import routes.payment_execution_routes as payment_execution_module

    monkeypatch.setattr(
        payment_execution_module,
        "_execute_order_backed_payment_canary",
        fake_execute,
    )

    # The route now LOADS the merchant instead of fabricating one with
    # status="approved" hardcoded — that fabrication skipped this very loader's
    # "Only approved merchants can process payments" check while creating a real
    # order and running a real PSP payment. Stubbed rather than removed, so the
    # assertions below still prove the route uses the AUTHENTICATED merchant_id.
    loaded_for = {}

    async def fake_load(merchant_id):
        loaded_for["merchant_id"] = merchant_id
        return {
            "merchant_id": merchant_id,
            "business_name": "Test Integrations",
            "contact_email": "merchant@example.com",
            "status": "approved",
        }

    monkeypatch.setattr(payment_execution_module, "_load_canary_merchant", fake_load)

    response = client.post(
        "/merchant/payment-canary/order-backed",
        json={
            "amount": 100,
            "currency": "USD",
            "customer_email": "merchant@example.com",
            "enforce_live_readiness": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == "ORD_HIDDEN_CANARY"
    assert captured["merchant"]["merchant_id"] == "merch_test_integrations"
    assert captured["merchant"]["contact_email"] == "merchant@example.com"
    # …and it looked that merchant up rather than inventing it.
    assert loaded_for["merchant_id"] == "merch_test_integrations"
    assert captured["payment_request"].order_id.startswith("merchant_canary_")
    assert captured["payment_request"].enforce_live_readiness is True
    assert captured["payment_request"].preferred_provider is None
    assert captured["source"] == "merchant_order_backed_canary"


def test_merchant_order_backed_canary_route_passes_preferred_provider(monkeypatch) -> None:
    client, module = _build_client()

    captured = {}

    async def fake_execute(*, merchant, payment_request, source):
        captured["merchant"] = merchant
        captured["payment_request"] = payment_request
        captured["source"] = source
        return {
            "success": True,
            "payment_id": "adyen_session_test",
            "order_id": "ORD_HIDDEN_CANARY_ADYEN",
            "amount": 100,
            "currency": "USD",
            "psp_used": "adyen",
            "status": "requires_action",
            "transaction_id": "adyen_session_test",
            "requires_customer_action": True,
            "payment_action": {
                "type": "adyen_session",
                "client_secret": "session-data",
                "session_data": "session-data",
                "client_key": "test_client_key",
                "raw": {"id": "SESSION_TEST", "environment": "test"},
            },
            "error_message": None,
            "timestamp": "2026-03-24T00:00:00Z",
        }

    import routes.payment_execution_routes as payment_execution_module

    monkeypatch.setattr(
        payment_execution_module,
        "_execute_order_backed_payment_canary",
        fake_execute,
    )

    # The route now LOADS the merchant instead of fabricating one with
    # status="approved" hardcoded — that fabrication skipped this very loader's
    # "Only approved merchants can process payments" check while creating a real
    # order and running a real PSP payment. Stubbed rather than removed, so the
    # assertions below still prove the route uses the AUTHENTICATED merchant_id.
    loaded_for = {}

    async def fake_load(merchant_id):
        loaded_for["merchant_id"] = merchant_id
        return {
            "merchant_id": merchant_id,
            "business_name": "Test Integrations",
            "contact_email": "merchant@example.com",
            "status": "approved",
        }

    monkeypatch.setattr(payment_execution_module, "_load_canary_merchant", fake_load)

    response = client.post(
        "/merchant/payment-canary/order-backed",
        json={
            "amount": 100,
            "currency": "USD",
            "customer_email": "merchant@example.com",
            "enforce_live_readiness": False,
            "preferred_provider": "adyen",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == "ORD_HIDDEN_CANARY_ADYEN"
    assert captured["payment_request"].preferred_provider == "adyen"
    assert captured["payment_request"].enforce_live_readiness is False
    assert captured["source"] == "merchant_order_backed_canary"
