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
    assert audit["sync_signals"]["refund_sync"]["status"] == "not_observed"
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
    assert audit["sync_signals"]["return_sync"]["status"] == "ready"
    assert audit["sync_signals"]["refund_sync"]["refund_record_count"] == 1
    assert audit["sync_signals"]["return_sync"]["return_record_count"] == 1
