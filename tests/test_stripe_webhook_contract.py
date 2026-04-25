from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
os.chdir(REPO_ROOT)

from main import app


def _stripe_event(event_type: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": event_type,
        "data": {
            "object": obj,
        },
    }


@pytest.mark.asyncio
async def test_stripe_webhook_rejects_invalid_signature_when_secret_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        raise Exception("bad signature")

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"type":"payment_intent.succeeded"}',
            headers={"stripe-signature": "bad_sig"},
        )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid signature"


@pytest.mark.asyncio
async def test_stripe_webhook_psp_path_uses_merchant_specific_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    used_secrets: list[str] = []

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        if "FROM merchant_psps" in query:
            assert values["psp_id"] == "psp_stripe_live_123"
            return {
                "provider_config": {
                    "webhook_endpoint_secret": "whsec_merchant_specific",
                }
            }
        if values.get("payment_intent_id") == "pi_psp_path_secret":
            return None
        raise AssertionError(f"Unexpected query: {query}")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        used_secrets.append(secret)
        if secret != "whsec_merchant_specific":
            raise Exception("bad signature")
        return _stripe_event(
            "payment_intent.payment_failed",
            {
                "id": "pi_psp_path_secret",
                "last_payment_error": {"message": "Card declined"},
            },
        )

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_global_fallback", raising=False)
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe/psp_stripe_live_123",
            content=b'{"id":"evt_psp_path_secret"}',
            headers={"stripe-signature": "sig_psp_path_secret"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "payment_intent.payment_failed"}
    assert used_secrets == ["whsec_merchant_specific"]


@pytest.mark.asyncio
async def test_stripe_webhook_accepts_stripe_object_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    class StripeLikeObject:
        def __init__(self, payload: Dict[str, Any]) -> None:
            self._payload = payload

        def __getattr__(self, name: str) -> Any:
            raise KeyError(name)

        def to_dict_recursive(self) -> Dict[str, Any]:
            return self._payload

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        if "FROM merchant_psps" in query:
            assert values["psp_id"] == "psp_stripe_live_object"
            return {
                "provider_config": {
                    "webhook_endpoint_secret": "whsec_merchant_specific",
                }
            }
        raise AssertionError(f"Unexpected query: {query}")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> StripeLikeObject:
        assert secret == "whsec_merchant_specific"
        return StripeLikeObject(
            _stripe_event(
                "checkout.session.completed",
                StripeLikeObject(
                    {
                        "id": "cs_object_payload",
                        "metadata": StripeLikeObject({"order_id": "ORD_OBJECT"}),
                    }
                ),
            )
        )

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "", raising=False)
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe/psp_stripe_live_object",
            content=b'{"id":"evt_object_payload"}',
            headers={"stripe-signature": "sig_object_payload"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "checkout.session.completed"}


@pytest.mark.asyncio
async def test_stripe_webhook_payment_intent_succeeded_marks_paid_and_creates_shopify_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.merchant_onboarding_routes as merchant_onboarding_module
    import routes.order_routes as order_routes_module
    import routes.webhook_routes as webhook_routes_module

    paid_calls: list[str] = []
    evidence_calls: list[tuple[str, str]] = []
    order_events: list[Dict[str, Any]] = []
    shopify_calls: list[str] = []
    merchant_webhook_calls: list[Dict[str, Any]] = []

    event = _stripe_event(
        "payment_intent.succeeded",
        {
            "id": "pi_success_contract",
            "amount": 4520,
            "currency": "usd",
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_success_contract"
        return {
            "order_id": "ORD_STRIPE_SUCCESS",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_success_contract",
        }

    async def fake_mark_order_paid(order_id: str) -> None:
        paid_calls.append(order_id)

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_create_order_snapshot_evidence_pack(order_id: str, triggered_by: str) -> None:
        evidence_calls.append((order_id, triggered_by))

    async def fake_get_primary_store(merchant_id: str) -> Dict[str, Any]:
        assert merchant_id == "m_stripe"
        return {"platform": "shopify"}

    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        assert merchant_id == "m_stripe"
        return {"merchant_id": merchant_id, "status": "active"}

    async def fake_create_shopify_order(order_id: str) -> bool:
        shopify_calls.append(order_id)
        return True

    async def fake_emit_merchant_webhook_event(
        merchant_id: str,
        *,
        event_type: str,
        payload: Dict[str, Any],
        request_id: str | None = None,
        force_delivery: bool = False,
    ) -> Dict[str, Any]:
        merchant_webhook_calls.append(
            {
                "merchant_id": merchant_id,
                "event_type": event_type,
                "payload": dict(payload),
                "request_id": request_id,
                "force_delivery": force_delivery,
            }
        )
        return {"status": "delivered"}

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(
        webhook_routes_module,
        "create_order_snapshot_evidence_pack",
        fake_create_order_snapshot_evidence_pack,
    )
    monkeypatch.setattr(webhook_routes_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(
        merchant_onboarding_module,
        "get_merchant_onboarding",
        fake_get_merchant_onboarding,
    )
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fake_create_shopify_order)
    monkeypatch.setattr(
        webhook_routes_module,
        "emit_merchant_webhook_event",
        fake_emit_merchant_webhook_event,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_success"}',
            headers={"stripe-signature": "sig_success"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "payment_intent.succeeded"}
    assert paid_calls == ["ORD_STRIPE_SUCCESS"]
    assert evidence_calls == [("ORD_STRIPE_SUCCESS", "stripe_webhook")]
    assert shopify_calls == ["ORD_STRIPE_SUCCESS"]
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "payment_confirmed_webhook"
    assert order_events[0]["order_id"] == "ORD_STRIPE_SUCCESS"
    assert order_events[0]["merchant_id"] == "m_stripe"
    assert order_events[0]["metadata"]["payment_intent_id"] == "pi_success_contract"
    assert merchant_webhook_calls == [
        {
            "merchant_id": "m_stripe",
            "event_type": "payment.completed",
            "payload": {
                "order_id": "ORD_STRIPE_SUCCESS",
                "merchant_id": "m_stripe",
                "payment_id": "pi_success_contract",
                "transaction_id": "pi_success_contract",
                "amount": 45.2,
                "currency": "usd",
                "psp_used": "stripe",
                "status": "paid",
                "customer_email": None,
            },
            "request_id": None,
            "force_delivery": False,
        }
    ]


@pytest.mark.asyncio
async def test_stripe_webhook_payment_intent_succeeded_falls_back_to_order_metadata_for_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.merchant_onboarding_routes as merchant_onboarding_module
    import routes.order_routes as order_routes_module
    import routes.webhook_routes as webhook_routes_module

    paid_calls: list[str] = []
    evidence_calls: list[tuple[str, str]] = []
    order_updates: list[tuple[str, Dict[str, Any]]] = []

    event = _stripe_event(
        "payment_intent.succeeded",
        {
            "id": "pi_canary_metadata_success",
            "amount": 100,
            "currency": "usd",
            "metadata": {
                "order_id": "ORD_CANARY_METADATA",
                "ops_canary": "true",
                "skip_platform_order_creation": "true",
            },
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_canary_metadata_success"
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        assert order_id == "ORD_CANARY_METADATA"
        return {
            "order_id": order_id,
            "merchant_id": "m_stripe",
            "payment_intent_id": None,
            "metadata": {
                "ops_canary": True,
                "skip_platform_order_creation": True,
            },
        }

    async def fake_update_order(order_id: str, update_data: Dict[str, Any]) -> bool:
        order_updates.append((order_id, dict(update_data)))
        return True

    async def fake_mark_order_paid(order_id: str) -> None:
        paid_calls.append(order_id)

    async def fake_log_order_event(**kwargs: Any) -> None:
        return None

    async def fake_create_order_snapshot_evidence_pack(order_id: str, triggered_by: str) -> None:
        evidence_calls.append((order_id, triggered_by))

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ops canary webhook must not create Shopify orders")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(webhook_routes_module, "update_order", fake_update_order)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(
        webhook_routes_module,
        "create_order_snapshot_evidence_pack",
        fake_create_order_snapshot_evidence_pack,
    )
    monkeypatch.setattr(webhook_routes_module, "get_primary_store", fail_if_called)
    monkeypatch.setattr(merchant_onboarding_module, "get_merchant_onboarding", fail_if_called)
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_canary_metadata_success"}',
            headers={"stripe-signature": "sig_canary_metadata_success"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "payment_intent.succeeded"}
    assert paid_calls == ["ORD_CANARY_METADATA"]
    assert evidence_calls == [("ORD_CANARY_METADATA", "stripe_webhook")]
    assert order_updates == [
        (
            "ORD_CANARY_METADATA",
            {
                "payment_intent_id": "pi_canary_metadata_success",
                "psp_used": "stripe",
            },
        )
    ]


@pytest.mark.asyncio
async def test_stripe_webhook_payment_intent_succeeded_does_not_restore_paid_after_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    event = _stripe_event(
        "payment_intent.succeeded",
        {
            "id": "pi_success_replay_refunded",
            "amount": 4520,
            "currency": "usd",
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_success_replay_refunded"
        return {
            "order_id": "ORD_STRIPE_REFUNDED",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_success_replay_refunded",
            "status": "partially_refunded",
            "payment_status": "partially_refunded",
            "total_refunded": "12.00",
        }

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("refund-settled Stripe order should not be restored to paid")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "create_order_snapshot_evidence_pack", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "get_primary_store", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_success_replay_refunded"}',
            headers={"stripe-signature": "sig_success_replay_refunded"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "payment_intent.succeeded"}


@pytest.mark.asyncio
async def test_stripe_webhook_payment_failed_marks_order_payment_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    status_updates: list[tuple[str, str]] = []
    order_events: list[Dict[str, Any]] = []

    event = _stripe_event(
        "payment_intent.payment_failed",
        {
            "id": "pi_fail_contract",
            "last_payment_error": {"message": "Card declined"},
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_fail_contract"
        return {
            "order_id": "ORD_STRIPE_FAIL",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_fail_contract",
        }

    async def fake_update_order_status(order_id: str, status: str) -> None:
        status_updates.append((order_id, status))

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_fail"}',
            headers={"stripe-signature": "sig_fail"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "payment_intent.payment_failed"}
    assert status_updates == [("ORD_STRIPE_FAIL", "payment_failed")]
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "payment_failed_webhook"
    assert order_events[0]["metadata"]["payment_intent_id"] == "pi_fail_contract"
    assert order_events[0]["metadata"]["error"] == "Card declined"


@pytest.mark.asyncio
async def test_stripe_webhook_payment_failed_does_not_downgrade_refunded_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    event = _stripe_event(
        "payment_intent.payment_failed",
        {
            "id": "pi_fail_replay_refunded",
            "last_payment_error": {"message": "Late failure replay"},
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_fail_replay_refunded"
        return {
            "order_id": "ORD_STRIPE_REFUNDED_FAIL_REPLAY",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_fail_replay_refunded",
            "status": "partially_refunded",
            "payment_status": "partially_refunded",
            "total_refunded": "12.00",
        }

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("refund-settled Stripe order should not be downgraded to payment_failed")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_fail_replay_refunded"}',
            headers={"stripe-signature": "sig_fail_replay_refunded"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "payment_intent.payment_failed"}


@pytest.mark.asyncio
async def test_stripe_webhook_charge_refunded_reconciles_partial_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_refund_contract",
            "payment_intent": "pi_refund_contract",
            "amount_refunded": 1200,
            "currency": "usd",
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_refund_contract"
        return {
            "order_id": "ORD_STRIPE_REFUND",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_refund_contract",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "metadata": {},
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_refund"}',
            headers={"stripe-signature": "sig_refund"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "charge.refunded"}
    assert len(status_updates) == 1
    assert status_updates[0]["order_id"] == "ORD_STRIPE_REFUND"
    assert status_updates[0]["status"] == "partially_refunded"
    assert status_updates[0]["payment_status"] == "partially_refunded"
    assert str(status_updates[0]["total_refunded"]) == "12"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_processed_webhook"
    assert order_events[0]["metadata"]["charge_id"] == "ch_refund_contract"
    assert order_events[0]["metadata"]["refund_amount"] == 1200


@pytest.mark.asyncio
async def test_stripe_webhook_refund_created_logs_timeline_without_mutating_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    order_events: list[Dict[str, Any]] = []
    metadata_updates: list[Dict[str, Any]] = []

    event = _stripe_event(
        "refund.created",
        {
            "id": "re_created_contract",
            "payment_intent": "pi_refund_created_contract",
            "amount": 1200,
            "currency": "usd",
            "status": "pending",
            "pending_reason": "processing",
            "destination_details": {
                "type": "card",
                "card": {
                    "reference_status": "pending",
                    "reference_type": "acquirer_reference_number",
                    "type": "refund",
                },
            },
            "metadata": {},
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_refund_created_contract"
        return {
            "order_id": "ORD_STRIPE_REFUND_CREATED",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_refund_created_contract",
            "currency": "USD",
        }

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_update_order(order_id: str, values: Dict[str, Any]) -> bool:
        metadata_updates.append({"order_id": order_id, **values})
        return True

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("refund.created should not mutate order state")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "update_order", fake_update_order)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_refund_created"}',
            headers={"stripe-signature": "sig_refund_created"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "refund.created"}
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_created_webhook"
    assert order_events[0]["order_id"] == "ORD_STRIPE_REFUND_CREATED"
    assert order_events[0]["metadata"]["refund_id"] == "re_created_contract"
    assert order_events[0]["metadata"]["status"] == "pending"
    assert len(metadata_updates) == 1
    assert metadata_updates[0]["metadata"]["stripe_refund_status"]["pending_reason"] == "processing"
    assert metadata_updates[0]["metadata"]["stripe_refund_status"]["reference_status"] == "pending"


@pytest.mark.asyncio
async def test_stripe_webhook_refund_updated_pending_logs_without_mutating_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    order_events: list[Dict[str, Any]] = []
    metadata_updates: list[Dict[str, Any]] = []

    event = _stripe_event(
        "refund.updated",
        {
            "id": "re_updated_pending",
            "payment_intent": "pi_refund_updated_pending",
            "amount": 1200,
            "currency": "usd",
            "status": "pending",
            "pending_reason": "processing",
            "destination_details": {
                "type": "card",
                "card": {
                    "reference_status": "pending",
                    "reference_type": "acquirer_reference_number",
                    "type": "refund",
                },
            },
            "metadata": {},
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_refund_updated_pending"
        return {
            "order_id": "ORD_STRIPE_REFUND_UPDATED_PENDING",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_refund_updated_pending",
            "currency": "USD",
        }

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_update_order(order_id: str, values: Dict[str, Any]) -> bool:
        metadata_updates.append({"order_id": order_id, **values})
        return True

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("pending refund.updated should not mutate order state")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "update_order", fake_update_order)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_refund_updated_pending"}',
            headers={"stripe-signature": "sig_refund_updated_pending"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "refund.updated"}
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_pending_webhook"
    assert order_events[0]["order_id"] == "ORD_STRIPE_REFUND_UPDATED_PENDING"
    assert order_events[0]["metadata"]["refund_id"] == "re_updated_pending"
    assert order_events[0]["metadata"]["pending_reason"] == "processing"
    assert len(metadata_updates) == 1
    assert metadata_updates[0]["metadata"]["stripe_refund_status"]["status"] == "pending"
    assert metadata_updates[0]["metadata"]["stripe_refund_status"]["reference_type"] == "acquirer_reference_number"


@pytest.mark.asyncio
async def test_stripe_webhook_refund_failed_rolls_back_matching_optimistic_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    event = _stripe_event(
        "refund.failed",
        {
            "id": "re_failed_contract",
            "payment_intent": "pi_refund_failed_contract",
            "amount": 1200,
            "currency": "usd",
            "failure_reason": "lost_or_stolen_card",
            "metadata": {},
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_refund_failed_contract"
        return {
            "order_id": "ORD_STRIPE_REFUND_FAILED",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_refund_failed_contract",
            "total": "20.00",
            "total_refunded": "12.00",
            "currency": "USD",
            "metadata": {
                "refund_id": "re_failed_contract",
                "refund_amount": "12.00",
            },
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_refund_failed"}',
            headers={"stripe-signature": "sig_refund_failed"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "refund.failed"}
    assert len(status_updates) == 1
    assert status_updates[0]["order_id"] == "ORD_STRIPE_REFUND_FAILED"
    assert status_updates[0]["status"] == "paid"
    assert status_updates[0]["payment_status"] == "paid"
    assert str(status_updates[0]["total_refunded"]) == "0.00"
    assert status_updates[0]["metadata"]["stripe_last_refund_failure"]["refund_id"] == "re_failed_contract"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_failed_webhook"
    assert order_events[0]["metadata"]["rollback_applied"] is True
    assert order_events[0]["metadata"]["failure_reason"] == "lost_or_stolen_card"


@pytest.mark.asyncio
async def test_stripe_webhook_refund_failed_logs_without_rollback_when_refund_id_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    order_events: list[Dict[str, Any]] = []

    event = _stripe_event(
        "refund.failed",
        {
            "id": "re_failed_mismatch",
            "payment_intent": "pi_refund_failed_mismatch",
            "amount": 1200,
            "currency": "usd",
            "failure_reason": "expired_or_canceled_card",
            "metadata": {},
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_refund_failed_mismatch"
        return {
            "order_id": "ORD_STRIPE_REFUND_FAILED_MISMATCH",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_refund_failed_mismatch",
            "total": "20.00",
            "total_refunded": "12.00",
            "currency": "USD",
            "metadata": {
                "refund_id": "re_other",
                "refund_amount": "12.00",
            },
        }

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_update_order(order_id: str, values: Dict[str, Any]) -> bool:
        return True

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("mismatched refund.failed should not roll back order state")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(webhook_routes_module, "update_order", fake_update_order)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_refund_failed_mismatch"}',
            headers={"stripe-signature": "sig_refund_failed_mismatch"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "refund.failed"}
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_failed_webhook"
    assert order_events[0]["metadata"]["rollback_applied"] is False
    assert order_events[0]["metadata"]["refund_id"] == "re_failed_mismatch"


@pytest.mark.asyncio
async def test_stripe_webhook_refund_updated_succeeded_reconciles_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    event = _stripe_event(
        "refund.updated",
        {
            "id": "re_updated_success",
            "payment_intent": "pi_refund_updated_success",
            "amount": 1200,
            "currency": "usd",
            "status": "succeeded",
            "destination_details": {
                "type": "card",
                "card": {
                    "reference": "123456789012",
                    "reference_status": "available",
                    "reference_type": "acquirer_reference_number",
                    "type": "refund",
                },
            },
            "metadata": {},
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_refund_updated_success"
        return {
            "order_id": "ORD_STRIPE_REFUND_UPDATED_SUCCESS",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_refund_updated_success",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "metadata": {},
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_refund_updated_success"}',
            headers={"stripe-signature": "sig_refund_updated_success"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "refund.updated"}
    assert len(status_updates) == 1
    assert status_updates[0]["order_id"] == "ORD_STRIPE_REFUND_UPDATED_SUCCESS"
    assert status_updates[0]["status"] == "partially_refunded"
    assert status_updates[0]["payment_status"] == "partially_refunded"
    assert str(status_updates[0]["total_refunded"]) == "12"
    assert status_updates[0]["metadata"]["stripe_refund_updated"]["refund_id"] == "re_updated_success"
    assert status_updates[0]["metadata"]["stripe_refund_status"]["reference"] == "123456789012"
    assert status_updates[0]["metadata"]["stripe_refund_status"]["tracking_reference_kind"] == "ARN"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_processed_webhook"
    assert order_events[0]["metadata"]["source_event"] == "refund.updated"


@pytest.mark.asyncio
async def test_stripe_webhook_refund_updated_failed_rolls_back_matching_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    event = _stripe_event(
        "refund.updated",
        {
            "id": "re_updated_failed",
            "payment_intent": "pi_refund_updated_failed",
            "amount": 1200,
            "currency": "usd",
            "status": "failed",
            "failure_reason": "expired_or_canceled_card",
            "metadata": {},
        },
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values["payment_intent_id"] == "pi_refund_updated_failed"
        return {
            "order_id": "ORD_STRIPE_REFUND_UPDATED_FAILED",
            "merchant_id": "m_stripe",
            "payment_intent_id": "pi_refund_updated_failed",
            "total": "20.00",
            "total_refunded": "12.00",
            "currency": "USD",
            "metadata": {
                "refund_id": "re_updated_failed",
                "refund_amount": "12.00",
            },
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_refund_updated_failed"}',
            headers={"stripe-signature": "sig_refund_updated_failed"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "refund.updated"}
    assert len(status_updates) == 1
    assert status_updates[0]["order_id"] == "ORD_STRIPE_REFUND_UPDATED_FAILED"
    assert status_updates[0]["status"] == "paid"
    assert status_updates[0]["payment_status"] == "paid"
    assert str(status_updates[0]["total_refunded"]) == "0.00"
    assert status_updates[0]["metadata"]["stripe_last_refund_failure"]["source_event"] == "refund.updated"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_failed_webhook"
    assert order_events[0]["metadata"]["rollback_applied"] is True
    assert order_events[0]["metadata"]["source_event"] == "refund.updated"


@pytest.mark.asyncio
async def test_stripe_webhook_dispute_event_upserts_record_without_mutating_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    dispute_upserts: list[Dict[str, Any]] = []
    evidence_pack_calls: list[Dict[str, Any]] = []

    event = _stripe_event(
        "charge.dispute.created",
        {
            "id": "dp_contract_1",
            "status": "needs_response",
            "charge": "ch_dispute_1",
            "metadata": {
                "merchant_id": "m_stripe",
                "order_id": "ORD_STRIPE_DISPUTE",
            },
        },
    )

    async def fake_upsert_stripe_dispute_record_best_effort(dispute: Dict[str, Any], *, event_type: str, **kwargs: Any) -> None:
        dispute_upserts.append({"dispute": dispute, "event_type": event_type, **kwargs})

    async def fake_create_dispute_evidence_pack(**kwargs: Any) -> None:
        evidence_pack_calls.append(kwargs)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dispute webhook should not mutate order payment state")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(
        dispute_records_module,
        "upsert_stripe_dispute_record_best_effort",
        fake_upsert_stripe_dispute_record_best_effort,
    )
    monkeypatch.setattr(pcs_module, "create_dispute_evidence_pack", fake_create_dispute_evidence_pack)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_dispute"}',
            headers={"stripe-signature": "sig_dispute"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "charge.dispute.created"}
    assert len(dispute_upserts) == 1
    assert dispute_upserts[0]["event_type"] == "charge.dispute.created"
    assert dispute_upserts[0]["dispute"]["id"] == "dp_contract_1"
    assert dispute_upserts[0]["dispute"]["metadata"]["order_id"] == "ORD_STRIPE_DISPUTE"
    assert evidence_pack_calls == [
        {
            "merchant_id": "m_stripe",
            "dispute_ref": "dp_contract_1",
            "order_id": "ORD_STRIPE_DISPUTE",
            "dispute_payload": {
                "id": "dp_contract_1",
                "status": "needs_response",
                "charge": "ch_dispute_1",
                "metadata": {
                    "merchant_id": "m_stripe",
                    "order_id": "ORD_STRIPE_DISPUTE",
                },
            },
            "source": "stripe",
            "status": "draft",
            "event_type": "charge.dispute.created",
            "triggered_by": "stripe_webhook:charge.dispute.created",
        }
    ]


@pytest.mark.asyncio
async def test_stripe_webhook_dispute_event_remains_success_when_upsert_is_best_effort_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    event = _stripe_event(
        "charge.dispute.closed",
        {
            "id": "dp_contract_2",
            "status": "won",
            "charge": "ch_dispute_2",
        },
    )

    async def fake_upsert_stripe_dispute_record_best_effort(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("dispute store unavailable")

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("best-effort dispute upsert should not trigger order mutation")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(
        dispute_records_module,
        "upsert_stripe_dispute_record_best_effort",
        fake_upsert_stripe_dispute_record_best_effort,
    )
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_dispute_best_effort"}',
            headers={"stripe-signature": "sig_dispute_best_effort"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "charge.dispute.closed"}


@pytest.mark.asyncio
async def test_stripe_webhook_dispute_closed_creates_frozen_evidence_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    dispute_upserts: list[Dict[str, Any]] = []
    evidence_pack_calls: list[Dict[str, Any]] = []

    event = _stripe_event(
        "charge.dispute.closed",
        {
            "id": "dp_contract_3",
            "status": "won",
            "charge": "ch_dispute_3",
            "metadata": {
                "merchant_id": "m_stripe",
                "order_id": "ORD_STRIPE_DISPUTE_CLOSED",
            },
        },
    )

    async def fake_upsert_stripe_dispute_record_best_effort(dispute: Dict[str, Any], *, event_type: str, **kwargs: Any) -> None:
        dispute_upserts.append({"dispute": dispute, "event_type": event_type, **kwargs})

    async def fake_create_dispute_evidence_pack(**kwargs: Any) -> None:
        evidence_pack_calls.append(kwargs)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dispute closed webhook should not mutate order payment state")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(
        dispute_records_module,
        "upsert_stripe_dispute_record_best_effort",
        fake_upsert_stripe_dispute_record_best_effort,
    )
    monkeypatch.setattr(pcs_module, "create_dispute_evidence_pack", fake_create_dispute_evidence_pack)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_dispute_closed"}',
            headers={"stripe-signature": "sig_dispute_closed"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "charge.dispute.closed"}
    assert len(dispute_upserts) == 1
    assert dispute_upserts[0]["event_type"] == "charge.dispute.closed"
    assert evidence_pack_calls == [
        {
            "merchant_id": "m_stripe",
            "dispute_ref": "dp_contract_3",
            "order_id": "ORD_STRIPE_DISPUTE_CLOSED",
            "dispute_payload": {
                "id": "dp_contract_3",
                "status": "won",
                "charge": "ch_dispute_3",
                "metadata": {
                    "merchant_id": "m_stripe",
                    "order_id": "ORD_STRIPE_DISPUTE_CLOSED",
                },
            },
            "source": "stripe",
            "status": "frozen",
            "event_type": "charge.dispute.closed",
            "triggered_by": "stripe_webhook:charge.dispute.closed",
        }
    ]


@pytest.mark.asyncio
async def test_stripe_webhook_dispute_funds_withdrawn_creates_frozen_evidence_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    dispute_upserts: list[Dict[str, Any]] = []
    evidence_pack_calls: list[Dict[str, Any]] = []

    event = _stripe_event(
        "charge.dispute.funds_withdrawn",
        {
            "id": "dp_contract_funds_withdrawn",
            "charge": "ch_dispute_funds_withdrawn",
            "metadata": {
                "merchant_id": "m_stripe",
                "order_id": "ORD_STRIPE_DISPUTE_FUNDS_WITHDRAWN",
            },
        },
    )

    async def fake_upsert_stripe_dispute_record_best_effort(dispute: Dict[str, Any], *, event_type: str, **kwargs: Any) -> None:
        dispute_upserts.append({"dispute": dispute, "event_type": event_type, **kwargs})

    async def fake_create_dispute_evidence_pack(**kwargs: Any) -> None:
        evidence_pack_calls.append(kwargs)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("funds_withdrawn webhook should not mutate order payment state")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(
        dispute_records_module,
        "upsert_stripe_dispute_record_best_effort",
        fake_upsert_stripe_dispute_record_best_effort,
    )
    monkeypatch.setattr(pcs_module, "create_dispute_evidence_pack", fake_create_dispute_evidence_pack)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_dispute_funds_withdrawn"}',
            headers={"stripe-signature": "sig_dispute_funds_withdrawn"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "charge.dispute.funds_withdrawn"}
    assert len(dispute_upserts) == 1
    assert dispute_upserts[0]["event_type"] == "charge.dispute.funds_withdrawn"
    assert evidence_pack_calls == [
        {
            "merchant_id": "m_stripe",
            "dispute_ref": "dp_contract_funds_withdrawn",
            "order_id": "ORD_STRIPE_DISPUTE_FUNDS_WITHDRAWN",
            "dispute_payload": {
                "id": "dp_contract_funds_withdrawn",
                "charge": "ch_dispute_funds_withdrawn",
                "metadata": {
                    "merchant_id": "m_stripe",
                    "order_id": "ORD_STRIPE_DISPUTE_FUNDS_WITHDRAWN",
                },
            },
            "source": "stripe",
            "status": "frozen",
            "event_type": "charge.dispute.funds_withdrawn",
            "triggered_by": "stripe_webhook:charge.dispute.funds_withdrawn",
        }
    ]


@pytest.mark.asyncio
async def test_stripe_webhook_dispute_warning_closed_creates_frozen_evidence_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    dispute_upserts: list[Dict[str, Any]] = []
    evidence_pack_calls: list[Dict[str, Any]] = []

    event = _stripe_event(
        "charge.dispute.updated",
        {
            "id": "dp_contract_warning_closed",
            "status": "warning_closed",
            "charge": "ch_dispute_warning_closed",
            "metadata": {
                "merchant_id": "m_stripe",
                "order_id": "ORD_STRIPE_DISPUTE_WARNING_CLOSED",
            },
        },
    )

    async def fake_upsert_stripe_dispute_record_best_effort(dispute: Dict[str, Any], *, event_type: str, **kwargs: Any) -> None:
        dispute_upserts.append({"dispute": dispute, "event_type": event_type, **kwargs})

    async def fake_create_dispute_evidence_pack(**kwargs: Any) -> None:
        evidence_pack_calls.append(kwargs)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("warning_closed webhook should not mutate order payment state")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(
        dispute_records_module,
        "upsert_stripe_dispute_record_best_effort",
        fake_upsert_stripe_dispute_record_best_effort,
    )
    monkeypatch.setattr(pcs_module, "create_dispute_evidence_pack", fake_create_dispute_evidence_pack)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_dispute_warning_closed"}',
            headers={"stripe-signature": "sig_dispute_warning_closed"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "charge.dispute.updated"}
    assert len(dispute_upserts) == 1
    assert dispute_upserts[0]["event_type"] == "charge.dispute.updated"
    assert evidence_pack_calls == [
        {
            "merchant_id": "m_stripe",
            "dispute_ref": "dp_contract_warning_closed",
            "order_id": "ORD_STRIPE_DISPUTE_WARNING_CLOSED",
            "dispute_payload": {
                "id": "dp_contract_warning_closed",
                "status": "warning_closed",
                "charge": "ch_dispute_warning_closed",
                "metadata": {
                    "merchant_id": "m_stripe",
                    "order_id": "ORD_STRIPE_DISPUTE_WARNING_CLOSED",
                },
            },
            "source": "stripe",
            "status": "frozen",
            "event_type": "charge.dispute.updated",
            "triggered_by": "stripe_webhook:charge.dispute.updated",
        }
    ]


@pytest.mark.asyncio
async def test_stripe_webhook_dispute_warning_needs_response_stays_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    dispute_upserts: list[Dict[str, Any]] = []
    evidence_pack_calls: list[Dict[str, Any]] = []

    event = _stripe_event(
        "charge.dispute.updated",
        {
            "id": "dp_contract_warning_needs_response",
            "status": "warning_needs_response",
            "charge": "ch_dispute_warning_needs_response",
            "metadata": {
                "merchant_id": "m_stripe",
                "order_id": "ORD_STRIPE_DISPUTE_WARNING_NEEDS_RESPONSE",
            },
        },
    )

    async def fake_upsert_stripe_dispute_record_best_effort(dispute: Dict[str, Any], *, event_type: str, **kwargs: Any) -> None:
        dispute_upserts.append({"dispute": dispute, "event_type": event_type, **kwargs})

    async def fake_create_dispute_evidence_pack(**kwargs: Any) -> None:
        evidence_pack_calls.append(kwargs)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("warning_needs_response webhook should not mutate order payment state")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(
        dispute_records_module,
        "upsert_stripe_dispute_record_best_effort",
        fake_upsert_stripe_dispute_record_best_effort,
    )
    monkeypatch.setattr(pcs_module, "create_dispute_evidence_pack", fake_create_dispute_evidence_pack)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_dispute_warning_needs_response"}',
            headers={"stripe-signature": "sig_dispute_warning_needs_response"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "charge.dispute.updated"}
    assert len(dispute_upserts) == 1
    assert dispute_upserts[0]["event_type"] == "charge.dispute.updated"
    assert evidence_pack_calls == [
        {
            "merchant_id": "m_stripe",
            "dispute_ref": "dp_contract_warning_needs_response",
            "order_id": "ORD_STRIPE_DISPUTE_WARNING_NEEDS_RESPONSE",
            "dispute_payload": {
                "id": "dp_contract_warning_needs_response",
                "status": "warning_needs_response",
                "charge": "ch_dispute_warning_needs_response",
                "metadata": {
                    "merchant_id": "m_stripe",
                    "order_id": "ORD_STRIPE_DISPUTE_WARNING_NEEDS_RESPONSE",
                },
            },
            "source": "stripe",
            "status": "draft",
            "event_type": "charge.dispute.updated",
            "triggered_by": "stripe_webhook:charge.dispute.updated",
        }
    ]


@pytest.mark.asyncio
async def test_stripe_webhook_dispute_warning_under_review_is_frozen_issuer_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    dispute_upserts: list[Dict[str, Any]] = []
    evidence_pack_calls: list[Dict[str, Any]] = []

    event = _stripe_event(
        "charge.dispute.updated",
        {
            "id": "dp_contract_warning_under_review",
            "status": "warning_under_review",
            "charge": "ch_dispute_warning_under_review",
            "evidence_details": {"has_evidence": True, "submission_count": 1},
            "metadata": {
                "merchant_id": "m_stripe",
                "order_id": "ORD_STRIPE_DISPUTE_WARNING_UNDER_REVIEW",
            },
        },
    )

    async def fake_upsert_stripe_dispute_record_best_effort(dispute: Dict[str, Any], *, event_type: str, **kwargs: Any) -> None:
        dispute_upserts.append({"dispute": dispute, "event_type": event_type, **kwargs})

    async def fake_create_dispute_evidence_pack(**kwargs: Any) -> None:
        evidence_pack_calls.append(kwargs)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("warning_under_review webhook should not mutate order payment state")

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(
        dispute_records_module,
        "upsert_stripe_dispute_record_best_effort",
        fake_upsert_stripe_dispute_record_best_effort,
    )
    monkeypatch.setattr(pcs_module, "create_dispute_evidence_pack", fake_create_dispute_evidence_pack)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fail_if_called)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_dispute_warning_under_review"}',
            headers={"stripe-signature": "sig_dispute_warning_under_review"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "event": "charge.dispute.updated"}
    assert len(dispute_upserts) == 1
    assert dispute_upserts[0]["event_type"] == "charge.dispute.updated"
    assert evidence_pack_calls == [
        {
            "merchant_id": "m_stripe",
            "dispute_ref": "dp_contract_warning_under_review",
            "order_id": "ORD_STRIPE_DISPUTE_WARNING_UNDER_REVIEW",
            "dispute_payload": {
                "id": "dp_contract_warning_under_review",
                "status": "warning_under_review",
                "charge": "ch_dispute_warning_under_review",
                "evidence_details": {"has_evidence": True, "submission_count": 1},
                "metadata": {
                    "merchant_id": "m_stripe",
                    "order_id": "ORD_STRIPE_DISPUTE_WARNING_UNDER_REVIEW",
                },
            },
            "source": "stripe",
            "status": "frozen",
            "event_type": "charge.dispute.updated",
            "triggered_by": "stripe_webhook:charge.dispute.updated",
        }
    ]
