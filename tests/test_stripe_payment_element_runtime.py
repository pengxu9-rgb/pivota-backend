from decimal import Decimal
from types import SimpleNamespace

import pytest


class _FakeStripePaymentIntent:
    def __init__(self):
        self.id = "pi_test_123"
        self.client_secret = "pi_test_123_secret_456"
        self.amount = 100
        self.currency = "usd"
        self.status = "requires_action"


class _FakePaymentIntentsAPI:
    def __init__(self, recorder):
        self._recorder = recorder

    def create(self, payload, request_options):
        self._recorder.append(
            {
                "payload": payload,
                "request_options": request_options,
            }
        )
        return _FakeStripePaymentIntent()


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
