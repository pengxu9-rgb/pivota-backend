from decimal import Decimal
from typing import Optional

import pytest


class _FakePaymentIntent:
    def __init__(
        self,
        *,
        payment_id: str,
        psp_type: str,
        client_secret: str,
        public_key: Optional[str] = None,
        stripe_account: Optional[str] = None,
    ):
        self.id = payment_id
        self.psp_type = psp_type
        self.client_secret = client_secret
        self.public_key = public_key
        self.stripe_account = stripe_account
        self.redirect_url = None
        self.status = "requires_action"
        self.raw_response = {
            "clientKey": "test_client_key",
            "environment": "test",
            **({"public_key": public_key} if public_key else {}),
            **({"stripe_account": stripe_account} if stripe_account else {}),
        }


@pytest.mark.asyncio
async def test_initiate_merchant_payment_honors_preferred_psp_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import merchant_payment_initiation_service as module

    attempted_providers = []

    class _FakeAdapter:
        def __init__(self, provider: str):
            self.provider = provider

        async def create_payment_intent(self, *, amount, currency, metadata):
            attempted_providers.append(self.provider)
            return (
                True,
                _FakePaymentIntent(
                    payment_id="adyen_session_test",
                    psp_type=self.provider,
                    client_secret="session-data",
                ),
                None,
            )

    def fake_get_psp_adapter(provider: str, api_key: str, **kwargs):
        return _FakeAdapter(provider)

    monkeypatch.setattr(module, "get_psp_adapter", fake_get_psp_adapter)

    result = await module.initiate_merchant_payment(
        merchant_id="merch_test_payment",
        amount=Decimal("1.00"),
        currency="USD",
        metadata={"source": "test"},
        preferred_psps=["adyen"],
        candidates=[
            {
                "provider": "stripe",
                "api_key": "sk_test_x",
                "environment": "test",
                "provider_config": {"mode": "payment_intent"},
            },
            {
                "provider": "adyen",
                "api_key": "AQE_test_x",
                "environment": "test",
                "provider_config": {
                    "merchant_account": "WoopayECOM",
                    "client_key": "test_client_key",
                },
            },
        ],
    )

    assert attempted_providers == ["adyen"]
    assert result["success"] is True
    assert result["psp_used"] == "adyen"
    assert result["payment_action"]["type"] == "adyen_session"


@pytest.mark.asyncio
async def test_initiate_merchant_payment_uses_preferred_order_for_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from services import merchant_payment_initiation_service as module

    attempted_providers = []

    class _FakeAdapter:
        def __init__(self, provider: str):
            self.provider = provider

        async def create_payment_intent(self, *, amount, currency, metadata):
            attempted_providers.append(self.provider)
            if self.provider == "adyen":
                return False, None, "Adyen test failure"
            return (
                True,
                _FakePaymentIntent(
                    payment_id="cs_checkout_test",
                    psp_type=self.provider,
                    client_secret="checkout-session",
                ),
                None,
            )

    def fake_get_psp_adapter(provider: str, api_key: str, **kwargs):
        return _FakeAdapter(provider)

    monkeypatch.setattr(module, "get_psp_adapter", fake_get_psp_adapter)

    result = await module.initiate_merchant_payment(
        merchant_id="merch_test_payment",
        amount=Decimal("1.00"),
        currency="USD",
        metadata={"source": "test"},
        preferred_psps=["adyen", "checkout"],
        candidates=[
            {
                "provider": "stripe",
                "api_key": "sk_test_x",
                "environment": "test",
                "provider_config": {"mode": "payment_intent"},
            },
            {
                "provider": "checkout",
                "api_key": "sk_sbox_x",
                "environment": "test",
                "provider_config": {
                    "processing_channel_id": "pc_test",
                    "public_key": "pk_sbox_test",
                },
            },
            {
                "provider": "adyen",
                "api_key": "AQE_test_x",
                "environment": "test",
                "provider_config": {
                    "merchant_account": "WoopayECOM",
                    "client_key": "test_client_key",
                },
            },
        ],
    )

    assert attempted_providers == ["adyen", "checkout"]
    assert result["success"] is True
    assert result["psp_used"] == "checkout"
    assert result["payment_action"]["type"] == "checkout_session"


def test_build_payment_action_includes_stripe_public_key() -> None:
    from services.merchant_payment_initiation_service import build_payment_action

    action = build_payment_action(
        _FakePaymentIntent(
            payment_id="pi_live_123",
            psp_type="stripe",
            client_secret="pi_live_123_secret_abc",
            public_key="pk_live_123",
        ),
        psp_used="stripe",
    )

    assert action["type"] == "stripe_client_secret"
    assert action["public_key"] == "pk_live_123"


def test_build_payment_action_accepts_publishable_key_alias() -> None:
    from services.merchant_payment_initiation_service import build_payment_action

    intent = _FakePaymentIntent(
        payment_id="pi_live_alias",
        psp_type="stripe",
        client_secret="pi_live_alias_secret",
    )
    intent.raw_response = {
        "publishable_key": "pk_live_alias_123",
    }

    action = build_payment_action(intent, psp_used="stripe")

    assert action["type"] == "stripe_client_secret"
    assert action["public_key"] == "pk_live_alias_123"


def test_build_payment_action_includes_stripe_account() -> None:
    from services.merchant_payment_initiation_service import build_payment_action

    action = build_payment_action(
        _FakePaymentIntent(
            payment_id="pi_live_connect",
            psp_type="stripe",
            client_secret="pi_live_connect_secret",
            public_key="pk_live_connect",
            stripe_account="acct_live_connect_123",
        ),
        psp_used="stripe",
    )

    assert action["type"] == "stripe_client_secret"
    assert action["stripe_account"] == "acct_live_connect_123"
