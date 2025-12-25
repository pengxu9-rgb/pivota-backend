from __future__ import annotations

from datetime import datetime

from services.pcs_reducer import reduce_order_facts


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_reducer_out_of_order_facts_converge_to_same_state():
    facts = [
        {
            "fact_id": "a",
            "fact_type": "shopify.orders.updated",
            "occurred_at": _ts("2025-01-01T00:00:00Z"),
            "received_at": _ts("2025-01-01T00:00:10Z"),
            "dedupe_key": "shopify:orders:1",
            "payload_json": {"id": 1001, "financial_status": "authorized", "fulfillment_status": None, "currency": "USD", "total_price": "10.00"},
        },
        {
            "fact_id": "b",
            "fact_type": "internal.payment_updated",
            "occurred_at": _ts("2025-01-01T00:00:02Z"),
            "received_at": _ts("2025-01-01T00:00:11Z"),
            "dedupe_key": "internal:payment:1",
            "payload_json": {"status": "succeeded"},
        },
        {
            "fact_id": "c",
            "fact_type": "shopify.fulfillments.update",
            "occurred_at": _ts("2025-01-01T00:00:03Z"),
            "received_at": _ts("2025-01-01T00:00:12Z"),
            "dedupe_key": "shopify:fulfillment:1",
            "payload_json": {"order_id": 1001, "status": "success"},
        },
        # Duplicate (same dedupe_key) should not affect output.
        {
            "fact_id": "dup",
            "fact_type": "shopify.fulfillments.update",
            "occurred_at": _ts("2025-01-01T00:00:03Z"),
            "received_at": _ts("2025-01-01T00:00:13Z"),
            "dedupe_key": "shopify:fulfillment:1",
            "payload_json": {"order_id": 1001, "status": "success"},
        },
        # Late-arriving fact: earlier occurred_at but later received_at.
        {
            "fact_id": "late",
            "fact_type": "shopify.orders.updated",
            "occurred_at": _ts("2025-01-01T00:00:01Z"),
            "received_at": _ts("2025-01-01T00:10:00Z"),
            "dedupe_key": "shopify:orders:late",
            "payload_json": {"id": 1001, "financial_status": "paid"},
        },
    ]

    out1 = reduce_order_facts(facts)
    out2 = reduce_order_facts(list(reversed(facts)))

    assert out1["current_state_json"] == out2["current_state_json"]
    assert out1["current_state_json"]["financial_status"] == "paid"
    assert out1["current_state_json"]["fulfillment_status"] == "fulfilled"

