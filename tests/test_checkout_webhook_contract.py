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

os.chdir(REPO_ROOT)

from main import app


def _checkout_payload(order_id: str, *, event_id: str, event_type: str = "payment_captured") -> Dict[str, Any]:
    return {
        "type": event_type,
        "data": {
            "id": event_id,
            "reference": order_id,
            "approved": True,
        },
    }


@pytest.mark.asyncio
async def test_checkout_webhook_rejects_invalid_signature_and_records_failed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.webhook_service as webhook_module

    recorded_events: list[Dict[str, Any]] = []

    async def fake_verify_checkout_signature(payload: bytes, signature_header: str, secret: str) -> bool:
        return False

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        recorded_events.append(kwargs)
        return 1

    monkeypatch.setenv("CHECKOUT_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "verify_checkout_signature",
        staticmethod(fake_verify_checkout_signature),
    )
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "record_webhook_event",
        staticmethod(fake_record_webhook_event),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json=_checkout_payload("ORD_SIG", event_id="evt_invalid_sig"),
            headers={"Cko-Signature": "bad_sig"},
        )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid signature"
    assert len(recorded_events) == 1
    assert recorded_events[0]["event_id"] == "evt_invalid_sig"
    assert recorded_events[0]["event_type"] == "payment_captured"
    assert recorded_events[0]["psp_type"] == "checkout"
    assert recorded_events[0]["order_id"] == "ORD_SIG"
    assert recorded_events[0]["payload"] == _checkout_payload("ORD_SIG", event_id="evt_invalid_sig")
    assert recorded_events[0]["signature_verified"] is False
    assert recorded_events[0]["signature_header"] == "bad_sig"
    assert recorded_events[0]["status"] == "failed"
    assert recorded_events[0]["headers"]["cko-signature"] == "bad_sig"


@pytest.mark.asyncio
async def test_checkout_webhook_rejects_missing_signature_when_secret_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.webhook_service as webhook_module

    recorded_events: list[Dict[str, Any]] = []

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        recorded_events.append(kwargs)
        return 1

    monkeypatch.setenv("CHECKOUT_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "record_webhook_event",
        staticmethod(fake_record_webhook_event),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json=_checkout_payload("ORD_NO_SIG", event_id="evt_missing_sig"),
        )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing signature"
    assert len(recorded_events) == 1
    assert recorded_events[0]["event_id"] == "evt_missing_sig"
    assert recorded_events[0]["order_id"] == "ORD_NO_SIG"
    assert recorded_events[0]["signature_verified"] is False
    assert recorded_events[0]["signature_header"] is None
    assert recorded_events[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_checkout_webhook_ignores_missing_order_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.webhook_service as webhook_module

    recorded_events: list[Dict[str, Any]] = []

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        recorded_events.append(kwargs)
        return 1

    monkeypatch.delenv("CHECKOUT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "record_webhook_event",
        staticmethod(fake_record_webhook_event),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json={
                "type": "payment_captured",
                "data": {
                    "id": "evt_missing_ref",
                    "approved": True,
                },
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "ignored",
        "reason": "missing_order_reference",
        "event_id": "evt_missing_ref",
    }
    assert recorded_events == [
        {
            "event_id": "evt_missing_ref",
            "event_type": "payment_captured",
            "psp_type": "checkout",
            "order_id": None,
            "payload": {
                "type": "payment_captured",
                "data": {"id": "evt_missing_ref", "approved": True},
            },
            "status": "ignored",
        }
    ]


@pytest.mark.asyncio
async def test_checkout_webhook_ignores_non_success_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.webhook_service as webhook_module

    recorded_events: list[Dict[str, Any]] = []

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        recorded_events.append(kwargs)
        return 1

    monkeypatch.delenv("CHECKOUT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "record_webhook_event",
        staticmethod(fake_record_webhook_event),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json={
                "type": "payment_declined",
                "data": {
                    "id": "evt_declined",
                    "reference": "ORD_DECLINED",
                    "approved": False,
                },
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "ignored",
        "reason": "event payment_declined",
        "event_id": "evt_declined",
    }
    assert recorded_events == [
        {
            "event_id": "evt_declined",
            "event_type": "payment_declined",
            "psp_type": "checkout",
            "order_id": "ORD_DECLINED",
            "payload": {
                "type": "payment_declined",
                "data": {
                    "id": "evt_declined",
                    "reference": "ORD_DECLINED",
                    "approved": False,
                },
            },
            "status": "ignored",
        }
    ]


@pytest.mark.asyncio
async def test_checkout_webhook_duplicate_event_short_circuits_without_marking_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.orders as orders_module
    import services.webhook_service as webhook_module

    record_calls: list[Dict[str, Any]] = []
    mark_paid_calls: list[str] = []

    async def fake_check_duplicate_event(event_id: str, order_id: str | None = None):
        return True, {"event_id": event_id, "order_id": order_id, "status": "processed"}

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        record_calls.append(kwargs)
        return 1

    async def fake_mark_order_paid(order_id: str) -> None:
        mark_paid_calls.append(order_id)

    monkeypatch.delenv("CHECKOUT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "check_duplicate_event",
        staticmethod(fake_check_duplicate_event),
    )
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "record_webhook_event",
        staticmethod(fake_record_webhook_event),
    )
    monkeypatch.setattr(orders_module, "mark_order_paid", fake_mark_order_paid)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json=_checkout_payload("ORD_DUPLICATE", event_id="evt_duplicate"),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "duplicate"
    assert body["event_id"] == "evt_duplicate"
    assert not record_calls
    assert not mark_paid_calls


@pytest.mark.asyncio
async def test_checkout_webhook_reprocesses_existing_failed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.orders as orders_module
    import routes.order_routes as order_routes_module
    import services.merchant_store_service as merchant_store_service
    import services.webhook_service as webhook_module

    recorded_events: list[Dict[str, Any]] = []
    status_updates: list[tuple[str, str, str | None]] = []
    payment_updates: list[Dict[str, Any]] = []
    mark_paid_calls: list[str] = []
    order_events: list[Dict[str, Any]] = []

    async def fake_check_duplicate_event(event_id: str, order_id: str | None = None):
        return False, {
            "id": 17,
            "event_id": event_id,
            "order_id": order_id,
            "status": "failed",
            "processed_at": None,
            "error_message": "transient upstream timeout",
        }

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        recorded_events.append(kwargs)
        return 17

    async def fake_update_event_status(event_id: str, status: str, error_message: str | None = None) -> None:
        status_updates.append((event_id, status, error_message))

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_webhook",
            "payment_status": "awaiting_payment",
            "shopify_order_id": None,
            "payment_intent_id": "pi_retryable",
            "client_secret": "cs_retryable",
            "total": "45.20",
            "currency": "USD",
        }

    async def fake_update_payment_info(**kwargs: Any) -> None:
        payment_updates.append(kwargs)

    async def fake_mark_order_paid(order_id: str) -> bool:
        mark_paid_calls.append(order_id)
        return True

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_get_primary_store(merchant_id: str):
        return None

    async def fake_create_shopify_order(order_id: str) -> None:
        raise AssertionError("shopify sync should not run when no primary store is configured")

    monkeypatch.delenv("CHECKOUT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "check_duplicate_event",
        staticmethod(fake_check_duplicate_event),
    )
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "record_webhook_event",
        staticmethod(fake_record_webhook_event),
    )
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "update_event_status",
        staticmethod(fake_update_event_status),
    )
    monkeypatch.setattr(orders_module, "get_order", fake_get_order)
    monkeypatch.setattr(orders_module, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(orders_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(order_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fake_create_shopify_order)
    monkeypatch.setattr(merchant_store_service, "get_primary_store", fake_get_primary_store)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json=_checkout_payload("ORD_RETRYABLE", event_id="evt_retryable"),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["event_id"] == "evt_retryable"
    assert recorded_events[0]["status"] == "pending"
    assert payment_updates == [
        {
            "order_id": "ORD_RETRYABLE",
            "payment_intent_id": "evt_retryable",
            "client_secret": "cs_retryable",
            "payment_status": "paid",
            "psp_used": "checkout",
        }
    ]
    assert mark_paid_calls == ["ORD_RETRYABLE"]
    assert status_updates == [("evt_retryable", "processed", None)]
    assert order_events[0]["event_type"] == "payment_succeeded"


@pytest.mark.asyncio
async def test_checkout_webhook_already_paid_marks_event_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.orders as orders_module
    import routes.order_routes as order_routes_module
    import services.merchant_store_service as merchant_store_service
    import services.webhook_service as webhook_module

    recorded_events: list[Dict[str, Any]] = []
    status_updates: list[tuple[str, str, str | None]] = []
    payment_updates: list[Dict[str, Any]] = []
    mark_paid_calls: list[str] = []

    async def fake_check_duplicate_event(event_id: str, order_id: str | None = None):
        return False, None

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        recorded_events.append(kwargs)
        return 1

    async def fake_update_event_status(event_id: str, status: str, error_message: str | None = None) -> None:
        status_updates.append((event_id, status, error_message))

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_webhook",
            "payment_status": "paid",
            "shopify_order_id": "shop_123",
            "payment_intent_id": "pi_paid",
            "client_secret": "cs_paid",
            "total": "45.20",
            "currency": "USD",
        }

    async def fake_update_payment_info(**kwargs: Any) -> None:
        payment_updates.append(kwargs)

    async def fake_mark_order_paid(order_id: str) -> None:
        mark_paid_calls.append(order_id)

    async def fake_log_order_event(**kwargs: Any) -> None:
        raise AssertionError("already-paid webhook should not log a second payment_succeeded event")

    async def fake_get_primary_store(merchant_id: str):
        return None

    async def fake_create_shopify_order(order_id: str) -> None:
        raise AssertionError("already-linked order should not enqueue shopify sync")

    monkeypatch.delenv("CHECKOUT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "check_duplicate_event",
        staticmethod(fake_check_duplicate_event),
    )
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "record_webhook_event",
        staticmethod(fake_record_webhook_event),
    )
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "update_event_status",
        staticmethod(fake_update_event_status),
    )
    monkeypatch.setattr(orders_module, "get_order", fake_get_order)
    monkeypatch.setattr(orders_module, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(orders_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(order_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fake_create_shopify_order)
    monkeypatch.setattr(merchant_store_service, "get_primary_store", fake_get_primary_store)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json=_checkout_payload("ORD_ALREADY_PAID", event_id="evt_already_paid"),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "already_paid"
    assert body["shopify_sync"] == "already_linked"
    assert recorded_events[0]["status"] == "pending"
    assert status_updates == [("evt_already_paid", "processed", None)]
    assert not payment_updates
    assert not mark_paid_calls


@pytest.mark.asyncio
async def test_checkout_webhook_success_marks_paid_and_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.orders as orders_module
    import routes.order_routes as order_routes_module
    import services.merchant_store_service as merchant_store_service
    import services.webhook_service as webhook_module

    recorded_events: list[Dict[str, Any]] = []
    status_updates: list[tuple[str, str, str | None]] = []
    payment_updates: list[Dict[str, Any]] = []
    mark_paid_calls: list[str] = []
    order_events: list[Dict[str, Any]] = []

    async def fake_check_duplicate_event(event_id: str, order_id: str | None = None):
        return False, None

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        recorded_events.append(kwargs)
        return 1

    async def fake_update_event_status(event_id: str, status: str, error_message: str | None = None) -> None:
        status_updates.append((event_id, status, error_message))

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_webhook",
            "payment_status": "awaiting_payment",
            "shopify_order_id": None,
            "payment_intent_id": "pi_pending",
            "client_secret": "cs_pending",
            "total": "45.20",
            "currency": "USD",
        }

    async def fake_update_payment_info(**kwargs: Any) -> None:
        payment_updates.append(kwargs)

    async def fake_mark_order_paid(order_id: str) -> bool:
        mark_paid_calls.append(order_id)
        return True

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_get_primary_store(merchant_id: str):
        return None

    async def fake_create_shopify_order(order_id: str) -> None:
        raise AssertionError("shopify sync should not run when no primary store is configured")

    monkeypatch.delenv("CHECKOUT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "check_duplicate_event",
        staticmethod(fake_check_duplicate_event),
    )
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "record_webhook_event",
        staticmethod(fake_record_webhook_event),
    )
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "update_event_status",
        staticmethod(fake_update_event_status),
    )
    monkeypatch.setattr(orders_module, "get_order", fake_get_order)
    monkeypatch.setattr(orders_module, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(orders_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(order_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fake_create_shopify_order)
    monkeypatch.setattr(merchant_store_service, "get_primary_store", fake_get_primary_store)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json=_checkout_payload("ORD_SUCCESS", event_id="evt_success"),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["event_id"] == "evt_success"
    assert recorded_events[0]["status"] == "pending"
    assert payment_updates == [
        {
            "order_id": "ORD_SUCCESS",
            "payment_intent_id": "evt_success",
            "client_secret": "cs_pending",
            "payment_status": "paid",
            "psp_used": "checkout",
        }
    ]
    assert mark_paid_calls == ["ORD_SUCCESS"]
    assert status_updates == [("evt_success", "processed", None)]
    assert order_events[0]["event_type"] == "payment_succeeded"
    assert order_events[0]["metadata"]["webhook_event_id"] == "evt_success"


@pytest.mark.asyncio
async def test_checkout_webhook_missing_order_marks_event_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.orders as orders_module
    import services.webhook_service as webhook_module

    recorded_events: list[Dict[str, Any]] = []
    status_updates: list[tuple[str, str, str | None]] = []

    async def fake_check_duplicate_event(event_id: str, order_id: str | None = None):
        return False, None

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        recorded_events.append(kwargs)
        return 1

    async def fake_update_event_status(event_id: str, status: str, error_message: str | None = None) -> None:
        status_updates.append((event_id, status, error_message))

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return None

    monkeypatch.delenv("CHECKOUT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "check_duplicate_event",
        staticmethod(fake_check_duplicate_event),
    )
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "record_webhook_event",
        staticmethod(fake_record_webhook_event),
    )
    monkeypatch.setattr(
        webhook_module.WebhookService,
        "update_event_status",
        staticmethod(fake_update_event_status),
    )
    monkeypatch.setattr(orders_module, "get_order", fake_get_order)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json=_checkout_payload("ORD_MISSING", event_id="evt_missing_order"),
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Order not found"
    assert recorded_events[0]["status"] == "pending"
    assert status_updates == [("evt_missing_order", "failed", "Order not found")]


@pytest.mark.asyncio
async def test_checkout_webhook_enqueues_the_merchant_order_durably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two sites in this webhook used to dispatch the merchant-order create
    through BackgroundTasks, which died with the process. They now enqueue, and
    must carry `require_shopify_primary` — this path has only ever created
    merchant orders for a Shopify PRIMARY store, unlike the sync it calls."""
    import db.orders as orders_module
    import routes.order_routes as order_routes_module
    import routes.payment_routes as payment_routes_module
    import services.webhook_service as webhook_module

    enqueued: list[Dict[str, Any]] = []

    async def fake_check_duplicate_event(event_id: str, order_id: str | None = None):
        return False, None

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        return 21

    async def fake_update_event_status(event_id: str, status: str, error_message: str | None = None) -> None:
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_webhook",
            "payment_status": "awaiting_payment",
            "shopify_order_id": None,
            "payment_intent_id": "pi_enqueue",
            "client_secret": "cs_enqueue",
            "total": "45.20",
            "currency": "USD",
        }

    async def fake_update_payment_info(**kwargs: Any) -> None:
        return None

    async def fake_mark_order_paid(order_id: str) -> bool:
        return True

    async def fake_log_order_event(**kwargs: Any) -> None:
        return None

    async def fake_enqueue(*, order_id, merchant_id, require_shopify_primary=False):
        enqueued.append({
            "order_id": order_id,
            "merchant_id": merchant_id,
            "require_shopify_primary": require_shopify_primary,
        })
        return "job-1"

    monkeypatch.delenv("CHECKOUT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webhook_module.WebhookService, "check_duplicate_event", staticmethod(fake_check_duplicate_event))
    monkeypatch.setattr(webhook_module.WebhookService, "record_webhook_event", staticmethod(fake_record_webhook_event))
    monkeypatch.setattr(webhook_module.WebhookService, "update_event_status", staticmethod(fake_update_event_status))
    monkeypatch.setattr(orders_module, "get_order", fake_get_order)
    monkeypatch.setattr(orders_module, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(orders_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(order_routes_module, "log_order_event", fake_log_order_event)
    # Patch where the name is USED: payment_routes binds it with a module-level
    # `from ... import`, so patching the source module would not reach it.
    monkeypatch.setattr(payment_routes_module, "enqueue_merchant_order_create", fake_enqueue)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json=_checkout_payload("ORD_ENQUEUE", event_id="evt_enqueue"),
        )

    assert resp.status_code == 200
    assert enqueued == [{
        "order_id": "ORD_ENQUEUE",
        "merchant_id": "m_webhook",
        "require_shopify_primary": True,
    }]


@pytest.mark.asyncio
async def test_checkout_webhook_already_paid_branch_also_enqueues_durably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The already-paid branch is the SECOND of this webhook's two create sites.
    It is reached on a redelivered webhook for an order that was finalized by
    another path — precisely when the first attempt's background task may have
    been the thing that died."""
    import db.orders as orders_module
    import routes.payment_routes as payment_routes_module
    import services.webhook_service as webhook_module

    enqueued: list[Dict[str, Any]] = []

    async def fake_check_duplicate_event(event_id: str, order_id: str | None = None):
        return False, None

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        return 22

    async def fake_update_event_status(event_id: str, status: str, error_message: str | None = None) -> None:
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return {
            "order_id": order_id,
            "merchant_id": "m_already",
            # already paid, and no merchant order yet
            "payment_status": "paid",
            "shopify_order_id": None,
            "payment_intent_id": "pi_already",
            "total": "45.20",
            "currency": "USD",
        }

    async def fake_enqueue(*, order_id, merchant_id, require_shopify_primary=False):
        enqueued.append({
            "order_id": order_id,
            "merchant_id": merchant_id,
            "require_shopify_primary": require_shopify_primary,
        })
        return "job-1"

    monkeypatch.delenv("CHECKOUT_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(webhook_module.WebhookService, "check_duplicate_event", staticmethod(fake_check_duplicate_event))
    monkeypatch.setattr(webhook_module.WebhookService, "record_webhook_event", staticmethod(fake_record_webhook_event))
    monkeypatch.setattr(webhook_module.WebhookService, "update_event_status", staticmethod(fake_update_event_status))
    monkeypatch.setattr(orders_module, "get_order", fake_get_order)
    monkeypatch.setattr(payment_routes_module, "enqueue_merchant_order_create", fake_enqueue)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/payments/webhooks/checkout",
            json=_checkout_payload("ORD_ALREADY", event_id="evt_already_enqueue"),
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "already_paid"
    assert enqueued == [{
        "order_id": "ORD_ALREADY",
        "merchant_id": "m_already",
        "require_shopify_primary": True,
    }]
