"""The pure Webflow order mapper.

The centre of gravity here is MONEY. Webflow states amounts in minor units
already, so the mapper does no conversion — and the single worst outcome this
integration could produce is a 100x error from someone "fixing" that to match
every other adapter in the repo. So the documented example (`5898` == $58.98) is
pinned with its own test, and a `value` that is not a whole number of minor
units is asserted to be REFUSED rather than guessed at.
"""

from __future__ import annotations

import pytest

STORE_ID = "store-wf"
ORDER_ID = "0000-0001"
SOURCE = "webflow_webhook"


def _order(**overrides):
    order = {
        "orderId": ORDER_ID,
        "status": "unfulfilled",
        "acceptedOn": "2026-09-01T10:00:00.000Z",
        # The documented Webflow money shape: `unit` is the currency and `value`
        # is an INTEGER number of minor units.
        "customerPaid": {"unit": "USD", "value": 5898, "string": "$58.98"},
        "netAmount": {"unit": "USD", "value": 5600, "string": "$56.00"},
        "purchasedItemsCount": 2,
        "purchasedItems": [
            {
                "count": 2,
                "rowTotal": {"unit": "USD", "value": 5898},
                "productId": "prod-1",
                "productName": "A Very Nice Shirt",
                "variantId": "var-1",
                "variantSKU": "SKU-1",
            }
        ],
        "customerInfo": {"fullName": "A Buyer", "email": "buyer@example.com"},
        "paymentProcessor": "stripe",
        "stripeDetails": {
            "paymentIntentId": "pi_1",
            "chargeId": "ch_1",
            "customerId": "cus_1",
        },
    }
    order.update(overrides)
    return order


def _mapping(order, **kwargs):
    from services.webflow_event_adapter import map_webflow_order

    return map_webflow_order(order, store_id=STORE_ID, source=SOURCE, **kwargs)


def _map(order, **kwargs):
    """The BATCH out of a mapping. Most tests are about the events alone; the
    ones about what was deliberately NOT recorded use `_mapping`."""
    return _mapping(order, **kwargs).batch


def _by_type(batch):
    return {event.event_type: event for event in batch.events}


# ---- money: the 100x test ---------------------------------------------------


def test_the_documented_money_example_is_read_as_minor_units_unchanged():
    """`{"unit": "USD", "value": 5898, "string": "$58.98"}` is 5898 cents.

    The mutant this kills is a `* 100` "for consistency with the other
    adapters": that would file a $58.98 order as $5,898.00, and nothing
    downstream distinguishes an inflated amount from a real one.
    """
    events = _by_type(_map(_order()))

    assert events["order.paid"].amount_cents == 5898
    assert events["order.paid"].currency == "USD"
    # And the human-readable string Webflow also sends confirms the reading.
    assert _order()["customerPaid"]["string"] == "$58.98"


def test_a_money_value_as_an_integer_string_is_accepted():
    """Webflow's JSON has been seen with both; both are whole minor units."""
    events = _by_type(_map(_order(customerPaid={"unit": "USD", "value": "5898"})))

    assert events["order.paid"].amount_cents == 5898


@pytest.mark.parametrize(
    "value",
    [
        "58.98",  # a decimal string is the shape that becomes 58 or 5898
        58.98,
        "5,898",
        "abc",
        True,
        [5898],
    ],
)
def test_a_value_that_is_not_whole_minor_units_is_refused_not_guessed(value):
    from services.webflow_event_adapter import WebflowMoneyFormatError

    with pytest.raises(WebflowMoneyFormatError):
        _map(_order(customerPaid={"unit": "USD", "value": value}))


# ---- money: `value` cross-checked against `string` --------------------------
#
# The refusals above only reject values that are not WHOLE NUMBERS. That leaves
# the other half of the 100x claim wide open: `60` is a perfectly whole number,
# and it is 1/100th of a `"$60.00"` order. `string` is a second, independent
# statement of the same amount in the same object, so it is parsed and compared —
# and a disagreement RAISES, because there is no third source to break the tie.


def test_the_documented_example_agrees_with_its_own_string():
    """`{"unit":"USD","value":5898,"string":"$58.98"}` -> 5898, and the
    cross-check is what now says so rather than the docstring."""
    events = _by_type(_map(_order(customerPaid={"unit": "USD", "value": 5898,
                                                "string": "$58.98"})))

    assert events["order.paid"].amount_cents == 5898


def test_a_zero_decimal_string_with_a_thousands_separator_agrees():
    """`"\u00a55,898"` is 5898 yen, not 5.898 of anything.

    A single separator with exactly three digits after it is a GROUP separator.
    Getting this wrong would make the cross-check refuse every JPY order — the
    parser has to be right about the boring case before its refusals mean
    anything.
    """
    events = _by_type(
        _map(_order(customerPaid={"unit": "JPY", "value": 5898,
                                  "string": "\u00a55,898"}))
    )

    assert events["order.paid"].amount_cents == 5898


def test_a_zero_decimal_value_stated_in_HUNDREDTHS_is_refused():
    """Assumption 22, closed for any order that carries a `string`.

    If Webflow reported a \u00a55,898 order as `value: 589800`, this bridge used to
    file it as \u00a5589,800 — a 100x over-count with no second source to
    contradict it. There IS a second source: the string in the same object.
    """
    from services.webflow_event_adapter import WebflowMoneyFormatError

    with pytest.raises(WebflowMoneyFormatError) as excinfo:
        _map(_order(customerPaid={"unit": "JPY", "value": 589800,
                                  "string": "\u00a55,898"}))

    message = str(excinfo.value)
    assert "589800" in message and "5898" in message, message
    assert "JPY" in message


def test_a_value_in_MAJOR_units_is_refused_even_though_it_is_a_whole_number():
    """The hole the whole-number check could never see.

    `{"unit":"USD","value":60,"string":"$60.00"}` passes every earlier refusal —
    60 is an integer, non-negative, not a decimal string — and files a $60 order
    as 60 cents.
    """
    from services.webflow_event_adapter import WebflowMoneyFormatError

    with pytest.raises(WebflowMoneyFormatError) as excinfo:
        _map(_order(customerPaid={"unit": "USD", "value": 60,
                                  "string": "$60.00"}))

    assert "6000" in str(excinfo.value)


def test_neither_side_wins_a_disagreement():
    """Documented behaviour, pinned as behaviour: the observation is REFUSED.

    Preferring `string` would make the ledger's totals depend on a display
    value; preferring `value` is the assumption under test. So the mapper emits
    NOTHING for this order — not a partially-mapped one, not a guess — and the
    sweep counts it `invalid` while the receiver answers 422.
    """
    from services.webflow_event_adapter import WebflowMoneyFormatError

    with pytest.raises(WebflowMoneyFormatError):
        _map(_order(customerPaid={"unit": "USD", "value": 5898,
                                  "string": "$1.00"}))


@pytest.mark.parametrize(
    "case, text",
    [
        ("plain", "58.98"),
        ("a symbol and a group separator", "$1,234.56"),
        ("a European decimal comma", "\u20ac1.234,56"),
        ("a currency code instead of a symbol", "USD 1234.56"),
        ("a space as the group separator", "1 234.56"),
        ("a non-breaking space", "1\u00a0234.56"),
    ],
)
def test_the_string_parser_reads_the_formats_a_money_string_comes_in(case, text):
    from services.webflow_event_adapter import _decimal_from_money_string

    assert _decimal_from_money_string(text) is not None, case


@pytest.mark.parametrize(
    "case, money",
    [
        ("no string at all", {"unit": "USD", "value": 5898}),
        ("an empty string", {"unit": "USD", "value": 5898, "string": ""}),
        ("a string with no digits", {"unit": "USD", "value": 5898, "string": "free"}),
        (
            # Without the currency there is no multiplier, so there is nothing
            # to compare against. Skipped, not guessed at with 100.
            "no readable currency",
            {"value": 5898, "string": "$58.98"},
        ),
    ],
)
def test_a_missing_or_unreadable_string_keeps_the_previous_behaviour(case, money):
    """The cross-check must never make an order that used to map stop mapping.

    Most of the fixtures in this file, and every order Webflow has actually been
    observed sending through the sweep's tests, carry no `string`.
    """
    events = _by_type(_map(_order(customerPaid=money)))

    assert events["order.paid"].amount_cents == 5898, case


def test_an_unparseable_string_is_a_DEBUG_line_and_not_a_refusal(caplog):
    """A refusal here would turn an unfamiliar formatting locale into a store
    that records no money at all — the cure being worse than the disease."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="webflow_event_adapter"):
        events = _by_type(
            _map(_order(customerPaid={"unit": "USD", "value": 5898,
                                      "string": "fifty-eight ninety-eight"}))
        )

    assert events["order.paid"].amount_cents == 5898
    assert any(
        "not cross-checked" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG
    )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_negative_amount_is_refused_rather_than_clamped():
    from services.webflow_event_adapter import WebflowMoneyFormatError

    with pytest.raises(WebflowMoneyFormatError):
        _map(_order(customerPaid={"unit": "USD", "value": -100}))


def test_a_float_that_is_a_whole_number_is_accepted():
    """JSON `5898.0` is still 5898 cents; only a fractional one is a red flag."""
    events = _by_type(_map(_order(customerPaid={"unit": "USD", "value": 5898.0})))

    assert events["order.paid"].amount_cents == 5898


def test_zero_decimal_currencies_are_not_special_cased():
    """Because the value is ALREADY minor units, there is nothing to convert.

    A JPY order of 5898 yen is `value: 5898`, exactly as a USD order of $58.98 is
    5898 cents. An adapter that carried the usual zero-decimal table would be
    carrying dead code that could only ever introduce a bug — SO LONG AS Webflow
    really does state JPY that way, which is assumption 22 and is what the
    tripwire below exists for.
    """
    events = _by_type(_map(_order(customerPaid={"unit": "JPY", "value": 5898})))

    assert events["order.paid"].amount_cents == 5898
    assert events["order.paid"].currency == "JPY"


def test_a_zero_decimal_currency_trips_a_warning_naming_the_store_and_the_value(
    caplog, monkeypatch
):
    """The tripwire under assumption 22.

    "There is no zero-decimal table because there is nothing to special-case" is
    only true if Webflow states JPY in yen rather than in hundredths of a yen.
    No order this repo has seen proves that, and if it is wrong a ¥5,898 order is
    filed as ¥589,800 with no second source to contradict it. So the FIRST such
    order per process per currency says so, out loud.
    """
    import logging

    from services import webflow_event_adapter as adapter

    monkeypatch.setattr(adapter, "_ZERO_DECIMAL_OBSERVED", set())

    with caplog.at_level(logging.WARNING, logger="webflow_event_adapter"):
        _map(_order(customerPaid={"unit": "JPY", "value": 5898}))

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1, warnings
    assert "webflow_zero_decimal_currency_observed" in warnings[0]
    assert "JPY" in warnings[0]
    assert STORE_ID in warnings[0]
    assert "5898" in warnings[0]
    assert ORDER_ID in warnings[0]

    # ...and ONCE per currency, not once per order: a store doing steady JPY
    # volume must not turn this into a log flood that gets muted.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="webflow_event_adapter"):
        _map(_order(orderId="0000-0002", customerPaid={"unit": "JPY", "value": 1200}))
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_two_decimal_currency_trips_NOTHING(caplog, monkeypatch):
    """The counterpart. A warning that fired for USD would fire on essentially
    every order and mean nothing at all."""
    import logging

    from services import webflow_event_adapter as adapter

    monkeypatch.setattr(adapter, "_ZERO_DECIMAL_OBSERVED", set())

    with caplog.at_level(logging.WARNING, logger="webflow_event_adapter"):
        _map(_order(customerPaid={"unit": "USD", "value": 5898}))

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_zero_amount_order_does_not_take_the_order_paid_key(caplog):
    """`and paid_minor` in the `order.paid` guard, pinned.

    A money event with a zero amount would take this order's DETERMINISTIC
    `order.paid` key — and the ledger is first-write-wins, so the real figure
    arriving on the next observation would be discarded forever. `is not None`
    there looks equivalent and is not: it admits the zero.
    """
    events = _by_type(_map(_order(customerPaid={"unit": "USD", "value": 0})))

    assert "order.paid" not in events, (
        "a value:0 order took its own order.paid key — the real amount can now "
        "never be recorded under it"
    )
    # And the order itself still exists: a guard that dropped everything would
    # satisfy the assertion above while losing the purchase.
    assert "order.created" in events
    assert events["order.created"].amount_cents == 0


# ---- currency ---------------------------------------------------------------


def test_the_currency_comes_from_unit_not_from_a_currency_key():
    events = _by_type(_map(_order()))

    assert events["order.created"].currency == "USD"


def test_a_paid_order_with_no_readable_currency_records_no_money_event():
    """No currency means the funnel cannot bucket the amount, so there is no
    money event — but the order itself is still observed."""
    batch = _map(_order(customerPaid={"value": 5898}, netAmount={"value": 5600}))
    events = _by_type(batch)

    assert "order.created" in events
    assert "order.paid" not in events


@pytest.mark.parametrize(
    "case, customer_paid",
    [
        ("no currency", {"value": 5898}),
        ("absent amount", {"unit": "USD"}),
        ("zero amount", {"unit": "USD", "value": 0}),
    ],
)
def test_a_refunded_order_with_no_readable_money_keeps_the_order_and_NAMES_the_gap(
    case, customer_paid
):
    """The negative counterpart of the test above — and it must not cost the ORDER.

    A refund silently omitted is money the ledger says never came back, so it
    cannot be swallowed. But raising took `order.created` with it: the receiver
    answered 422, Webflow retries a 422 into the same 422, and the order never
    landed at all. So the readable half is recorded and the unreadable half is a
    NAMED reason a caller can count, log and print.
    """
    from services.webflow_event_adapter import REFUND_AMOUNT_UNREADABLE

    mapping = _mapping(
        _order(
            status="refunded",
            refundedOn="2026-09-03T10:00:00.000Z",
            customerPaid=customer_paid,
            netAmount={"value": 5600},
        )
    )

    types = {event.event_type for event in mapping.batch.events}
    assert "order.created" in types, f"{case}: the order itself was dropped"
    assert "refund.succeeded" not in types, f"{case}: an unreadable refund was invented"
    assert len(mapping.ignored) == 1, case
    assert mapping.ignored[0].startswith(REFUND_AMOUNT_UNREADABLE), case
    assert ORDER_ID in mapping.ignored[0], case


def test_a_readable_refund_reports_NOTHING_ignored():
    """The positive counterpart: `ignored` must not be a field that is always
    populated, or the counter it feeds means nothing."""
    mapping = _mapping(
        _order(
            status="refunded",
            refundedOn="2026-09-03T10:00:00.000Z",
            customerPaid={"unit": "USD", "value": 5898},
        )
    )

    assert mapping.ignored == ()
    assert "refund.succeeded" in {e.event_type for e in mapping.batch.events}


def test_a_malformed_refund_amount_is_STILL_fatal_to_the_whole_observation():
    """Absent is not the same as WRONG. `"58.98"` is the 100x shape, and half
    of it must never be recorded — that is a different claim from the one above
    and it has to keep raising."""
    with pytest.raises(ValueError):
        _map(
            _order(
                status="refunded",
                refundedOn="2026-09-03T10:00:00.000Z",
                customerPaid={"unit": "USD", "value": "58.98"},
            )
        )


# ---- every status -----------------------------------------------------------


def test_a_pending_order_is_created_but_not_paid():
    """`pending` is Webflow's unpaid state (a PayPal payment awaiting capture).

    A later `ecomm_order_changed`, or the sweep, completes it — and because the
    event ids are deterministic, that later observation adds `order.paid` beside
    the same `order.created` rather than duplicating the order.
    """
    batch = _map(_order(status="pending", acceptedOn=None))
    events = _by_type(batch)

    assert set(events) == {"order.created"}


@pytest.mark.parametrize(
    "status", ["unfulfilled", "fulfilled", "disputed", "dispute-lost", "refunded"]
)
def test_every_accepted_status_emits_order_paid(status):
    """An accepted Webflow order was paid for, whatever happened to it after."""
    order = _order(status=status)
    if status == "refunded":
        order["refundedOn"] = "2026-09-03T10:00:00.000Z"
    if status in ("disputed", "dispute-lost"):
        order["disputedOn"] = "2026-09-03T10:00:00.000Z"
    events = _by_type(_map(order))

    assert events["order.paid"].amount_cents == 5898
    assert events["order.paid"].metadata["native_amount_semantics"] == "customer_paid"


def test_a_disputed_order_holds_funds_and_emits_no_money_movement():
    """`disputed` means the funds are HELD pending the outcome, not returned.

    Recording a refund here and another one if the dispute is later lost would
    count the same money out twice; recording one and never correcting it if the
    dispute is WON would invent a refund that never happened.
    """
    batch = _map(_order(status="disputed", disputedOn="2026-09-03T10:00:00.000Z"))
    events = _by_type(batch)

    assert set(events) == {"order.created", "order.paid"}
    assert events["order.paid"].metadata["native_status"] == "disputed"


def test_a_refunded_order_emits_one_full_amount_refund():
    """Webflow refunds are FULL-ORDER only, so the amount is the whole
    `customerPaid` and there is exactly one of them — no cumulative delta, and
    therefore no baseline read and no lock anywhere in this integration."""
    batch = _map(
        _order(
            status="refunded",
            refundedOn="2026-09-03T10:00:00.000Z",
            stripeDetails={"refundId": "re_1", "refundReason": "requested_by_customer"},
        )
    )
    events = _by_type(batch)
    refund = events["refund.succeeded"]

    assert refund.amount_cents == 5898
    assert refund.currency == "USD"
    assert refund.occurred_at.isoformat() == "2026-09-03T10:00:00+00:00"
    assert refund.metadata["native_amount_semantics"] == "full_order_refund"
    # The PSP's refund id is a DIAGNOSTIC, never the key: it is present for a
    # Stripe order and absent for a PayPal one, so keying on it would give one
    # refund two rows across observations that disagree about whether it is
    # there.
    assert refund.metadata["native_psp_refund_id"] == "re_1"
    assert "native_psp_refund_reason" not in refund.metadata
    assert refund.refund_id == f"{ORDER_ID}:refund"


def test_the_SAME_refund_seen_with_and_without_a_psp_refund_id_is_ONE_row():
    """The mutant this kills: keying the refund on `stripeDetails.refundId`.

    That field is present for a Stripe order and absent for a PayPal one — and it
    is also absent from the first observation of a Stripe refund whose
    `stripeDetails` has not been populated yet. An order observed once without it
    and once with it would then land on TWO different keys, and the funnel SUMS
    refund rows: one refund would read as two.

    Asserting `refund_id` alone is not enough, because `refund_id` is a separate
    field from the entity the EVENT ID is derived from. It is the event id that
    decides whether the ledger collapses the two observations.
    """
    without = _by_type(
        _map(
            _order(
                status="refunded",
                refundedOn="2026-09-03T10:00:00.000Z",
                stripeDetails={"chargeId": "ch_1"},
            )
        )
    )["refund.succeeded"]
    with_id = _by_type(
        _map(
            _order(
                status="refunded",
                refundedOn="2026-09-03T10:00:00.000Z",
                stripeDetails={"chargeId": "ch_1", "refundId": "re_1"},
            )
        )
    )["refund.succeeded"]

    assert without.event_id == with_id.event_id
    assert without.refund_id == with_id.refund_id == f"{ORDER_ID}:refund"
    # The PSP id still rides along as a diagnostic on the observation that had it.
    assert with_id.metadata["native_psp_refund_id"] == "re_1"
    assert "native_psp_refund_id" not in without.metadata


def test_a_lost_dispute_is_money_out_under_the_SAME_key_as_a_refund():
    """The two statuses are mutually exclusive at any instant, but an order can
    MOVE between them across observations. Two keys would then record the same
    money twice; one key makes at most one refund row per order structurally
    true, and both carry the identical full `customerPaid` amount anyway."""
    refunded = _by_type(
        _map(_order(status="refunded", refundedOn="2026-09-03T10:00:00.000Z"))
    )["refund.succeeded"]
    lost = _by_type(
        _map(
            _order(
                status="dispute-lost",
                disputedOn="2026-09-04T10:00:00.000Z",
                # A different PSP shape from the refunded observation above, so
                # this compares the KEY rather than two identical inputs.
                stripeDetails={"disputeId": "dp_1", "refundId": "re_9"},
            )
        )
    )["refund.succeeded"]

    assert lost.amount_cents == 5898
    assert lost.metadata["native_amount_semantics"] == "dispute_lost_full_amount"
    assert lost.metadata["native_psp_dispute_id"] == "dp_1"
    assert lost.event_id == refunded.event_id
    assert lost.refund_id == refunded.refund_id == f"{ORDER_ID}:refund"


@pytest.mark.parametrize(
    "case, order_fields, expected",
    [
        (
            "the spelling this bridge was written against",
            {"disputeUpdatedOn": "2026-09-05T10:00:00.000Z"},
            "2026-09-05T10:00:00+00:00",
        ),
        (
            # The spelling the Data API reference shows on the order object.
            # Tried BEFORE `disputedOn` because it moves when the dispute is
            # DECIDED, while `disputedOn` records when it was opened.
            "the reference's spelling",
            {"disputeLastUpdated": "2026-09-05T10:00:00.000Z"},
            "2026-09-05T10:00:00+00:00",
        ),
        (
            "both, with the original spelling winning",
            {
                "disputeUpdatedOn": "2026-09-05T10:00:00.000Z",
                "disputeLastUpdated": "2026-09-06T10:00:00.000Z",
            },
            "2026-09-05T10:00:00+00:00",
        ),
        (
            # The fallback. It back-dates the refund row by the length of the
            # dispute, which is why the two decision spellings come first.
            "neither, so the OPENING timestamp",
            {"disputedOn": "2026-09-01T09:00:00.000Z"},
            "2026-09-01T09:00:00+00:00",
        ),
    ],
)
def test_the_lost_dispute_anchor_reads_every_spelling_of_the_decision_time(
    case, order_fields, expected
):
    """Which field Webflow actually sends is an ASSUMED claim (row 19).

    Reading one spelling and falling through to `disputedOn` anchors the refund
    at the moment the dispute was OPENED rather than lost — a row back-dated by
    however long the dispute ran, which lands it in the wrong reporting period
    and, on the sweep's dispute lane, behind a cursor that has already passed.
    """
    order = _order(status="dispute-lost", **order_fields)
    order.pop("refundedOn", None)

    event = _by_type(_map(order))["refund.succeeded"]

    assert event.occurred_at.isoformat() == expected, case


def test_webflow_has_no_cancelled_state_so_none_is_invented():
    """`order.cancelled` is never emitted. Inventing one from `refunded` would
    file a refund as a cancellation and count the order in two funnels."""
    for status in ("pending", "unfulfilled", "fulfilled", "refunded", "dispute-lost"):
        order = _order(status=status)
        order["refundedOn"] = "2026-09-03T10:00:00.000Z"
        order["disputedOn"] = "2026-09-03T10:00:00.000Z"
        assert "order.cancelled" not in _by_type(_map(order))


# ---- identity and dedupe ----------------------------------------------------


def test_event_ids_are_derived_from_the_order_and_not_from_the_ingress():
    """A webhook observation and a sweep observation of one order must collapse
    onto one ledger row. If `source` reached the id, an OAuth store with both
    paths armed would count every purchase twice."""
    from services.webflow_event_adapter import map_webflow_order

    webhook = map_webflow_order(
        _order(), store_id=STORE_ID, source="webflow_webhook"
    ).batch
    sweep = map_webflow_order(
        _order(), store_id=STORE_ID, source="webflow_reconciliation"
    ).batch

    assert [e.event_id for e in webhook.events] == [e.event_id for e in sweep.events]
    assert webhook.events[0].source == "webflow_webhook"
    assert sweep.events[0].source == "webflow_reconciliation"


def test_event_ids_are_scoped_to_the_store():
    from services.webflow_event_adapter import map_webflow_order

    a = map_webflow_order(_order(), store_id="store-a", source=SOURCE).batch
    b = map_webflow_order(_order(), store_id="store-b", source=SOURCE).batch

    assert a.events[0].event_id != b.events[0].event_id


def test_the_order_ref_is_namespaced():
    assert _map(_order()).events[0].order_ref == f"webflow:{ORDER_ID}"


def test_an_order_with_no_id_is_a_malformed_order():
    with pytest.raises(ValueError):
        _map(_order(orderId=None))


# ---- what must NEVER reach the ledger ---------------------------------------


def test_no_buyer_pii_reaches_the_ledger():
    """`customerInfo` is a name and an email. Neither is a join key and both are
    PII the ledger must never hold."""
    batch = _map(_order())

    for event in batch.events:
        assert event.buyer_id is None
        rendered = repr(event.metadata)
        assert "buyer@example.com" not in rendered
        assert "A Buyer" not in rendered


def test_custom_data_is_never_read_as_a_pivota_order_identity():
    """`customData` is buyer-entered free text. Reading a `pivota:` string out of
    it would let a forged value merge this order into an interaction it does not
    own (the BigCommerce reasoning)."""
    batch = _map(
        _order(customData=[{"name": "note", "value": "pivota:ord_someone_elses"}])
    )

    for event in batch.events:
        assert event.order_ref == f"webflow:{ORDER_ID}"
        assert "ord_someone_elses" not in repr(event.metadata)


def test_line_items_keep_join_keys_and_quantity_and_nothing_else():
    """No line-item AMOUNT is carried, deliberately.

    The ledger's line-item vocabulary spells amounts as `price`/`subtotal`/
    `total`, and every other adapter writes a DECIMAL string there. Webflow's are
    minor-unit integers, so writing one under those names would plant the same
    100x ambiguity this adapter refuses everywhere else — for a diagnostic, while
    the order's real money is `customerPaid`. `productName` is merchant copy and
    is dropped for the usual reason.
    """
    items = _map(_order()).events[0].metadata["native_line_items"]

    assert items == [
        {
            "product_id": "prod-1",
            "variant_id": "var-1",
            "sku": "SKU-1",
            "quantity": 2,
        }
    ]


def test_a_flagged_test_order_is_ignored_entirely():
    """Webflow is not known to flag test orders, so this guard is defensive —
    but an unpaid test order in the paid totals is fabricated GMV under a
    deterministic key that could then never be reused for the real one."""
    from services.webflow_event_adapter import UnsupportedWebflowEvent

    with pytest.raises(UnsupportedWebflowEvent):
        _map(_order(metadata={"isTest": True}))


# ---- triggers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("ecomm_new_order", "ecomm_new_order"),
        ("ECOMM_ORDER_CHANGED", "ecomm_order_changed"),
        (" ecomm_new_order ", "ecomm_new_order"),
        ("ecomm_inventory_changed", None),
        ("", None),
        (None, None),
    ],
)
def test_trigger_normalization(value, expected):
    from services.webflow_event_adapter import normalize_webflow_trigger

    assert normalize_webflow_trigger(value) == expected
