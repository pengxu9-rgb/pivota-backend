from decimal import Decimal
from types import SimpleNamespace

import pytest


class _FakeStripePaymentIntent:
    def __init__(self, status: str = "requires_action"):
        self.id = "pi_test_123"
        self.client_secret = "pi_test_123_secret_456"
        self.amount = 100
        self.currency = "usd"
        self.status = status


class _FakePaymentIntentsAPI:
    def __init__(self, recorder):
        self._recorder = recorder

    def create(self, payload, request_options):
        self._recorder.append(
            {
                "method": "create",
                "payload": payload,
                "request_options": request_options,
            }
        )
        return _FakeStripePaymentIntent()

    def capture(self, payment_intent_id, payload, request_options):
        self._recorder.append(
            {
                "method": "capture",
                "payment_intent_id": payment_intent_id,
                "payload": payload,
                "request_options": request_options,
            }
        )
        return _FakeStripePaymentIntent(status="succeeded")

    def cancel(self, payment_intent_id, payload, request_options):
        self._recorder.append(
            {
                "method": "cancel",
                "payment_intent_id": payment_intent_id,
                "payload": payload,
                "request_options": request_options,
            }
        )
        return _FakeStripePaymentIntent(status="canceled")


class _FakeCheckoutSession:
    id = "cs_test_123"
    url = "https://checkout.stripe.test/cs_test_123"


class _FakeCheckoutSessionsAPI:
    def __init__(self, recorder):
        self._recorder = recorder

    def create(self, payload, request_options):
        self._recorder.append(
            {
                "method": "checkout.sessions.create",
                "payload": payload,
                "request_options": request_options,
            }
        )
        return _FakeCheckoutSession()


@pytest.mark.asyncio
async def test_stripe_adapter_defaults_to_non_redirect_payment_intents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import psp_adapter as module

    requests = []

    class _FakeStripeClient:
        def __init__(self, *args, **kwargs):
            self.v1 = SimpleNamespace(
                payment_intents=_FakePaymentIntentsAPI(requests),
            )

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(module.stripe, "StripeClient", _FakeStripeClient)
    monkeypatch.setattr(module.stripe, "RequestsClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(module.asyncio, "to_thread", _run_inline)

    adapter = module.StripeAdapter(
        api_key="sk_live_test_123",
        public_key="pk_live_test_123",
    )

    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("1.00"),
        currency="USD",
        metadata={
            "order_id": "ORD_TEST_1",
        },
    )

    assert success is True
    assert error is None
    assert intent is not None
    assert (
        requests[0]["payload"]["automatic_payment_methods"]["allow_redirects"] == "never"
    )


@pytest.mark.asyncio
async def test_stripe_adapter_enables_redirect_methods_when_return_url_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import psp_adapter as module

    requests = []

    class _FakeStripeClient:
        def __init__(self, *args, **kwargs):
            self.v1 = SimpleNamespace(
                payment_intents=_FakePaymentIntentsAPI(requests),
            )

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(module.stripe, "StripeClient", _FakeStripeClient)
    monkeypatch.setattr(module.stripe, "RequestsClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(module.asyncio, "to_thread", _run_inline)

    adapter = module.StripeAdapter(
        api_key="sk_live_test_123",
        public_key="pk_live_test_123",
    )

    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("1.00"),
        currency="USD",
        metadata={
            "order_id": "ORD_TEST_2",
            "return_url": "https://agent.pivota.cc/order/success?orderId=ORD_TEST_2",
        },
    )

    assert success is True
    assert error is None
    assert intent is not None
    assert (
        requests[0]["payload"]["automatic_payment_methods"]["allow_redirects"] == "always"
    )


@pytest.mark.asyncio
async def test_stripe_adapter_supports_manual_capture_payment_intents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import psp_adapter as module

    requests = []

    class _FakeStripeClient:
        def __init__(self, *args, **kwargs):
            self.v1 = SimpleNamespace(
                payment_intents=_FakePaymentIntentsAPI(requests),
            )

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(module.stripe, "StripeClient", _FakeStripeClient)
    monkeypatch.setattr(module.stripe, "RequestsClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(module.asyncio, "to_thread", _run_inline)

    adapter = module.StripeAdapter(api_key="sk_live_test_123")

    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("1.00"),
        currency="USD",
        metadata={
            "order_id": "ORD_AUTH_1",
            "capture_method": "manual",
        },
    )

    assert success is True
    assert error is None
    assert intent is not None
    assert requests[0]["method"] == "create"
    assert requests[0]["payload"]["capture_method"] == "manual"
    assert intent.raw_response["capture_method"] == "manual"


@pytest.mark.asyncio
async def test_stripe_payment_intent_create_uses_per_order_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import psp_adapter as module

    requests = []

    class _FakeStripeClient:
        def __init__(self, *args, **kwargs):
            self.v1 = SimpleNamespace(
                payment_intents=_FakePaymentIntentsAPI(requests),
            )

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(module.stripe, "StripeClient", _FakeStripeClient)
    monkeypatch.setattr(module.stripe, "RequestsClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(module.asyncio, "to_thread", _run_inline)

    adapter = module.StripeAdapter(api_key="sk_live_test_123")

    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("1.00"),
        currency="USD",
        metadata={
            "order_id": "ORD_IDEMPOTENT_CREATE",
            "idempotency_key": "caller_key_should_not_scope_provider_create",
        },
    )

    assert success is True
    assert error is None
    assert intent is not None
    assert requests[0]["method"] == "create"
    assert requests[0]["request_options"] == {
        "idempotency_key": "agent_payment:payment_intent:ORD_IDEMPOTENT_CREATE",
    }


@pytest.mark.asyncio
async def test_stripe_checkout_session_supports_manual_capture_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import psp_adapter as module

    requests = []

    class _FakeStripeClient:
        def __init__(self, *args, **kwargs):
            self.v1 = SimpleNamespace(
                checkout=SimpleNamespace(sessions=_FakeCheckoutSessionsAPI(requests)),
                payment_intents=_FakePaymentIntentsAPI(requests),
            )

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(module.stripe, "StripeClient", _FakeStripeClient)
    monkeypatch.setattr(module.stripe, "RequestsClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(module.asyncio, "to_thread", _run_inline)

    adapter = module.StripeAdapter(api_key="sk_live_test_123")

    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("25.00"),
        currency="USD",
        metadata={
            "order_id": "ORD_AUTH_CHECKOUT",
            "psp_mode": "stripe_checkout",
            "payment_flow": "authorization_first",
            "capture_method": "manual",
            "return_url": "https://agent.pivota.cc/checkout/return?order_id=ORD_AUTH_CHECKOUT",
        },
    )

    assert success is True
    assert error is None
    assert intent is not None
    assert intent.id == "cs_test_123"
    assert intent.redirect_url == "https://checkout.stripe.test/cs_test_123"
    assert requests[0]["method"] == "checkout.sessions.create"
    assert (
        requests[0]["payload"]["success_url"]
        == "https://agent.pivota.cc/checkout/return?order_id=ORD_AUTH_CHECKOUT&session_id={CHECKOUT_SESSION_ID}"
    )
    assert requests[0]["payload"]["payment_intent_data"]["capture_method"] == "manual"
    assert requests[0]["payload"]["payment_intent_data"]["metadata"]["order_id"] == "ORD_AUTH_CHECKOUT"


@pytest.mark.asyncio
async def test_stripe_checkout_session_create_uses_per_order_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import psp_adapter as module

    requests = []

    class _FakeStripeClient:
        def __init__(self, *args, **kwargs):
            self.v1 = SimpleNamespace(
                checkout=SimpleNamespace(sessions=_FakeCheckoutSessionsAPI(requests)),
                payment_intents=_FakePaymentIntentsAPI(requests),
            )

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(module.stripe, "StripeClient", _FakeStripeClient)
    monkeypatch.setattr(module.stripe, "RequestsClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(module.asyncio, "to_thread", _run_inline)

    adapter = module.StripeAdapter(api_key="sk_live_test_123")

    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("25.00"),
        currency="USD",
        metadata={
            "order_id": "ORD_IDEMPOTENT_CHECKOUT",
            "idempotency_key": "caller_key_should_not_scope_provider_create",
            "psp_mode": "stripe_checkout",
        },
    )

    assert success is True
    assert error is None
    assert intent is not None
    assert requests[0]["method"] == "checkout.sessions.create"
    assert requests[0]["request_options"] == {
        "idempotency_key": "agent_payment:checkout_session:ORD_IDEMPOTENT_CHECKOUT",
    }


@pytest.mark.asyncio
async def test_stripe_create_idempotency_keys_are_endpoint_scoped_for_same_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import psp_adapter as module

    requests = []

    class _FakeStripeClient:
        def __init__(self, *args, **kwargs):
            self.v1 = SimpleNamespace(
                checkout=SimpleNamespace(sessions=_FakeCheckoutSessionsAPI(requests)),
                payment_intents=_FakePaymentIntentsAPI(requests),
            )

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(module.stripe, "StripeClient", _FakeStripeClient)
    monkeypatch.setattr(module.stripe, "RequestsClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(module.asyncio, "to_thread", _run_inline)

    adapter = module.StripeAdapter(api_key="sk_live_test_123")

    first_ok, first_intent, first_error = await adapter.create_payment_intent(
        amount=Decimal("1.69"),
        currency="USD",
        metadata={"order_id": "ORD_SAME_ORDER"},
    )
    second_ok, second_intent, second_error = await adapter.create_payment_intent(
        amount=Decimal("1.69"),
        currency="USD",
        metadata={"order_id": "ORD_SAME_ORDER", "psp_mode": "stripe_checkout"},
    )

    assert first_ok is True
    assert first_error is None
    assert first_intent is not None
    assert second_ok is True
    assert second_error is None
    assert second_intent is not None
    assert requests[0]["method"] == "create"
    assert requests[0]["request_options"] == {
        "idempotency_key": "agent_payment:payment_intent:ORD_SAME_ORDER",
    }
    assert requests[1]["method"] == "checkout.sessions.create"
    assert requests[1]["request_options"] == {
        "idempotency_key": "agent_payment:checkout_session:ORD_SAME_ORDER",
    }


@pytest.mark.asyncio
async def test_stripe_adapter_capture_and_cancel_authorization_use_idempotency_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import psp_adapter as module

    requests = []

    class _FakeStripeClient:
        def __init__(self, *args, **kwargs):
            self.v1 = SimpleNamespace(
                payment_intents=_FakePaymentIntentsAPI(requests),
            )

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(module.stripe, "StripeClient", _FakeStripeClient)
    monkeypatch.setattr(module.stripe, "RequestsClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(module.asyncio, "to_thread", _run_inline)

    adapter = module.StripeAdapter(api_key="sk_live_test_123")

    capture_ok, capture_id, capture_error = await adapter.capture_payment(
        "pi_auth_123",
        amount=Decimal("1.00"),
        idempotency_key="capture:ord_1",
    )
    cancel_ok, cancel_id, cancel_error = await adapter.cancel_payment_authorization(
        "pi_auth_456",
        reason="abandoned",
        idempotency_key="cancel:ord_2",
    )

    assert capture_ok is True
    assert capture_id == "pi_test_123"
    assert capture_error is None
    assert requests[0]["method"] == "capture"
    assert requests[0]["payment_intent_id"] == "pi_auth_123"
    assert requests[0]["payload"]["amount_to_capture"] == 100
    assert requests[0]["request_options"]["idempotency_key"] == "capture:ord_1"

    assert cancel_ok is True
    assert cancel_id == "pi_test_123"
    assert cancel_error is None
    assert requests[1]["method"] == "cancel"
    assert requests[1]["payment_intent_id"] == "pi_auth_456"
    assert requests[1]["payload"]["cancellation_reason"] == "abandoned"
    assert requests[1]["request_options"]["idempotency_key"] == "cancel:ord_2"
