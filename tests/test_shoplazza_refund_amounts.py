"""Shoplazza refund amounts: the cumulative total, turned into per-delivery money.

WHAT THE PLATFORM ACTUALLY SENDS (verified against the 202601 order-webhook
schema, see docs/SHOPLINE_SHOPLAZZA_ADAPTERS.md): the body of
`orders/partially_refunded` and `orders/refunded` is the ORDER resource. It has
no `refunds[]` array, no refund record id, and no per-refund timestamp. The only
non-deprecated refund magnitude on it is `total_refund_price`, "Total refund
amount that has been successfully processed" — CUMULATIVE across every refund of
that order.

So the amount of one delivery is that total minus what the receiver has already
recorded, and every test below is about that subtraction: what it emits, what it
refuses to emit, and what it keys the result on.
"""

from __future__ import annotations

import pytest


MERCHANT_ID = "merchant-sz"
STORE_ID = "store-sz"
ORDER_ID = "sz-order-1"


def _order(**overrides):
    """A Shoplazza refund delivery's order, with only documented fields."""
    order = {
        "id": ORDER_ID,
        "created_at": "2026-08-27T11:00:00Z",
        "updated_at": "2026-08-27T13:00:00Z",
        "currency": "USD",
        "total_price": "40.00",
        "real_total_paid": "40.00",
        "financial_status": "partially_refunded",
        "total_refund_price": "10.00",
        "landing_site": "https://demo.myshoplaza.com/?pivota_click_id=clk_12345678",
        "customer": {"id": "buyer-2", "email": "private@example.com"},
        "shipping_address": {"name": "Private", "phone": "555-0199"},
        "line_items": [
            {
                "id": "line-2",
                "product_id": "product-2",
                "variant_id": "variant-2",
                "sku": "SKU-2",
                "quantity": 1,
                "total": "40.00",
                "product_title": "Do not persist",
                "custom_properties": {"message": "private"},
            }
        ],
        "payment_line": {
            "id": "payment-line-2",
            "transaction_no": "transaction-2",
            "credit_card_number": "4242",
            "merchant_email": "merchant@example.com",
        },
    }
    order.update(overrides)
    return order


def _map(previously=None, topic="orders/partially_refunded", delivery_id="delivery-1", **order_overrides):
    from services.shopline_family_event_adapter import map_shoplazza_webhook

    return map_shoplazza_webhook(
        {"order": _order(**order_overrides)},
        topic=topic,
        delivery_id=delivery_id,
        store_id=STORE_ID,
        previously_recorded_refund_cents=previously,
    )


# ---------------------------------------------------------------- emitting ---


def test_first_observation_of_the_cumulative_total_is_entirely_new_money():
    event = _map(previously=None, total_refund_price="10.00").events[0]

    assert event.event_type == "refund.succeeded"
    assert event.amount_cents == 1000
    assert event.currency == "USD"
    assert event.refund_id == f"{ORDER_ID}:1000"
    assert event.order_id == ORDER_ID
    assert event.order_ref == f"shoplazza:{ORDER_ID}"
    assert event.payment_id == "transaction-2"
    assert event.metadata["native_cumulative_refund_total"] == "10.00"
    assert event.metadata["native_amount_semantics"] == "cumulative_refund_total_delta"


def test_a_second_partial_refund_records_the_delta_not_the_cumulative_total():
    """The whole point. 10.00 then 25.00 is 10.00 + 15.00, never 10.00 + 25.00."""
    event = _map(previously=1000, total_refund_price="25.00").events[0]

    assert event.amount_cents == 1500
    # Keyed on the CUMULATIVE figure, so the two deltas are two distinct rows
    # the funnel sums, and a redelivery of this same total collides with it.
    assert event.refund_id == f"{ORDER_ID}:2500"
    assert event.metadata["native_cumulative_refund_total"] == "25.00"


def test_a_full_refund_topic_takes_the_same_path():
    event = _map(
        previously=1000,
        topic="orders/refunded",
        total_refund_price="40.00",
        financial_status="refunded",
    ).events[0]

    assert event.amount_cents == 3000
    assert event.refund_id == f"{ORDER_ID}:4000"
    assert event.metadata["native_topic"] == "orders/refunded"


def test_a_zero_decimal_currency_keeps_minor_unit_semantics():
    event = _map(previously=None, currency="JPY", total_refund_price="2500").events[0]

    assert event.amount_cents == 2500
    assert event.currency == "JPY"
    assert event.refund_id == f"{ORDER_ID}:2500"


def test_the_refund_event_carries_no_buyer_or_merchant_pii():
    serialized = _map(previously=None).events[0].model_dump_json()

    for private_value in (
        "private@example.com",
        "merchant@example.com",
        "credit_card_number",
        "555-0199",
        "Do not persist",
    ):
        assert private_value not in serialized


# --------------------------------------------------------------- refusing ---


def test_a_redelivery_of_an_already_recorded_total_emits_nothing():
    from services.shopline_family_event_adapter import UnsupportedShoplineFamilyEvent

    with pytest.raises(UnsupportedShoplineFamilyEvent) as excinfo:
        _map(previously=2500, total_refund_price="25.00")
    assert "refund_not_new" in str(excinfo.value)


def test_a_cumulative_total_below_what_is_recorded_emits_nothing():
    """A correction downwards is not a refund; emitting 0 would shadow the key."""
    from services.shopline_family_event_adapter import UnsupportedShoplineFamilyEvent

    with pytest.raises(UnsupportedShoplineFamilyEvent) as excinfo:
        _map(previously=2500, total_refund_price="5.00")
    assert "refund_not_new" in str(excinfo.value)


def test_a_zero_cumulative_total_emits_nothing():
    from services.shopline_family_event_adapter import UnsupportedShoplineFamilyEvent

    with pytest.raises(UnsupportedShoplineFamilyEvent) as excinfo:
        _map(previously=None, total_refund_price="0.00")
    assert "refund_not_new" in str(excinfo.value)


@pytest.mark.parametrize("absent", [None, ""])
def test_an_absent_cumulative_total_is_ignored_rather_than_rejected(absent):
    """Absence is not a malformed claim.

    Nothing in the delivery says money moved, so there is nothing to record.
    A 4xx here would also be permanent data loss on Shoplazza's side: the
    platform retries only a 5xx, so rejecting an amount-less delivery buys
    noise and no redelivery.
    """
    from services.shopline_family_event_adapter import UnsupportedShoplineFamilyEvent

    with pytest.raises(UnsupportedShoplineFamilyEvent) as excinfo:
        _map(previously=None, total_refund_price=absent)
    assert "refund_total_absent" in str(excinfo.value)


@pytest.mark.parametrize("unreadable", ["not-a-number", "-5.00", {"amount": "5.00"}])
def test_a_present_but_unreadable_cumulative_total_is_rejected(unreadable):
    """Present-and-wrong is a malformed money claim, and must be loud."""
    with pytest.raises(ValueError) as excinfo:
        _map(previously=None, total_refund_price=unreadable)
    assert "total_refund_price" in str(excinfo.value)


def test_a_refund_without_a_currency_is_rejected():
    """An amount with no currency is money the funnel can never count."""
    with pytest.raises(ValueError) as excinfo:
        _map(previously=None, currency="")
    assert "currency" in str(excinfo.value)


def test_a_negative_previously_recorded_figure_is_rejected():
    with pytest.raises(ValueError):
        _map(previously=-1)


def test_a_refund_delivery_with_no_order_id_is_still_rejected():
    from services.shopline_family_event_adapter import map_shoplazza_webhook

    with pytest.raises(ValueError) as excinfo:
        map_shoplazza_webhook(
            {"order": {"currency": "USD", "total_refund_price": "10.00"}},
            topic="orders/refunded",
            delivery_id="delivery-1",
            store_id=STORE_ID,
        )
    assert "order id" in str(excinfo.value)


# ------------------------------------------------------------------- keys ---


def test_the_refund_event_id_is_derived_from_the_refund_key_not_the_delivery_id():
    """Two deliveries of the same cumulative total must be ONE ledger event.

    Before this change the event id was keyed on the delivery id, so a
    Shoplazza retry (a new `X-Shoplazza-Deduplication-ID` is NOT issued for a
    retry, but a re-fire of the same total is a real occurrence) produced a
    second row. The key is now the money, so it cannot.
    """
    first = _map(previously=1000, delivery_id="delivery-a", total_refund_price="25.00").events[0]
    second = _map(previously=1000, delivery_id="delivery-b", total_refund_price="25.00").events[0]

    assert first.event_id == second.event_id
    assert first.refund_id == second.refund_id == f"{ORDER_ID}:2500"
    # The delivery id survives only as diagnostics, never as identity.
    assert first.trace_id == "delivery-a"
    assert second.trace_id == "delivery-b"
    assert "delivery-a" not in first.event_id


def test_two_different_cumulative_totals_produce_two_different_event_ids():
    first = _map(previously=None, total_refund_price="10.00").events[0]
    second = _map(previously=1000, total_refund_price="25.00").events[0]

    assert first.event_id != second.event_id


def test_shoplazza_order_ref_reads_the_ref_the_receiver_needs_before_mapping():
    from services.shopline_family_event_adapter import shoplazza_order_ref

    assert shoplazza_order_ref({"order": _order()}) == f"shoplazza:{ORDER_ID}"
    # Unwrapped bodies are tolerated the same way the mapper tolerates them.
    assert shoplazza_order_ref(_order()) == f"shoplazza:{ORDER_ID}"
    assert shoplazza_order_ref({"order": {}}) is None
    assert shoplazza_order_ref("not an object") is None


def test_the_refund_topics_are_a_subset_of_the_supported_topics():
    """The subscription list must keep offering both refund topics."""
    from services.shopline_family_event_adapter import (
        SHOPLAZZA_REFUND_TOPICS,
        SUPPORTED_SHOPLAZZA_TOPICS,
    )

    assert SHOPLAZZA_REFUND_TOPICS == {"orders/partially_refunded", "orders/refunded"}
    assert SHOPLAZZA_REFUND_TOPICS <= SUPPORTED_SHOPLAZZA_TOPICS


# ------------------------------------------------- the lifecycle is intact ---


@pytest.mark.parametrize(
    ("topic", "event_type"),
    [
        ("orders/create", "order.created"),
        ("orders/paid", "order.paid"),
        ("orders/cancelled", "order.cancelled"),
    ],
)
def test_order_lifecycle_topics_are_untouched_by_the_refund_work(topic, event_type):
    from services.shopline_family_event_adapter import map_shoplazza_webhook

    event = map_shoplazza_webhook(
        {"order": _order(placed_at="2026-08-27T11:01:00Z", canceled_at="2026-08-27T12:00:00Z")},
        topic=topic,
        delivery_id="delivery-lifecycle",
        store_id=STORE_ID,
    ).events[0]

    assert event.event_type == event_type
    assert event.amount_cents == 4000
    assert event.order_id == ORDER_ID
    assert event.refund_id is None
    # Still keyed on the order, and still stable across deliveries.
    assert event.metadata.get("native_amount_semantics") is None
    replay = map_shoplazza_webhook(
        {"order": _order()},
        topic=topic,
        delivery_id="a-completely-different-delivery",
        store_id=STORE_ID,
    ).events[0]
    assert replay.event_id == event.event_id
