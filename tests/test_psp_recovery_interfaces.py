from __future__ import annotations

import inspect
from datetime import datetime
from decimal import Decimal

import pytest


def test_non_stripe_refund_adapters_accept_recovery_idempotency() -> None:
    from adapters.checkout_adapter import CheckoutAdapter
    from adapters.paypal_adapter import PayPalAdapter
    from adapters.psp_adapter import AdyenAdapter

    for adapter_cls in (AdyenAdapter, CheckoutAdapter, PayPalAdapter):
        params = inspect.signature(adapter_cls.refund_payment).parameters
        assert "idempotency_key" in params
        assert "amount" in params
        assert "reason" in params


def test_psp_adapter_exposes_optional_authorization_methods() -> None:
    from adapters.psp_adapter import AdyenAdapter, PSPAdapter, StripeAdapter
    from adapters.checkout_adapter import CheckoutAdapter
    from adapters.paypal_adapter import PayPalAdapter

    assert hasattr(PSPAdapter, "capture_payment")
    assert hasattr(PSPAdapter, "cancel_payment_authorization")
    assert hasattr(StripeAdapter, "capture_payment")
    assert hasattr(StripeAdapter, "cancel_payment_authorization")
    assert hasattr(PayPalAdapter, "capture_payment")
    assert hasattr(PayPalAdapter, "cancel_payment_authorization")
    assert hasattr(AdyenAdapter, "capture_payment")
    assert hasattr(AdyenAdapter, "cancel_payment_authorization")
    assert hasattr(CheckoutAdapter, "capture_payment")
    assert hasattr(CheckoutAdapter, "cancel_payment_authorization")


def test_paypal_adapter_matches_common_confirm_signature() -> None:
    from adapters.paypal_adapter import PayPalAdapter

    confirm_params = inspect.signature(PayPalAdapter.confirm_payment).parameters
    assert "payment_intent_id" in confirm_params
    assert "payment_method_id" in confirm_params


@pytest.mark.asyncio
async def test_paypal_access_token_cache_accepts_timestamp_expiry() -> None:
    from adapters.paypal_adapter import PayPalAdapter

    adapter = PayPalAdapter("paypal_client", "paypal_secret")
    adapter.access_token = "cached_token"
    adapter.token_expiry = datetime.now().timestamp() + 60

    assert await adapter._get_access_token() == "cached_token"


@pytest.mark.asyncio
async def test_paypal_payment_create_uses_major_unit_amounts(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import paypal_adapter as module
    from adapters.paypal_adapter import PayPalAdapter

    captured = {}

    class _FakeResponse:
        status_code = 201

        def json(self):
            return {
                "id": "PAYPAL_ORDER_1",
                "links": [{"rel": "approve", "href": "https://paypal.test/approve"}],
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return _FakeResponse()

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())

    adapter = PayPalAdapter("paypal_client", "paypal_secret")

    async def fake_access_token() -> str:
        return "paypal_token"

    adapter._get_access_token = fake_access_token  # type: ignore[method-assign]

    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("1.09"),
        currency="USD",
        metadata={"order_id": "order_1", "description": "Recovery test"},
    )

    assert success is True
    assert error is None
    assert intent is not None
    assert intent.amount == 109
    assert captured["payload"]["purchase_units"][0]["amount"] == {
        "currency_code": "USD",
        "value": "1.09",
    }


@pytest.mark.asyncio
async def test_paypal_payment_create_supports_authorize_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import paypal_adapter as module
    from adapters.paypal_adapter import PayPalAdapter

    captured = {}

    class _FakeResponse:
        status_code = 201
        text = ""

        def json(self):
            return {
                "id": "PAYPAL_ORDER_AUTH",
                "links": [{"rel": "approve", "href": "https://paypal.test/approve"}],
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return _FakeResponse()

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())

    adapter = PayPalAdapter("paypal_client", "paypal_secret")

    async def fake_access_token() -> str:
        return "paypal_token"

    adapter._get_access_token = fake_access_token  # type: ignore[method-assign]

    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("25.00"),
        currency="USD",
        metadata={
            "order_id": "order_auth",
            "payment_flow": "authorization_first",
            "capture_method": "manual",
        },
    )

    assert success is True
    assert error is None
    assert intent is not None
    assert captured["payload"]["intent"] == "AUTHORIZE"
    assert captured["headers"]["PayPal-Request-Id"] == "order_auth"


@pytest.mark.asyncio
async def test_paypal_confirm_authorizes_authorize_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import paypal_adapter as module
    from adapters.paypal_adapter import PayPalAdapter

    calls = []

    class _FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = "fake"

        def json(self):
            return self._payload

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, **kwargs):
            calls.append(("get", url, headers))
            return _FakeResponse(
                200,
                {
                    "id": "PAYPAL_ORDER_AUTH",
                    "intent": "AUTHORIZE",
                    "status": "APPROVED",
                    "purchase_units": [
                        {"amount": {"currency_code": "USD", "value": "25.00"}}
                    ],
                },
            )

        async def post(self, url, headers=None, json=None, **kwargs):
            calls.append(("post", url, headers, json))
            return _FakeResponse(
                201,
                {
                    "id": "PAYPAL_ORDER_AUTH",
                    "purchase_units": [
                        {
                            "payments": {
                                "authorizations": [
                                    {
                                        "id": "AUTH_1",
                                        "status": "CREATED",
                                        "amount": {"currency_code": "USD", "value": "25.00"},
                                    }
                                ]
                            }
                        }
                    ],
                },
            )

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())

    adapter = PayPalAdapter("paypal_client", "paypal_secret")

    async def fake_access_token() -> str:
        return "paypal_token"

    adapter._get_access_token = fake_access_token  # type: ignore[method-assign]

    success, status, error = await adapter.confirm_payment("PAYPAL_ORDER_AUTH")

    assert success is True
    assert status == "requires_capture"
    assert error is None
    post_call = [call for call in calls if call[0] == "post"][0]
    assert post_call[1].endswith("/v2/checkout/orders/PAYPAL_ORDER_AUTH/authorize")
    assert post_call[2]["PayPal-Request-Id"] == "paypal_authorize:PAYPAL_ORDER_AUTH"


@pytest.mark.asyncio
async def test_paypal_authorization_status_capture_and_void(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import paypal_adapter as module
    from adapters.paypal_adapter import PayPalAdapter

    calls = []
    order_payload = {
        "id": "PAYPAL_ORDER_AUTH",
        "intent": "AUTHORIZE",
        "status": "COMPLETED",
        "purchase_units": [
            {
                "payments": {
                    "authorizations": [
                        {
                            "id": "AUTH_1",
                            "status": "CREATED",
                            "amount": {"currency_code": "USD", "value": "25.00"},
                        }
                    ]
                }
            }
        ],
    }

    class _FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = "fake"

        def json(self):
            return self._payload

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, **kwargs):
            calls.append(("get", url, headers))
            return _FakeResponse(200, order_payload)

        async def post(self, url, headers=None, json=None, **kwargs):
            calls.append(("post", url, headers, json))
            if url.endswith("/capture"):
                return _FakeResponse(201, {"id": "CAPTURE_1"})
            return _FakeResponse(204, {})

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())

    adapter = PayPalAdapter("paypal_client", "paypal_secret")

    async def fake_access_token() -> str:
        return "paypal_token"

    adapter._get_access_token = fake_access_token  # type: ignore[method-assign]

    ok, details, error = await adapter.get_payment_status_details("PAYPAL_ORDER_AUTH")
    assert ok is True
    assert error is None
    assert details["status"] == "requires_capture"
    assert details["amount"] == "25.00"
    assert details["currency"] == "USD"
    assert details["authorization_id"] == "AUTH_1"

    capture_ok, capture_ref, capture_error = await adapter.capture_payment(
        "PAYPAL_ORDER_AUTH",
        idempotency_key="auth_first_capture:order_1",
    )
    assert capture_ok is True
    assert capture_ref == "CAPTURE_1"
    assert capture_error is None

    void_ok, void_ref, void_error = await adapter.cancel_payment_authorization(
        "PAYPAL_ORDER_AUTH",
        idempotency_key="auth_first_void:order_1",
    )
    assert void_ok is True
    assert void_ref == "AUTH_1"
    assert void_error is None

    post_urls = [call[1] for call in calls if call[0] == "post"]
    assert any(url.endswith("/v2/payments/authorizations/AUTH_1/capture") for url in post_urls)
    assert any(url.endswith("/v2/payments/authorizations/AUTH_1/void") for url in post_urls)
    post_headers = [call[2] for call in calls if call[0] == "post"]
    assert post_headers[0]["PayPal-Request-Id"] == "auth_first_capture:order_1"
    assert post_headers[1]["PayPal-Request-Id"] == "auth_first_void:order_1"


@pytest.mark.asyncio
async def test_adyen_session_can_request_delayed_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    import adapters.psp_adapter as module
    from adapters.psp_adapter import AdyenAdapter

    captured = {}

    class _FakeResponse:
        status_code = 201
        text = ""

        def json(self):
            return {"id": "ADYEN_SESSION_1", "sessionData": "adyen_session_data"}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())

    adapter = AdyenAdapter("A" * 80, merchant_account="PivotaTestMerchant")
    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("25.00"),
        currency="USD",
        metadata={
            "order_id": "order_adyen_auth",
            "payment_flow": "authorization_first",
            "capture_method": "manual",
        },
    )

    assert success is True
    assert error is None
    assert intent is not None
    assert captured["payload"]["captureDelayHours"] == 672


@pytest.mark.asyncio
async def test_adyen_capture_and_cancel_primitives_are_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    import adapters.psp_adapter as module
    from adapters.psp_adapter import AdyenAdapter

    calls = []

    class _FakeResponse:
        def __init__(self, payload):
            self.status_code = 201
            self._payload = payload
            self.text = "fake"

        def json(self):
            return self._payload

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers))
            if url.endswith("/captures"):
                return _FakeResponse({"pspReference": "ADYEN_CAPTURE_1"})
            return _FakeResponse({"pspReference": "ADYEN_CANCEL_1"})

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())

    adapter = AdyenAdapter("A" * 80, merchant_account="PivotaTestMerchant")

    capture_ok, capture_ref, capture_error = await adapter.capture_payment(
        "ADYEN_AUTH_1",
        amount=Decimal("25.00"),
        currency="USD",
        idempotency_key="auth_first_capture:order_adyen",
    )
    cancel_ok, cancel_ref, cancel_error = await adapter.cancel_payment_authorization(
        "ADYEN_AUTH_1",
        idempotency_key="auth_first_void:order_adyen",
    )

    assert capture_ok is True
    assert capture_ref == "ADYEN_CAPTURE_1"
    assert capture_error is None
    assert cancel_ok is True
    assert cancel_ref == "ADYEN_CANCEL_1"
    assert cancel_error is None
    assert calls[0][0].endswith("/payments/ADYEN_AUTH_1/captures")
    assert calls[0][1]["amount"] == {"value": 2500, "currency": "USD"}
    assert calls[0][2]["Idempotency-Key"] == "auth_first_capture:order_adyen"
    assert calls[1][0].endswith("/payments/ADYEN_AUTH_1/cancels")
    assert calls[1][2]["Idempotency-Key"] == "auth_first_void:order_adyen"


@pytest.mark.asyncio
async def test_checkout_authorized_status_is_not_treated_as_succeeded(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import checkout_adapter as module
    from adapters.checkout_adapter import CheckoutAdapter

    class _FakeResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {"status": "Authorized"}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None, timeout=None):
            return _FakeResponse()

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())

    adapter = CheckoutAdapter("sk_sbox_test", processing_channel_id="pc_test", environment="test")

    assert await adapter.get_payment_status("pay_auth") == (True, "requires_capture", None)
    assert await adapter.confirm_payment("pay_auth", "unused") == (True, "requires_capture", None)


@pytest.mark.asyncio
async def test_checkout_capture_and_void_primitives_are_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import checkout_adapter as module
    from adapters.checkout_adapter import CheckoutAdapter

    calls = []

    class _FakeResponse:
        status_code = 202
        text = "ok"

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers))
            if url.endswith("/captures"):
                return _FakeResponse({"action_id": "act_capture_1"})
            return _FakeResponse({"action_id": "act_void_1"})

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())

    adapter = CheckoutAdapter("sk_sbox_test", processing_channel_id="pc_test", environment="test")

    capture_ok, capture_ref, capture_error = await adapter.capture_payment(
        "pay_auth",
        amount=Decimal("25.00"),
        currency="USD",
        idempotency_key="auth_first_capture:order_checkout",
    )
    void_ok, void_ref, void_error = await adapter.cancel_payment_authorization(
        "pay_auth",
        idempotency_key="auth_first_void:order_checkout",
    )

    assert capture_ok is True
    assert capture_ref == "act_capture_1"
    assert capture_error is None
    assert void_ok is True
    assert void_ref == "act_void_1"
    assert void_error is None
    assert calls[0][0].endswith("/payments/pay_auth/captures")
    assert calls[0][1]["amount"] == 2500
    assert calls[0][1]["currency"] == "USD"
    assert calls[0][2]["Cko-Idempotency-Key"] == "auth_first_capture:order_checkout"
    assert calls[1][0].endswith("/payments/pay_auth/voids")
    assert calls[1][2]["Cko-Idempotency-Key"] == "auth_first_void:order_checkout"
