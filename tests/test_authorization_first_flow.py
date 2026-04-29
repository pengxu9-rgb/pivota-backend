from __future__ import annotations

from typing import Any, Dict

import pytest


def _auth_first_order(**overrides: Any) -> Dict[str, Any]:
    order = {
        "order_id": "ord_auth_first",
        "merchant_id": "merch_1",
        "payment_status": "awaiting_payment",
        "status": "pending",
        "payment_intent_id": "pi_auth_first",
        "psp_used": "stripe",
        "total": "25.00",
        "currency": "USD",
        "shopify_order_id": None,
        "metadata": {
            "payment_flow": {
                "mode": "authorization_first",
                "psp": "stripe",
                "store_platform": "shopify",
                "capture_method": "manual",
            }
        },
    }
    order.update(overrides)
    return order


class _FakeAuthFirstStripeAdapter:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self.calls = calls

    async def get_payment_status_details(self, payment_intent_id: str):
        self.calls.append(("status_details", payment_intent_id))
        return True, {"status": "requires_capture", "amount": "25.00", "currency": "USD"}, None

    async def capture_payment(self, payment_intent_id: str, amount=None, idempotency_key=None):
        self.calls.append(("capture", payment_intent_id, idempotency_key))
        return True, "pi_auth_first", None

    async def cancel_payment_authorization(self, payment_intent_id: str, reason=None, idempotency_key=None):
        self.calls.append(("cancel", payment_intent_id, idempotency_key))
        return True, "pi_auth_first", None


@pytest.mark.asyncio
async def test_authorization_first_creates_merchant_order_before_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    calls: list[tuple[str, Any]] = []
    linked = {"value": False}
    updates: list[Dict[str, Any]] = []

    async def fake_resolve_adapter(order: Dict[str, Any]):
        return "stripe", _FakeAuthFirstStripeAdapter(calls)

    async def fake_get_order(order_id: str):
        order = _auth_first_order(order_id=order_id)
        if linked["value"]:
            order["shopify_order_id"] = "shopify_123"
            order["metadata"]["merchant_order"] = {
                "platform": "shopify",
                "platform_order_id": "shopify_123",
            }
        return order

    async def fake_sync(order_id: str):
        calls.append(("merchant_order", order_id))
        linked["value"] = True
        return True

    async def fake_update_order_row(order_id: str, fields: Dict[str, Any]):
        updates.append({"order_id": order_id, "fields": fields})
        return True

    async def fake_mark_paid(order_id: str):
        calls.append(("mark_paid", order_id))
        return True

    async def fake_log_order_event(**kwargs: Any):
        calls.append(("event", kwargs["event_type"]))

    async def fake_recovery_update(**kwargs: Any):
        return kwargs["fields"]

    monkeypatch.setattr(module, "_resolve_order_psp_adapter", fake_resolve_adapter)
    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "sync_order_to_connected_store", fake_sync)
    monkeypatch.setattr(module, "update_order_row", fake_update_order_row)
    monkeypatch.setattr(module, "mark_order_paid", fake_mark_paid)
    monkeypatch.setattr(module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(module, "_update_payment_recovery_metadata_best_effort", fake_recovery_update)

    result = await module.finalize_authorized_payment_order(
        "ord_auth_first",
        order=_auth_first_order(),
        source_event="unit_test",
    )

    assert result["status"] == "success"
    assert result["captured"] is True
    assert result["linked_merchant_order"]["platform_order_id"] == "shopify_123"
    assert calls.index(("merchant_order", "ord_auth_first")) < calls.index(
        ("capture", "pi_auth_first", "auth_first_capture:ord_auth_first")
    )
    assert ("mark_paid", "ord_auth_first") in calls
    assert updates[0]["fields"]["payment_status"] == "authorized"


@pytest.mark.asyncio
async def test_authorization_first_voids_authorization_when_merchant_order_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    calls: list[tuple[str, Any]] = []
    recovery_updates: list[Dict[str, Any]] = []
    row_updates: list[Dict[str, Any]] = []

    async def fake_resolve_adapter(order: Dict[str, Any]):
        return "stripe", _FakeAuthFirstStripeAdapter(calls)

    async def fake_get_order(order_id: str):
        return _auth_first_order(order_id=order_id)

    async def fake_sync(order_id: str):
        calls.append(("merchant_order", order_id))
        return False

    async def fake_update_order_row(order_id: str, fields: Dict[str, Any]):
        row_updates.append(fields)
        return True

    async def fake_recovery_update(**kwargs: Any):
        recovery_updates.append(kwargs["fields"])
        return kwargs["fields"]

    async def fake_log_order_event(**kwargs: Any):
        calls.append(("event", kwargs["event_type"]))

    monkeypatch.setattr(module, "_resolve_order_psp_adapter", fake_resolve_adapter)
    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "sync_order_to_connected_store", fake_sync)
    monkeypatch.setattr(module, "update_order_row", fake_update_order_row)
    monkeypatch.setattr(module, "_update_payment_recovery_metadata_best_effort", fake_recovery_update)
    monkeypatch.setattr(module, "log_order_event", fake_log_order_event)

    result = await module.finalize_authorized_payment_order(
        "ord_auth_first",
        order=_auth_first_order(),
        source_event="unit_test",
    )

    assert result["status"] == "merchant_order_failed_authorization_voided"
    assert result["voided"] is True
    assert ("cancel", "pi_auth_first", "auth_first_void:ord_auth_first") in calls
    assert not any(call[0] == "capture" for call in calls)
    assert recovery_updates[-1]["refund_required"] is False
    assert recovery_updates[-1]["auto_void_succeeded"] is True
    assert row_updates[-1]["payment_status"] == "authorization_voided"


def test_authorized_merchant_order_write_is_shopify_only() -> None:
    import routes.order_routes as module

    order = _auth_first_order(payment_status="authorized")

    assert module._order_payment_allows_merchant_order_write(order, platform="shopify") is True
    assert module._order_payment_allows_merchant_order_write(order, platform="woocommerce") is False
    assert module._order_payment_allows_merchant_order_write(order, platform="bigcommerce") is False


def test_authorization_first_order_usage_accepts_paypal_shopify_metadata() -> None:
    import routes.order_routes as module

    order = _auth_first_order(
        psp_used="paypal",
        payment_intent_id="PAYPAL_ORDER_AUTH",
        metadata={
            "payment_flow": {
                "mode": "authorization_first",
                "psp": "paypal",
                "store_platform": "shopify",
                "capture_method": "manual",
            }
        },
    )

    assert module.order_uses_authorization_first_payment(order) is True
    assert module._order_payment_allows_merchant_order_write(order, platform="shopify") is False
    order["payment_status"] = "authorized"
    assert module._order_payment_allows_merchant_order_write(order, platform="shopify") is True


def test_paypal_authorization_first_order_flow_is_feature_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as module
    from config import feature_flags

    monkeypatch.setitem(feature_flags.FEATURE_FLAGS, "enable_authorization_first_orders", True)
    monkeypatch.setitem(feature_flags.FEATURE_FLAGS, "enable_paypal_authorization_first", False)

    assert (
        module._should_use_authorization_first_order_flow(
            merchant_id="merch_1",
            psp_type="paypal",
            psp_mode=None,
            store_info={"platform": "shopify"},
        )
        is False
    )


def test_stripe_checkout_authorization_first_order_flow_is_feature_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as module
    from config import feature_flags

    monkeypatch.setitem(feature_flags.FEATURE_FLAGS, "enable_authorization_first_orders", True)
    monkeypatch.setitem(feature_flags.FEATURE_FLAGS, "enable_stripe_manual_capture", False)

    assert (
        module._should_use_authorization_first_order_flow(
            merchant_id="merch_1",
            psp_type="stripe",
            psp_mode="stripe_checkout",
            store_info={"platform": "shopify"},
        )
        is False
    )

    monkeypatch.setitem(feature_flags.FEATURE_FLAGS, "enable_stripe_manual_capture", True)

    assert (
        module._should_use_authorization_first_order_flow(
            merchant_id="merch_1",
            psp_type="stripe",
            psp_mode="stripe_checkout",
            store_info={"platform": "shopify"},
        )
        is True
    )

    monkeypatch.setitem(feature_flags.FEATURE_FLAGS, "enable_paypal_authorization_first", True)

    assert (
        module._should_use_authorization_first_order_flow(
            merchant_id="merch_1",
            psp_type="paypal",
            psp_mode=None,
            store_info={"platform": "shopify"},
        )
        is True
    )
    assert (
        module._should_use_authorization_first_order_flow(
            merchant_id="merch_1",
            psp_type="paypal",
            psp_mode=None,
            store_info={"platform": "woocommerce"},
        )
        is False
    )
