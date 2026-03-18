from __future__ import annotations

from datetime import datetime, timezone

import pytest

from readiness.models import CheckoutSessionRecord, OrderSyncEventRecord
from readiness.sync_audit import build_order_sync_audit_snapshot


class FakeDB:
    def __init__(self, *, order_events=None, webhook_events=None, refund_records=None, return_records=None):
        self.order_events = order_events or []
        self.webhook_events = webhook_events or []
        self.refund_records = refund_records or []
        self.return_records = return_records or []

    async def fetch_all(self, query, values):
        sql = str(query)
        if "FROM order_events" in sql:
            return self.order_events
        if "FROM pcs_shopify_webhook_events" in sql:
            return self.webhook_events
        if "FROM refund_records" in sql:
            return self.refund_records
        if "FROM return_records" in sql:
            return self.return_records
        return []


@pytest.mark.asyncio
async def test_build_order_sync_audit_snapshot_reports_ready_writeback_and_pending_tail():
    checkout = CheckoutSessionRecord(
        checkout_id="rdchk_alpha_1",
        merchant_id="merch_1",
        channel="ucp",
        variant_id="431",
        quantity=1,
        payment_mode="merchant_native_alpha",
        status="state_synced",
        order_id="ORD_ALPHA_1",
        session_payload={
            "merchant_alpha_mode": "real_merchant_alpha",
            "source_of_truth": {"order_status": "readiness.order_sync.v2"},
        },
    )
    readiness_events = [
        OrderSyncEventRecord(checkout_id="rdchk_alpha_1", event_type="order_created", created_at="2026-03-18T00:00:00Z"),
        OrderSyncEventRecord(checkout_id="rdchk_alpha_1", event_type="order_forwarded_to_merchant", created_at="2026-03-18T00:01:00Z"),
        OrderSyncEventRecord(checkout_id="rdchk_alpha_1", event_type="state_synced", created_at="2026-03-18T00:02:00Z"),
    ]

    async def fake_get_order(_order_id: str):
        return {
            "order_id": "ORD_ALPHA_1",
            "status": "pending",
            "payment_status": "unpaid",
            "fulfillment_status": "processing",
            "shopify_order_id": "9001002003",
            "total_refunded": 0,
            "updated_at": datetime(2026, 3, 18, 0, 3, tzinfo=timezone.utc),
        }

    db = FakeDB(
        order_events=[
            {
                "event_type": "order_updated_webhook",
                "status": None,
                "error_message": None,
                "metadata": {"financial_status": "pending"},
                "created_at": datetime(2026, 3, 18, 0, 4, tzinfo=timezone.utc),
            }
        ],
        webhook_events=[
            {
                "topic": "orders/updated",
                "webhook_id": "wh_1",
                "signature_verified": True,
                "received_at": datetime(2026, 3, 18, 0, 5, tzinfo=timezone.utc),
                "occurred_at": datetime(2026, 3, 18, 0, 5, tzinfo=timezone.utc),
            }
        ],
    )

    audit = await build_order_sync_audit_snapshot(
        merchant_id="merch_1",
        checkout=checkout,
        readiness_events=readiness_events,
        get_order_fn=fake_get_order,
        db=db,
        sample_limit=5,
    )

    assert audit["shopify_order_id"] == "9001002003"
    assert audit["sync_signals"]["merchant_writeback"]["status"] == "ready"
    assert audit["sync_signals"]["webhook_ingest"]["status"] == "ready"
    assert audit["sync_signals"]["refund_sync"]["status"] == "not_eligible"
    assert audit["sync_signals"]["refund_sync"]["refund_eligible"] is False
    assert audit["sync_signals"]["refund_sync"]["eligibility_reason"] == "order_not_paid"
    assert audit["sync_signals"]["refund_transaction_mirror"]["status"] == "not_applicable"
    assert audit["sync_signals"]["return_sync"]["status"] == "not_observed"
    assert audit["evidence"]["sample_limit"] == 5


@pytest.mark.asyncio
async def test_build_order_sync_audit_snapshot_reports_refund_and_return_observation():
    checkout = CheckoutSessionRecord(
        checkout_id="rdchk_alpha_2",
        merchant_id="merch_1",
        channel="ucp",
        variant_id="431",
        quantity=1,
        payment_mode="merchant_native_alpha",
        status="state_synced",
        order_id="ORD_ALPHA_2",
        session_payload={"merchant_alpha_mode": "real_merchant_alpha"},
    )

    async def fake_get_order(_order_id: str):
        return {
            "order_id": "ORD_ALPHA_2",
            "status": "cancelled",
            "payment_status": "refunded",
            "fulfillment_status": "processing",
            "shopify_order_id": "9001002004",
            "total_refunded": 42.5,
        }

    db = FakeDB(
        order_events=[{"event_type": "order_cancelled_webhook", "created_at": "2026-03-18T00:10:00Z"}],
        webhook_events=[
            {"topic": "orders/cancelled", "signature_verified": True, "received_at": "2026-03-18T00:11:00Z"},
            {"topic": "refunds/create", "signature_verified": True, "received_at": "2026-03-18T00:12:00Z"},
            {"topic": "returns/requested", "signature_verified": True, "received_at": "2026-03-18T00:13:00Z"},
        ],
        refund_records=[
            {"refund_id": "REF_1", "amount": 42.5, "currency": "USD", "status": "completed", "created_at": "2026-03-18T00:12:30Z"}
        ],
        return_records=[
            {"source_return_id": "ret_1", "status": "open", "updated_at": "2026-03-18T00:13:30Z"}
        ],
    )

    audit = await build_order_sync_audit_snapshot(
        merchant_id="merch_1",
        checkout=checkout,
        readiness_events=[],
        get_order_fn=fake_get_order,
        db=db,
        sample_limit=3,
    )

    assert audit["sync_signals"]["cancellation_sync"]["status"] == "ready"
    assert audit["sync_signals"]["refund_sync"]["status"] == "ready"
    assert audit["sync_signals"]["refund_transaction_mirror"]["status"] == "not_observed"
    assert audit["sync_signals"]["return_sync"]["status"] == "ready"
    assert audit["sync_signals"]["refund_sync"]["refund_record_count"] == 1
    assert audit["sync_signals"]["refund_sync"]["successful_refund_record_count"] == 1
    assert audit["sync_signals"]["refund_sync"]["failed_refund_record_count"] == 0
    assert audit["sync_signals"]["return_sync"]["return_record_count"] == 1


@pytest.mark.asyncio
async def test_build_order_sync_audit_snapshot_warns_when_checkout_state_lags_cancelled_order():
    checkout = CheckoutSessionRecord(
        checkout_id="rdchk_alpha_3",
        merchant_id="merch_1",
        channel="ucp",
        variant_id="431",
        quantity=1,
        payment_mode="merchant_native_alpha",
        status="state_synced",
        order_id="ORD_ALPHA_3",
        session_payload={"merchant_alpha_mode": "real_merchant_alpha"},
    )

    async def fake_get_order(_order_id: str):
        return {
            "order_id": "ORD_ALPHA_3",
            "status": "cancelled",
            "payment_status": "unpaid",
            "shopify_order_id": "9001002005",
            "total_refunded": 0,
        }

    db = FakeDB(
        webhook_events=[
            {"topic": "orders/cancelled", "signature_verified": True, "received_at": "2026-03-18T00:20:00Z"}
        ]
    )

    audit = await build_order_sync_audit_snapshot(
        merchant_id="merch_1",
        checkout=checkout,
        readiness_events=[],
        get_order_fn=fake_get_order,
        db=db,
        sample_limit=3,
    )

    assert "readiness_checkout_state_lags_order_state" in audit["warnings"]


@pytest.mark.asyncio
async def test_build_order_sync_audit_snapshot_marks_paid_order_refund_eligible_before_refund_observed():
    checkout = CheckoutSessionRecord(
        checkout_id="rdchk_alpha_4",
        merchant_id="merch_1",
        channel="ucp",
        variant_id="431",
        quantity=1,
        payment_mode="merchant_native_alpha",
        status="state_synced",
        order_id="ORD_ALPHA_4",
        session_payload={"merchant_alpha_mode": "real_merchant_alpha"},
    )

    async def fake_get_order(_order_id: str):
        return {
            "order_id": "ORD_ALPHA_4",
            "status": "paid",
            "payment_status": "paid",
            "payment_intent_id": "pi_alpha_live_1",
            "psp_used": "stripe",
            "shopify_order_id": "9001002006",
            "total_refunded": 0,
        }

    db = FakeDB()

    audit = await build_order_sync_audit_snapshot(
        merchant_id="merch_1",
        checkout=checkout,
        readiness_events=[],
        get_order_fn=fake_get_order,
        db=db,
        sample_limit=3,
    )

    assert audit["sync_signals"]["refund_sync"]["status"] == "not_observed"
    assert audit["sync_signals"]["refund_sync"]["refund_eligible"] is True
    assert audit["sync_signals"]["refund_sync"]["eligibility_reason"] is None
    assert audit["sync_signals"]["refund_transaction_mirror"]["status"] == "not_applicable"


@pytest.mark.asyncio
async def test_build_order_sync_audit_snapshot_surfaces_soft_skipped_refund_transaction_mirror():
    checkout = CheckoutSessionRecord(
        checkout_id="rdchk_alpha_5",
        merchant_id="merch_1",
        channel="ucp",
        variant_id="431",
        quantity=1,
        payment_mode="merchant_native_alpha",
        status="refunded",
        order_id="ORD_ALPHA_5",
        session_payload={"merchant_alpha_mode": "real_merchant_alpha"},
    )

    async def fake_get_order(_order_id: str):
        return {
            "order_id": "ORD_ALPHA_5",
            "status": "refunded",
            "payment_status": "refunded",
            "payment_intent_id": "pi_alpha_refund_softskip",
            "psp_used": "stripe",
            "shopify_order_id": "9001002007",
            "total_refunded": 29.0,
        }

    db = FakeDB(
        order_events=[
            {
                "event_type": "readiness_refund_transaction_sync",
                "status": "soft_skipped",
                "metadata": {
                    "psp_refund_id": "re_alpha_softskip",
                    "platform_refund_id": "re_alpha_softskip",
                    "transaction_sync": {
                        "ok": False,
                        "soft_skipped": True,
                        "reason": "missing_parent_transaction",
                    },
                },
                "created_at": "2026-03-18T00:30:00Z",
            }
        ],
        refund_records=[
            {
                "refund_id": "REF_ALPHA_5",
                "amount": 29.0,
                "currency": "USD",
                "status": "completed",
                "platform_refund_id": "re_alpha_softskip",
                "created_at": "2026-03-18T00:29:00Z",
            }
        ],
    )

    audit = await build_order_sync_audit_snapshot(
        merchant_id="merch_1",
        checkout=checkout,
        readiness_events=[],
        get_order_fn=fake_get_order,
        db=db,
        sample_limit=3,
    )

    assert audit["sync_signals"]["refund_sync"]["status"] == "ready"
    assert audit["sync_signals"]["refund_transaction_mirror"]["status"] == "soft_skipped"
    assert audit["sync_signals"]["refund_transaction_mirror"]["reason"] == "missing_parent_transaction"
    assert audit["sync_signals"]["refund_transaction_mirror"]["soft_skipped"] is True
    assert audit["sync_signals"]["refund_transaction_mirror"]["platform_refund_id"] == "re_alpha_softskip"
    assert "shopify_refund_transaction_mirror_degraded" in audit["warnings"]


@pytest.mark.asyncio
async def test_build_order_sync_audit_snapshot_marks_failed_canonical_refund_as_failed():
    checkout = CheckoutSessionRecord(
        checkout_id="rdchk_alpha_6",
        merchant_id="merch_1",
        channel="ucp",
        variant_id="431",
        quantity=1,
        payment_mode="merchant_native_alpha",
        status="state_synced",
        order_id="ORD_ALPHA_6",
        session_payload={"merchant_alpha_mode": "real_merchant_alpha"},
    )

    async def fake_get_order(_order_id: str):
        return {
            "order_id": "ORD_ALPHA_6",
            "status": "paid",
            "payment_status": "paid",
            "payment_intent_id": "pi_alpha_refund_failed",
            "psp_used": "stripe",
            "shopify_order_id": "9001002008",
            "total_refunded": 0,
        }

    db = FakeDB(
        order_events=[
            {
                "event_type": "readiness_refund_transaction_sync",
                "status": "failed",
                "metadata": {
                    "transaction_sync": {
                        "ok": False,
                        "skipped": True,
                        "reason": "refund_not_completed",
                    }
                },
                "created_at": "2026-03-18T00:31:00Z",
            }
        ],
        refund_records=[
            {
                "refund_id": "REF_ALPHA_6",
                "amount": 29.0,
                "currency": "EUR",
                "status": "failed",
                "error_message": "Invalid API Key provided",
                "created_at": "2026-03-18T00:30:30Z",
            }
        ],
    )

    audit = await build_order_sync_audit_snapshot(
        merchant_id="merch_1",
        checkout=checkout,
        readiness_events=[],
        get_order_fn=fake_get_order,
        db=db,
        sample_limit=3,
    )

    assert audit["sync_signals"]["refund_sync"]["status"] == "failed"
    assert audit["sync_signals"]["refund_sync"]["successful_refund_record_count"] == 0
    assert audit["sync_signals"]["refund_sync"]["failed_refund_record_count"] == 1
    assert audit["sync_signals"]["refund_sync"]["latest_refund_record_status"] == "failed"
    assert audit["sync_signals"]["refund_sync"]["latest_error_message"] == "Invalid API Key provided"
    assert audit["sync_signals"]["refund_transaction_mirror"]["status"] == "failed"
    assert "canonical_refund_failed" in audit["warnings"]
