from decimal import Decimal

import pytest


class _FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data or {"id": "SESSION_TEST", "sessionData": "session_data_test"}
        self.text = text

    def json(self):
        return self._data


class _RecordingAsyncClient:
    def __init__(self, recorder):
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json, headers, timeout):
        self._recorder.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return _FakeResponse()


@pytest.mark.asyncio
async def test_adyen_adapter_forces_scheme_only_for_ops_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import psp_adapter as module

    requests = []

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda: _RecordingAsyncClient(requests),
    )

    adapter = module.AdyenAdapter(
        api_key="AQE_test_key_value_that_is_long_enough_to_avoid_fallback_1234567890",
        merchant_account="WoopayECOM",
        environment="test",
        client_key="test_client_key",
    )

    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("1.00"),
        currency="USD",
        metadata={
            "order_id": "ORD_CANARY_1",
            "ops_canary": True,
            "source": "ops_order_backed_canary",
        },
    )

    assert error is None
    assert success is True
    assert intent is not None
    assert requests[0]["json"]["allowedPaymentMethods"] == ["scheme"]


@pytest.mark.asyncio
async def test_adyen_adapter_does_not_force_scheme_for_general_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import psp_adapter as module

    requests = []

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda: _RecordingAsyncClient(requests),
    )

    adapter = module.AdyenAdapter(
        api_key="AQE_test_key_value_that_is_long_enough_to_avoid_fallback_1234567890",
        merchant_account="WoopayECOM",
        environment="test",
        client_key="test_client_key",
    )

    success, intent, error = await adapter.create_payment_intent(
        amount=Decimal("1.00"),
        currency="USD",
        metadata={
            "order_id": "ORD_GENERAL_1",
            "source": "merchant_payment_execute",
        },
    )

    assert error is None
    assert success is True
    assert intent is not None
    assert "allowedPaymentMethods" not in requests[0]["json"]
