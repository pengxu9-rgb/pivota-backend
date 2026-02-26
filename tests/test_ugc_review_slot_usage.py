from __future__ import annotations

from services.ugc_capabilities_service import compute_review_slot_usage


def test_compute_review_slot_usage_consumes_bound_orders_and_exposes_remaining_slots() -> None:
    usage = compute_review_slot_usage(
        paid_order_ids=["ord_2", "ord_1"],
        bindings=[
            {"review_id": 101, "order_id": "ord_2"},
        ],
    )

    assert usage["total_paid_orders"] == 2
    assert usage["used_slots"] == 1
    assert usage["available_slots"] == 1
    assert usage["used_order_ids"] == ["ord_2"]
    assert usage["available_order_ids"] == ["ord_1"]


def test_compute_review_slot_usage_legacy_null_binding_consumes_oldest_paid_order() -> None:
    usage = compute_review_slot_usage(
        paid_order_ids=["ord_3", "ord_2", "ord_1"],
        bindings=[
            {"review_id": 201, "order_id": None},
        ],
    )

    # Legacy rows without order_id consume earliest order quota.
    assert usage["legacy_binding_count"] == 1
    assert usage["legacy_consumed_order_ids"] == ["ord_1"]
    assert usage["used_order_ids"] == ["ord_1"]
    assert usage["available_order_ids"] == ["ord_3", "ord_2"]


def test_compute_review_slot_usage_ignores_bound_orders_not_in_paid_set() -> None:
    usage = compute_review_slot_usage(
        paid_order_ids=["ord_2", "ord_1"],
        bindings=[
            {"review_id": 301, "order_id": "ord_external"},
        ],
    )

    assert usage["used_slots"] == 0
    assert usage["available_slots"] == 2
    assert usage["available_order_ids"] == ["ord_2", "ord_1"]
