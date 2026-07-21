from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


def _stripe_event(event_type: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": event_type,
        "data": {
            "object": obj,
        },
    }


def _adyen_escape_component(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace(":", "\\:")


def _adyen_hmac_signature(notification: Dict[str, Any], secret: str) -> str:
    amount = notification.get("amount") or {}
    parts = [
        notification.get("pspReference"),
        notification.get("originalReference"),
        notification.get("merchantAccountCode"),
        notification.get("merchantReference"),
        amount.get("value"),
        amount.get("currency"),
        notification.get("eventCode"),
        notification.get("success"),
    ]
    signing_string = ":".join(_adyen_escape_component(part) for part in parts)
    key = bytes.fromhex(secret)
    digest = hmac.new(key, signing_string.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _adyen_payload(
    order_id: str,
    *,
    psp_reference: str,
    success: str,
    event_code: str,
    amount_value: int = 1200,
    amount_currency: str = "USD",
    original_reference: Optional[str] = None,
) -> Dict[str, Any]:
    notification = {
        "eventCode": event_code,
        "success": success,
        "pspReference": psp_reference,
        "originalReference": original_reference,
        "merchantReference": order_id,
        "merchantAccountCode": "WoopayECOM",
        "amount": {
            "value": amount_value,
            "currency": amount_currency,
        },
        "additionalData": {},
    }
    return {
        "notificationItems": [
            {
                "NotificationRequestItem": notification,
            }
        ]
    }


@pytest.mark.asyncio
async def test_payment_aftercare_canary_stripe_refund_timeline_converges_under_replay_and_out_of_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    state: Dict[str, Any] = {
        "order_id": "ORD_AFTERCARE_STRIPE",
        "merchant_id": "m_stripe",
        "payment_intent_id": "pi_aftercare_stripe",
        "total": "20.00",
        "total_refunded": "0.00",
        "currency": "USD",
        "metadata": {"refund_id": "re_aftercare_stripe"},
    }
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []
    events = iter(
        [
            _stripe_event(
                "refund.updated",
                {
                    "id": "re_aftercare_stripe",
                    "payment_intent": "pi_aftercare_stripe",
                    "amount": 1200,
                    "currency": "usd",
                    "status": "pending",
                    "pending_reason": "processing",
                    "metadata": {},
                },
            ),
            _stripe_event(
                "charge.refunded",
                {
                    "id": "ch_aftercare_stripe",
                    "payment_intent": "pi_aftercare_stripe",
                    "amount_refunded": 1200,
                    "currency": "usd",
                },
            ),
            _stripe_event(
                "refund.updated",
                {
                    "id": "re_aftercare_stripe",
                    "payment_intent": "pi_aftercare_stripe",
                    "amount": 1200,
                    "currency": "usd",
                    "status": "succeeded",
                    "metadata": {},
                },
            ),
        ]
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
        if values:
            assert values["payment_intent_id"] == "pi_aftercare_stripe"
        return dict(state)

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        assert order_id == state["order_id"]
        state["status"] = status
        state.update(kwargs)
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return next(events)

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
        resp_pending = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_refund_pending"}',
            headers={"stripe-signature": "sig_refund_pending"},
        )
        resp_refunded = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_charge_refunded"}',
            headers={"stripe-signature": "sig_charge_refunded"},
        )
        resp_replayed = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_refund_succeeded_late"}',
            headers={"stripe-signature": "sig_refund_succeeded_late"},
        )

    assert resp_pending.status_code == 200
    assert resp_refunded.status_code == 200
    assert resp_replayed.status_code == 200
    assert len(status_updates) == 2
    assert all(str(call["total_refunded"]) == "12" for call in status_updates)
    assert str(state["total_refunded"]) == "12"
    assert state["payment_status"] == "partially_refunded"
    assert order_events[0]["event_type"] == "refund_pending_webhook"
    assert order_events[1]["event_type"] == "refund_processed_webhook"
    assert order_events[2]["event_type"] == "refund_processed_webhook"
    assert order_events[2]["metadata"]["source_event"] == "refund.updated"


@pytest.mark.asyncio
async def test_payment_aftercare_canary_adyen_refund_timeline_converges_under_reversal_then_duplicate_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    state: Dict[str, Any] = {
        "order_id": "ORD_AFTERCARE_ADYEN",
        "merchant_id": "m_adyen",
        "status": "paid",
        "payment_status": "paid",
        "total": "20.00",
        "total_refunded": "0.00",
        "currency": "USD",
        "metadata": {},
    }
    webhook_calls: list[tuple[str, str, str, str]] = []
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []
    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

    reversal_payload = _adyen_payload(
        "ORD_AFTERCARE_ADYEN",
        psp_reference="PSP_AFTERCARE_REVERSAL",
        success="true",
        event_code="REFUNDED_REVERSED",
    )
    refund_payload = _adyen_payload(
        "ORD_AFTERCARE_ADYEN",
        psp_reference="PSP_AFTERCARE_REFUND",
        success="true",
        event_code="REFUND_WITH_DATA",
    )
    duplicate_refund_payload = _adyen_payload(
        "ORD_AFTERCARE_ADYEN",
        psp_reference="PSP_AFTERCARE_REFUND",
        success="true",
        event_code="REFUND_WITH_DATA",
    )
    for payload in (reversal_payload, refund_payload, duplicate_refund_payload):
        notification = payload["notificationItems"][0]["NotificationRequestItem"]
        notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        assert order_id == state["order_id"]
        return dict(state)

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        assert order_id == state["order_id"]
        state["status"] = status
        state.update(kwargs)
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_reversal = await client.post(
            "/psp/webhook/adyen",
            json=reversal_payload,
            auth=("adyen_user", "adyen_pass"),
        )
        resp_refund = await client.post(
            "/psp/webhook/adyen",
            json=refund_payload,
            auth=("adyen_user", "adyen_pass"),
        )
        resp_duplicate = await client.post(
            "/psp/webhook/adyen",
            json=duplicate_refund_payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp_reversal.status_code == 200
    assert resp_refund.status_code == 200
    assert resp_duplicate.status_code == 200
    assert len(status_updates) == 1
    assert status_updates[0]["status"] == "partially_refunded"
    assert str(status_updates[0]["total_refunded"]) == "12.00"
    assert str(state["total_refunded"]) == "12.00"
    assert state["payment_status"] == "partially_refunded"
    assert state["metadata"]["adyen_refund_psp_refs"] == ["PSP_AFTERCARE_REFUND"]
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_processed_webhook"
    assert order_events[0]["metadata"]["event_code"] == "REFUND_WITH_DATA"
    assert webhook_calls == [
        ("ORD_AFTERCARE_ADYEN", "reversed", "adyen", "PSP_AFTERCARE_REVERSAL"),
        ("ORD_AFTERCARE_ADYEN", "refunded", "adyen", "PSP_AFTERCARE_REFUND"),
        ("ORD_AFTERCARE_ADYEN", "refunded", "adyen", "PSP_AFTERCARE_REFUND"),
    ]


@pytest.mark.asyncio
async def test_payment_aftercare_canary_checkout_failed_then_reprocess_then_duplicate_processed_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.orders as orders_module
    import routes.order_routes as order_routes_module
    import services.merchant_store_service as merchant_store_service
    import services.webhook_service as webhook_module

    state: Dict[str, Any] = {
        "order_id": "ORD_AFTERCARE_CHECKOUT",
        "merchant_id": "m_checkout",
        "payment_status": "awaiting_payment",
        "shopify_order_id": None,
        "payment_intent_id": "pi_aftercare_checkout",
        "client_secret": "cs_aftercare_checkout",
        "total": "45.20",
        "currency": "USD",
    }
    duplicate_outcomes = iter(
        [
            (False, {"id": 41, "event_id": "evt_aftercare_checkout", "order_id": "ORD_AFTERCARE_CHECKOUT", "status": "failed"}),
            (True, {"id": 41, "event_id": "evt_aftercare_checkout", "order_id": "ORD_AFTERCARE_CHECKOUT", "status": "processed"}),
        ]
    )
    recorded_events: list[Dict[str, Any]] = []
    status_updates: list[tuple[str, str, Optional[str]]] = []
    payment_updates: list[Dict[str, Any]] = []
    mark_paid_calls: list[str] = []
    order_events: list[Dict[str, Any]] = []

    async def fake_check_duplicate_event(event_id: str, order_id: Optional[str] = None):
        assert event_id == "evt_aftercare_checkout"
        assert order_id == "ORD_AFTERCARE_CHECKOUT"
        return next(duplicate_outcomes)

    async def fake_record_webhook_event(**kwargs: Any) -> int:
        recorded_events.append(kwargs)
        return 41

    async def fake_update_event_status(event_id: str, status: str, error_message: Optional[str] = None) -> None:
        status_updates.append((event_id, status, error_message))

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        assert order_id == state["order_id"]
        return dict(state)

    async def fake_update_payment_info(**kwargs: Any) -> None:
        state["payment_intent_id"] = kwargs["payment_intent_id"]
        state["payment_status"] = kwargs["payment_status"]
        payment_updates.append(kwargs)

    async def fake_mark_order_paid(order_id: str) -> bool:
        state["payment_status"] = "paid"
        mark_paid_calls.append(order_id)
        return True

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_get_primary_store(merchant_id: str):
        return None

    async def fake_create_shopify_order(order_id: str) -> None:
        raise AssertionError("shopify sync should not run in checkout aftercare canary")

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

    payload = {
        "type": "payment_captured",
        "data": {
            "id": "evt_aftercare_checkout",
            "reference": "ORD_AFTERCARE_CHECKOUT",
            "approved": True,
        },
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_reprocess = await client.post("/api/payments/webhooks/checkout", json=payload)
        resp_duplicate = await client.post("/api/payments/webhooks/checkout", json=payload)

    assert resp_reprocess.status_code == 200
    assert resp_reprocess.json()["status"] == "success"
    assert resp_duplicate.status_code == 200
    assert resp_duplicate.json()["status"] == "duplicate"
    assert len(recorded_events) == 1
    assert recorded_events[0]["status"] == "pending"
    assert payment_updates == [
        {
            "order_id": "ORD_AFTERCARE_CHECKOUT",
            "payment_intent_id": "evt_aftercare_checkout",
            "client_secret": "cs_aftercare_checkout",
            "payment_status": "paid",
            "psp_used": "checkout",
        }
    ]
    assert mark_paid_calls == ["ORD_AFTERCARE_CHECKOUT"]
    assert status_updates == [("evt_aftercare_checkout", "processed", None)]
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "payment_succeeded"
    assert state["payment_status"] == "paid"


@pytest.mark.asyncio
async def test_payment_aftercare_canary_stripe_dispute_pack_does_not_downgrade_from_frozen_to_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.pcs_evidence_pack_service as pcs_module

    existing_pack = {
        "id": 8,
        "pack_version": 2,
        "status": "frozen",
        "merchant_id": "m_stripe",
        "order_id": "ORD_AFTERCARE_DISPUTE",
        "dispute_ref": "dp_aftercare_1",
    }

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        assert values == {"merchant_id": "m_stripe", "dispute_ref": "dp_aftercare_1"}
        return dict(existing_pack)

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("frozen dispute pack should not be downgraded or regenerated")

    monkeypatch.setattr(pcs_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(pcs_module.database, "execute", fail_if_called)
    monkeypatch.setattr(pcs_module, "get_order", fail_if_called)
    monkeypatch.setattr(pcs_module, "get_latest_policy_hashes", fail_if_called)
    monkeypatch.setattr(pcs_module, "_collect_audit_trail_refs", fail_if_called)

    result = await pcs_module.create_dispute_evidence_pack(
        merchant_id="m_stripe",
        dispute_ref="dp_aftercare_1",
        order_id="ORD_AFTERCARE_DISPUTE",
        dispute_payload={
            "id": "dp_aftercare_1",
            "status": "needs_response",
            "metadata": {"merchant_id": "m_stripe", "order_id": "ORD_AFTERCARE_DISPUTE"},
        },
        source="stripe",
        status="draft",
        event_type="charge.dispute.created",
        triggered_by="stripe_webhook:charge.dispute.created",
    )

    assert result == existing_pack


@pytest.mark.asyncio
async def test_payment_aftercare_canary_stripe_dispute_timeline_converges_created_to_funds_withdrawn_to_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    evidence_pack_calls: list[Dict[str, Any]] = []
    dispute_upserts: list[Dict[str, Any]] = []
    events = iter(
        [
            _stripe_event(
                "charge.dispute.created",
                {
                    "id": "dp_aftercare_timeline",
                    "status": "needs_response",
                    "charge": "ch_aftercare_timeline",
                    "metadata": {
                        "merchant_id": "m_stripe",
                        "order_id": "ORD_AFTERCARE_DISPUTE_TIMELINE",
                    },
                },
            ),
            _stripe_event(
                "charge.dispute.funds_withdrawn",
                {
                    "id": "dp_aftercare_timeline",
                    "charge": "ch_aftercare_timeline",
                    "metadata": {
                        "merchant_id": "m_stripe",
                        "order_id": "ORD_AFTERCARE_DISPUTE_TIMELINE",
                    },
                },
            ),
            _stripe_event(
                "charge.dispute.closed",
                {
                    "id": "dp_aftercare_timeline",
                    "status": "won",
                    "charge": "ch_aftercare_timeline",
                    "metadata": {
                        "merchant_id": "m_stripe",
                        "order_id": "ORD_AFTERCARE_DISPUTE_TIMELINE",
                    },
                },
            ),
        ]
    )

    async def fake_upsert_stripe_dispute_record_best_effort(dispute: Dict[str, Any], *, event_type: str, **kwargs: Any) -> None:
        dispute_upserts.append({"event_type": event_type, "dispute": dispute})

    async def fake_create_dispute_evidence_pack(**kwargs: Any) -> None:
        evidence_pack_calls.append(kwargs)

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return next(events)

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

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_created = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_dispute_created"}',
            headers={"stripe-signature": "sig_aftercare_dispute_created"},
        )
        resp_funds = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_dispute_funds_withdrawn"}',
            headers={"stripe-signature": "sig_aftercare_dispute_funds_withdrawn"},
        )
        resp_closed = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_dispute_closed"}',
            headers={"stripe-signature": "sig_aftercare_dispute_closed"},
        )

    assert resp_created.status_code == 200
    assert resp_funds.status_code == 200
    assert resp_closed.status_code == 200
    assert [call["status"] for call in evidence_pack_calls] == ["draft", "frozen", "frozen"]
    assert [call["event_type"] for call in evidence_pack_calls] == [
        "charge.dispute.created",
        "charge.dispute.funds_withdrawn",
        "charge.dispute.closed",
    ]
    assert [call["triggered_by"] for call in evidence_pack_calls] == [
        "stripe_webhook:charge.dispute.created",
        "stripe_webhook:charge.dispute.funds_withdrawn",
        "stripe_webhook:charge.dispute.closed",
    ]
    assert [call["event_type"] for call in dispute_upserts] == [
        "charge.dispute.created",
        "charge.dispute.funds_withdrawn",
        "charge.dispute.closed",
    ]


@pytest.mark.asyncio
async def test_payment_aftercare_canary_stripe_dispute_timeline_converges_warning_needs_response_to_warning_closed_to_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    evidence_pack_calls: list[Dict[str, Any]] = []
    dispute_upserts: list[Dict[str, Any]] = []
    events = iter(
        [
            _stripe_event(
                "charge.dispute.updated",
                {
                    "id": "dp_aftercare_warning_timeline",
                    "status": "warning_needs_response",
                    "charge": "ch_aftercare_warning_timeline",
                    "metadata": {
                        "merchant_id": "m_stripe",
                        "order_id": "ORD_AFTERCARE_WARNING_TIMELINE",
                    },
                },
            ),
            _stripe_event(
                "charge.dispute.updated",
                {
                    "id": "dp_aftercare_warning_timeline",
                    "status": "warning_closed",
                    "charge": "ch_aftercare_warning_timeline",
                    "metadata": {
                        "merchant_id": "m_stripe",
                        "order_id": "ORD_AFTERCARE_WARNING_TIMELINE",
                    },
                },
            ),
            _stripe_event(
                "charge.dispute.closed",
                {
                    "id": "dp_aftercare_warning_timeline",
                    "status": "lost",
                    "charge": "ch_aftercare_warning_timeline",
                    "metadata": {
                        "merchant_id": "m_stripe",
                        "order_id": "ORD_AFTERCARE_WARNING_TIMELINE",
                    },
                },
            ),
        ]
    )

    async def fake_upsert_stripe_dispute_record_best_effort(dispute: Dict[str, Any], *, event_type: str, **kwargs: Any) -> None:
        dispute_upserts.append({"event_type": event_type, "dispute": dispute})

    async def fake_create_dispute_evidence_pack(**kwargs: Any) -> None:
        evidence_pack_calls.append(kwargs)

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return next(events)

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

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_warning = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_dispute_warning_needs_response"}',
            headers={"stripe-signature": "sig_aftercare_dispute_warning_needs_response"},
        )
        resp_warning_closed = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_dispute_warning_closed"}',
            headers={"stripe-signature": "sig_aftercare_dispute_warning_closed"},
        )
        resp_closed = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_dispute_closed_lost"}',
            headers={"stripe-signature": "sig_aftercare_dispute_closed_lost"},
        )

    assert resp_warning.status_code == 200
    assert resp_warning_closed.status_code == 200
    assert resp_closed.status_code == 200
    assert [call["status"] for call in evidence_pack_calls] == ["draft", "frozen", "frozen"]
    assert [call["event_type"] for call in evidence_pack_calls] == [
        "charge.dispute.updated",
        "charge.dispute.updated",
        "charge.dispute.closed",
    ]
    assert [call["triggered_by"] for call in evidence_pack_calls] == [
        "stripe_webhook:charge.dispute.updated",
        "stripe_webhook:charge.dispute.updated",
        "stripe_webhook:charge.dispute.closed",
    ]
    assert [call["event_type"] for call in dispute_upserts] == [
        "charge.dispute.updated",
        "charge.dispute.updated",
        "charge.dispute.closed",
    ]


@pytest.mark.asyncio
async def test_payment_aftercare_canary_stripe_dispute_evidence_submission_then_issuer_review_then_closed_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.webhook_routes as webhook_routes_module
    import services.dispute_records_service as dispute_records_module
    import services.pcs_evidence_pack_service as pcs_module

    evidence_pack_calls: list[Dict[str, Any]] = []
    dispute_upserts: list[Dict[str, Any]] = []
    events = iter(
        [
            _stripe_event(
                "charge.dispute.updated",
                {
                    "id": "dp_aftercare_evidence_timeline",
                    "status": "needs_response",
                    "charge": "ch_aftercare_evidence_timeline",
                    "evidence_details": {"has_evidence": True, "submission_count": 1},
                    "metadata": {
                        "merchant_id": "m_stripe",
                        "order_id": "ORD_AFTERCARE_EVIDENCE_TIMELINE",
                    },
                },
            ),
            _stripe_event(
                "charge.dispute.updated",
                {
                    "id": "dp_aftercare_evidence_timeline",
                    "status": "warning_under_review",
                    "charge": "ch_aftercare_evidence_timeline",
                    "evidence_details": {"has_evidence": True, "submission_count": 1},
                    "metadata": {
                        "merchant_id": "m_stripe",
                        "order_id": "ORD_AFTERCARE_EVIDENCE_TIMELINE",
                    },
                },
            ),
            _stripe_event(
                "charge.dispute.closed",
                {
                    "id": "dp_aftercare_evidence_timeline",
                    "status": "lost",
                    "charge": "ch_aftercare_evidence_timeline",
                    "evidence_details": {"has_evidence": True, "submission_count": 1},
                    "metadata": {
                        "merchant_id": "m_stripe",
                        "order_id": "ORD_AFTERCARE_EVIDENCE_TIMELINE",
                    },
                },
            ),
        ]
    )

    async def fake_upsert_stripe_dispute_record_best_effort(dispute: Dict[str, Any], *, event_type: str, **kwargs: Any) -> None:
        dispute_upserts.append({"event_type": event_type, "dispute": dispute})

    async def fake_create_dispute_evidence_pack(**kwargs: Any) -> None:
        evidence_pack_calls.append(kwargs)

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return next(events)

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

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_submitted = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_dispute_submitted"}',
            headers={"stripe-signature": "sig_aftercare_dispute_submitted"},
        )
        resp_review = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_dispute_review"}',
            headers={"stripe-signature": "sig_aftercare_dispute_review"},
        )
        resp_closed = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_dispute_closed_lost_2"}',
            headers={"stripe-signature": "sig_aftercare_dispute_closed_lost_2"},
        )

    assert resp_submitted.status_code == 200
    assert resp_review.status_code == 200
    assert resp_closed.status_code == 200
    assert [call["source"] for call in evidence_pack_calls] == ["stripe", "stripe", "stripe"]
    assert [call["status"] for call in evidence_pack_calls] == ["draft", "frozen", "frozen"]
    assert [call["event_type"] for call in evidence_pack_calls] == [
        "charge.dispute.updated",
        "charge.dispute.updated",
        "charge.dispute.closed",
    ]
    assert [call["event_type"] for call in dispute_upserts] == [
        "charge.dispute.updated",
        "charge.dispute.updated",
        "charge.dispute.closed",
    ]


@pytest.mark.asyncio
async def test_payment_aftercare_canary_adyen_capture_failed_then_authorisation_replay_does_not_restore_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.psp_routes as psp_routes_module

    state: Dict[str, Any] = {
        "order_id": "ORD_AFTERCARE_ADYEN_CAPTURE",
        "merchant_id": "m_adyen",
        "status": "paid",
        "payment_status": "paid",
        "metadata": {},
    }
    webhook_calls: list[tuple[str, str, str, str]] = []
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []
    mark_paid_calls: list[str] = []
    secret = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

    capture_failed_payload = _adyen_payload(
        state["order_id"],
        psp_reference="PSP_AFTERCARE_CAPTURE_FAILED",
        original_reference="PSP_AFTERCARE_AUTH_ORIG",
        success="true",
        event_code="CAPTURE_FAILED",
        amount_value=2000,
        amount_currency="USD",
    )
    capture_notification = capture_failed_payload["notificationItems"][0]["NotificationRequestItem"]
    capture_notification["reason"] = "Capture failed after authorisation"
    capture_notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(capture_notification, secret)

    authorisation_payload = _adyen_payload(
        state["order_id"],
        psp_reference="PSP_AFTERCARE_AUTH_SUCCESS_REPLAY",
        success="true",
        event_code="AUTHORISATION",
        amount_value=2000,
        amount_currency="USD",
    )
    auth_notification = authorisation_payload["notificationItems"][0]["NotificationRequestItem"]
    auth_notification["additionalData"]["hmacSignature"] = _adyen_hmac_signature(auth_notification, secret)

    async def fake_handle_psp_webhook(payment_intent_id: str, status: str, psp: str, psp_txn_id: str):
        webhook_calls.append((payment_intent_id, status, psp, psp_txn_id))
        return payment_intent_id

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        assert order_id == state["order_id"]
        return dict(state)

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        assert order_id == state["order_id"]
        state["status"] = status
        state.update(kwargs)
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_mark_order_paid(order_id: str) -> None:
        mark_paid_calls.append(order_id)
        state["status"] = "paid"
        state["payment_status"] = "paid"

    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_username", "adyen_user", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_password", "adyen_pass", raising=False)
    monkeypatch.setattr(psp_routes_module.settings, "adyen_webhook_secret", secret, raising=False)
    monkeypatch.setattr(psp_routes_module, "handle_psp_webhook", fake_handle_psp_webhook)
    monkeypatch.setattr(psp_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(psp_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(psp_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(psp_routes_module, "mark_order_paid", fake_mark_order_paid)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_capture_failed = await client.post(
            "/psp/webhook/adyen",
            json=capture_failed_payload,
            auth=("adyen_user", "adyen_pass"),
        )
        resp_authorisation_replay = await client.post(
            "/psp/webhook/adyen",
            json=authorisation_payload,
            auth=("adyen_user", "adyen_pass"),
        )

    assert resp_capture_failed.status_code == 200
    assert resp_authorisation_replay.status_code == 200
    assert status_updates == [
        {
            "order_id": "ORD_AFTERCARE_ADYEN_CAPTURE",
            "status": "payment_failed",
            "payment_status": "payment_failed",
            "metadata": {
                "adyen_last_capture_failed": {
                    "psp_reference": "PSP_AFTERCARE_CAPTURE_FAILED",
                    "original_reference": "PSP_AFTERCARE_AUTH_ORIG",
                    "reason": "Capture failed after authorisation",
                    "received_at": status_updates[0]["metadata"]["adyen_last_capture_failed"]["received_at"],
                }
            },
        }
    ]
    assert mark_paid_calls == []
    assert state["status"] == "payment_failed"
    assert state["payment_status"] == "payment_failed"
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "capture_failed_webhook"
    assert webhook_calls == [
        (state["order_id"], "failed", "adyen", "PSP_AFTERCARE_CAPTURE_FAILED"),
        (state["order_id"], "succeeded", "adyen", "PSP_AFTERCARE_AUTH_SUCCESS_REPLAY"),
    ]


@pytest.mark.asyncio
async def test_payment_aftercare_canary_stripe_refund_then_payment_success_replay_does_not_restore_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    state: Dict[str, Any] = {
        "order_id": "ORD_AFTERCARE_STRIPE_REPLAY",
        "merchant_id": "m_stripe",
        "payment_intent_id": "pi_aftercare_stripe_replay",
        "total": "20.00",
        "total_refunded": "0.00",
        "currency": "USD",
        "status": "awaiting_payment",
        "payment_status": "awaiting_payment",
        "metadata": {},
    }
    status_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []
    paid_calls: list[str] = []
    events = iter(
        [
            _stripe_event(
                "charge.refunded",
                {
                    "id": "ch_aftercare_refunded_replay",
                    "payment_intent": "pi_aftercare_stripe_replay",
                    "amount_refunded": 1200,
                    "currency": "usd",
                },
            ),
            _stripe_event(
                "payment_intent.succeeded",
                {
                    "id": "pi_aftercare_stripe_replay",
                    "amount": 2000,
                    "currency": "usd",
                },
            ),
        ]
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
        if values:
            assert values["payment_intent_id"] == "pi_aftercare_stripe_replay"
        return dict(state)

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        assert order_id == state["order_id"]
        state["status"] = status
        state.update(kwargs)
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs: Any) -> None:
        order_events.append(kwargs)

    async def fake_mark_order_paid(order_id: str) -> None:
        paid_calls.append(order_id)
        state["status"] = "paid"
        state["payment_status"] = "paid"

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return next(events)

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fake_mark_order_paid)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_refund = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_charge_refunded_replay"}',
            headers={"stripe-signature": "sig_charge_refunded_replay"},
        )
        resp_success_replay = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_payment_success_replay"}',
            headers={"stripe-signature": "sig_payment_success_replay"},
        )

    assert resp_refund.status_code == 200
    assert resp_success_replay.status_code == 200
    assert len(status_updates) == 1
    assert status_updates[0]["status"] == "partially_refunded"
    assert str(status_updates[0]["total_refunded"]) == "12"
    assert state["status"] == "partially_refunded"
    assert state["payment_status"] == "partially_refunded"
    assert str(state["total_refunded"]) == "12"
    assert paid_calls == []
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_processed_webhook"


@pytest.mark.asyncio
async def test_payment_aftercare_canary_stripe_payment_failed_then_succeeded_recovers_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as database_module
    import routes.merchant_onboarding_routes as merchant_onboarding_module
    import routes.order_routes as order_routes_module
    import routes.webhook_routes as webhook_routes_module

    state: Dict[str, Any] = {
        "order_id": "ORD_AFTERCARE_STRIPE_RECOVERY",
        "merchant_id": "m_stripe",
        "payment_intent_id": "pi_aftercare_stripe_recovery",
        "status": "awaiting_payment",
        "payment_status": "awaiting_payment",
        # Recovery succeeded event carries amount 4520 minor / usd 100 = 45.20;
        # must match the order total for the integrity guard to finalize.
        "total": "45.20",
        "currency": "usd",
        "total_refunded": "0.00",
    }
    status_updates: list[tuple[str, str]] = []
    paid_calls: list[str] = []
    order_events: list[Dict[str, Any]] = []
    evidence_calls: list[tuple[str, str]] = []
    shopify_calls: list[str] = []
    events = iter(
        [
            _stripe_event(
                "payment_intent.payment_failed",
                {
                    "id": "pi_aftercare_stripe_recovery",
                    "last_payment_error": {"message": "3DS failed"},
                },
            ),
            _stripe_event(
                "payment_intent.succeeded",
                {
                    "id": "pi_aftercare_stripe_recovery",
                    "amount": 4520,
                    "currency": "usd",
                },
            ),
        ]
    )

    async def fake_fetch_one(query: str, values: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
        if values:
            assert values["payment_intent_id"] == "pi_aftercare_stripe_recovery"
        return dict(state)

    async def fake_update_order_status(order_id: str, status: str) -> None:
        assert order_id == state["order_id"]
        state["status"] = status
        state["payment_status"] = status
        status_updates.append((order_id, status))

    async def fake_mark_order_paid(order_id: str) -> bool:
        assert order_id == state["order_id"]
        state["status"] = "paid"
        state["payment_status"] = "paid"
        paid_calls.append(order_id)
        return True

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

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return next(events)

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fake_update_order_status)
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

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_failed = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_payment_failed"}',
            headers={"stripe-signature": "sig_aftercare_payment_failed"},
        )
        resp_succeeded = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_aftercare_payment_succeeded"}',
            headers={"stripe-signature": "sig_aftercare_payment_succeeded"},
        )

    assert resp_failed.status_code == 200
    assert resp_succeeded.status_code == 200
    assert status_updates == [(state["order_id"], "payment_failed")]
    assert paid_calls == [state["order_id"]]
    assert evidence_calls == [(state["order_id"], "stripe_webhook")]
    assert shopify_calls == [state["order_id"]]
    assert state["status"] == "paid"
    assert state["payment_status"] == "paid"
    assert [event["event_type"] for event in order_events] == [
        "payment_failed_webhook",
        "payment_confirmed_webhook",
    ]
