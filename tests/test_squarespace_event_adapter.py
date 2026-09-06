"""The pure Squarespace mapper: every event, and every reason not to emit one.

The mapper is given a whole order — the same resource the webhook receiver
fetches and the reconciliation sweep lists — plus the refund figure Pivota has
already recorded for it. These tests pin the four canonical events it can
produce, the two things it must refuse to produce (test-mode orders, and a
refund that is not new money), and the identity rules that let the two
ingresses dedupe against each other.
"""

from __future__ import annotations

import pytest

from services.squarespace_event_adapter import (
    UnsupportedSquarespaceEvent,
    is_supported_squarespace_topic,
    map_squarespace_order,
    squarespace_order_currency,
    squarespace_order_ref,
    squarespace_refunded_total_cents,
)

STORE_ID = "store-sq"
ORDER_ID = "5e1f0b6a1c9d440000a1b2c3"
WEBHOOK = "squarespace_webhook"
SWEEP = "squarespace_reconciliation"


def _order(**overrides):
    order = {
        "id": ORDER_ID,
        "orderNumber": "00042",
        "createdOn": "2026-09-01T10:00:00.000Z",
        "modifiedOn": "2026-09-02T11:30:00.000Z",
        "channel": "web",
        "testmode": False,
        "customerEmail": "buyer@example.com",
        "fulfillmentStatus": "PENDING",
        "grandTotal": {"value": "40.00", "currency": "USD"},
        "lineItems": [
            {
                "id": "line-1",
                "productId": "product-1",
                "variantId": "variant-1",
                "sku": "SKU-1",
                "productName": "A Nice Thing",
                "quantity": 2,
                "unitPricePaid": {"value": "20.00", "currency": "USD"},
            }
        ],
        "externalOrderReference": "pivota:ord_forged",
    }
    order.update(overrides)
    return order


def _by_type(batch):
    return {event.event_type: event for event in batch.events}


# ---- the events ------------------------------------------------------------


def test_a_plain_order_produces_created_and_paid():
    batch = map_squarespace_order(
        _order(), store_id=STORE_ID, source=WEBHOOK, topic="order.create"
    )

    events = _by_type(batch)
    assert set(events) == {"order.created", "order.paid"}
    assert events["order.paid"].amount_cents == 4000
    assert events["order.paid"].currency == "USD"
    assert events["order.paid"].metadata["native_amount_semantics"] == "order_grand_total"
    # `order.paid` is anchored at createdOn, not modifiedOn: there is no payment
    # status to read, and the payment is what CAUSED the order to exist. An
    # order edited weeks later must not move its own payment forward with it.
    assert events["order.paid"].occurred_at.isoformat() == "2026-09-01T10:00:00+00:00"
    assert events["order.created"].occurred_at == events["order.paid"].occurred_at
    assert events["order.paid"].order_ref == f"squarespace:{ORDER_ID}"
    assert events["order.paid"].order_id == ORDER_ID


def test_a_canceled_order_adds_order_cancelled_anchored_at_modified_on():
    batch = map_squarespace_order(
        _order(fulfillmentStatus="CANCELED"),
        store_id=STORE_ID,
        source=WEBHOOK,
        topic="order.update",
    )

    events = _by_type(batch)
    assert "order.cancelled" in events
    assert events["order.cancelled"].occurred_at.isoformat() == "2026-09-02T11:30:00+00:00"
    # The paid row is still there: a cancellation after payment does not
    # retract the payment, it is a separate fact and a refund carries the money.
    assert events["order.paid"].amount_cents == 4000


@pytest.mark.parametrize("status", ["PENDING", "FULFILLED", "pending", ""])
def test_a_non_canceled_fulfillment_status_emits_no_cancellation(status):
    batch = map_squarespace_order(
        _order(fulfillmentStatus=status), store_id=STORE_ID, source=WEBHOOK
    )

    assert "order.cancelled" not in _by_type(batch)


def test_a_refunded_order_emits_the_delta_of_the_cumulative_total():
    batch = map_squarespace_order(
        _order(refundedTotal={"value": "25.00", "currency": "USD"}),
        store_id=STORE_ID,
        source=WEBHOOK,
        previously_recorded_refund_cents=1000,
    )

    refund = _by_type(batch)["refund.succeeded"]
    assert refund.amount_cents == 1500
    assert refund.refund_id == f"{ORDER_ID}:2500"
    assert refund.currency == "USD"
    assert refund.metadata["native_cumulative_refund_total"] == "25.00"
    assert refund.metadata["native_amount_semantics"] == "cumulative_refund_total_delta"
    # No per-refund timestamp exists anywhere in the Orders API; modifiedOn is
    # the closest anchor there is.
    assert refund.occurred_at.isoformat() == "2026-09-02T11:30:00+00:00"


# ---- the refusals ----------------------------------------------------------


def test_a_testmode_order_is_ignored_entirely():
    """Not "mapped without the money" — not mapped at all.

    A test-mode order was never paid for. Emitting even `order.created` for it
    would occupy that order's deterministic keys, and a later real order can
    never have the same id, so the only effect is a fabricated funnel row.
    """
    with pytest.raises(UnsupportedSquarespaceEvent) as exc:
        map_squarespace_order(
            _order(testmode=True), store_id=STORE_ID, source=WEBHOOK, topic="order.create"
        )

    assert "testmode" in str(exc.value)


@pytest.mark.parametrize(
    "cumulative, previously",
    [
        # A redelivery, or the sweep seeing an order the webhook already did.
        ("10.00", 1000),
        # A merchant-side downward correction.
        ("10.00", 2500),
    ],
)
def test_a_cumulative_total_that_is_not_new_emits_no_refund(cumulative, previously):
    """A zero-amount row under `<order>:<total>` would take the key for good.

    The ledger is first-write-wins on the event id, so a zero written now
    permanently shadows the real refund that arrives under the same key later.
    """
    batch = map_squarespace_order(
        _order(refundedTotal={"value": cumulative, "currency": "USD"}),
        store_id=STORE_ID,
        source=WEBHOOK,
        previously_recorded_refund_cents=previously,
    )

    assert "refund.succeeded" not in _by_type(batch)
    # ...and the rest of the order is still recorded.
    assert "order.paid" in _by_type(batch)


def test_a_downward_correction_then_a_genuine_refund_records_only_the_new_money():
    """The documented consequence of never trusting the total to be monotonic.

    After 25.00 is corrected down to 20.00 (recorded: nothing) the next genuine
    refund to 30.00 emits 500, not 1000. The running total still lands on 3000,
    so aggregate refunded GMV is right; only that one per-event delta is short.
    """
    corrected = map_squarespace_order(
        _order(refundedTotal={"value": "20.00", "currency": "USD"}),
        store_id=STORE_ID,
        source=WEBHOOK,
        previously_recorded_refund_cents=2500,
    )
    assert "refund.succeeded" not in _by_type(corrected)

    later = map_squarespace_order(
        _order(refundedTotal={"value": "30.00", "currency": "USD"}),
        store_id=STORE_ID,
        source=WEBHOOK,
        previously_recorded_refund_cents=2500,
    )
    assert _by_type(later)["refund.succeeded"].amount_cents == 500


def test_a_zero_grand_total_emits_no_paid_row():
    """Same first-write-wins hazard, on the paid key rather than the refund key."""
    batch = map_squarespace_order(
        _order(grandTotal={"value": "0.00", "currency": "USD"}),
        store_id=STORE_ID,
        source=WEBHOOK,
    )

    assert "order.paid" not in _by_type(batch)
    assert "order.created" in _by_type(batch)


def test_a_refunded_total_with_no_currency_anywhere_is_refused():
    order = _order()
    order.pop("grandTotal")
    order["refundedTotal"] = {"value": "10.00"}

    with pytest.raises(ValueError):
        map_squarespace_order(order, store_id=STORE_ID, source=WEBHOOK)


def test_an_order_without_an_id_is_refused():
    with pytest.raises(ValueError):
        map_squarespace_order(_order(id=""), store_id=STORE_ID, source=WEBHOOK)


def test_a_negative_previously_recorded_figure_is_refused():
    with pytest.raises(ValueError):
        map_squarespace_order(
            _order(refundedTotal={"value": "10.00", "currency": "USD"}),
            store_id=STORE_ID,
            source=WEBHOOK,
            previously_recorded_refund_cents=-1,
        )


# ---- identity, and what never reaches the ledger ---------------------------


def test_the_two_ingresses_produce_identical_event_ids_for_one_order():
    """The whole point of keying on the order rather than the notification.

    A webhook observation and a sweep observation of the same order must
    collapse onto one ledger row. Only `source` differs.
    """
    from_webhook = map_squarespace_order(
        _order(refundedTotal={"value": "25.00", "currency": "USD"}),
        store_id=STORE_ID,
        source=WEBHOOK,
        topic="order.update",
        trace_id="notification-abc",
    )
    from_sweep = map_squarespace_order(
        _order(refundedTotal={"value": "25.00", "currency": "USD"}),
        store_id=STORE_ID,
        source=SWEEP,
        trace_id=None,
    )

    assert [event.event_id for event in from_webhook.events] == [
        event.event_id for event in from_sweep.events
    ]
    assert {event.source for event in from_webhook.events} == {WEBHOOK}
    assert {event.source for event in from_sweep.events} == {SWEEP}


def test_event_ids_are_scoped_to_the_store():
    a = map_squarespace_order(_order(), store_id="store-a", source=WEBHOOK)
    b = map_squarespace_order(_order(), store_id="store-b", source=WEBHOOK)

    assert {event.event_id for event in a.events} & {
        event.event_id for event in b.events
    } == set()


def test_no_buyer_email_or_product_name_reaches_the_ledger():
    """`customerEmail` is the only buyer field Squarespace exposes, and
    `productName` is merchant copy. Neither is a join key; both are dropped
    before the ledger's own metadata validation could reject them."""
    batch = map_squarespace_order(_order(), store_id=STORE_ID, source=WEBHOOK)

    event = _by_type(batch)["order.created"]
    assert event.buyer_id is None
    serialized = str(event.metadata)
    assert "buyer@example.com" not in serialized
    assert "A Nice Thing" not in serialized
    assert event.metadata["native_line_items"] == [
        {
            "id": "line-1",
            "product_id": "product-1",
            "variant_id": "variant-1",
            "sku": "SKU-1",
            "quantity": 2,
            "price": "20.00",
        }
    ]


def test_the_external_order_reference_is_never_read_as_a_pivota_identity():
    """`externalOrderReference` is free text an extension (or a buyer-facing
    integration) can set. Reading it as an order identity would let a forged
    `pivota:` string merge this order into an interaction it does not own."""
    batch = map_squarespace_order(_order(), store_id=STORE_ID, source=WEBHOOK)

    for event in batch.events:
        assert event.order_ref == f"squarespace:{ORDER_ID}"
        assert "ord_forged" not in str(event.metadata)


def test_the_order_number_is_kept_as_metadata_not_as_the_order_id():
    batch = map_squarespace_order(_order(), store_id=STORE_ID, source=WEBHOOK)

    event = _by_type(batch)["order.created"]
    assert event.metadata["native_order_number"] == "00042"
    assert event.order_id == ORDER_ID


# ---- the helpers the callers use to decide ---------------------------------


def test_the_helpers_read_the_order_without_mapping_it():
    order = _order(refundedTotal={"value": "25.00", "currency": "USD"})

    assert squarespace_order_ref(order) == f"squarespace:{ORDER_ID}"
    assert squarespace_order_currency(order) == "USD"
    assert squarespace_refunded_total_cents(order) == 2500
    # An order with nothing refunded reports zero, which is what lets the
    # callers skip the advisory lock entirely on the hot path.
    assert squarespace_refunded_total_cents(_order()) is None


@pytest.mark.parametrize(
    "topic, supported",
    [
        ("order.create", True),
        ("order.update", True),
        ("ORDER.CREATE", True),
        ("extension.uninstall", False),
        ("inventory.update", False),
        ("", False),
        (None, False),
    ],
)
def test_only_the_two_order_topics_are_supported(topic, supported):
    assert is_supported_squarespace_topic(topic) is supported


def test_a_zero_decimal_currency_is_not_multiplied():
    batch = map_squarespace_order(
        _order(grandTotal={"value": "4000", "currency": "JPY"}),
        store_id=STORE_ID,
        source=WEBHOOK,
    )

    assert _by_type(batch)["order.paid"].amount_cents == 4000
