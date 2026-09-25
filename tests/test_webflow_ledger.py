"""The one ingest path, and the contracts it must not drift from.

`services/webflow_ledger.py` is three lines of routing over the mapper, and all
three are load-bearing: which write path each ingress stamps, that the write
path is a server-fixed LITERAL, and that a mapper refusal is an `ignored` result
rather than an exception the receiver would turn into a 5xx.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

MERCHANT_ID = "merchant-wf"
STORE_ID = "store-wf"


def _order(**overrides):
    order = {
        "orderId": "0000-0001",
        "status": "unfulfilled",
        "acceptedOn": "2026-09-01T10:00:00.000Z",
        "customerPaid": {"unit": "USD", "value": 5898},
    }
    order.update(overrides)
    return order


def _capture(monkeypatch):
    from services import webflow_ledger as ledger

    calls = []

    async def fake_ingest(**kwargs):
        calls.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(ledger, "ingest_merchant_event_batch", fake_ingest)
    return calls


@pytest.mark.parametrize(
    "from_webhook, expected",
    [(True, "webflow_webhook"), (False, "webflow_reconciliation")],
)
async def test_each_ingress_stamps_its_own_write_path(monkeypatch, from_webhook, expected):
    from services.webflow_ledger import record_webflow_order

    calls = _capture(monkeypatch)

    result = await record_webflow_order(
        merchant_id=MERCHANT_ID,
        store_id=STORE_ID,
        order=_order(),
        from_webhook=from_webhook,
    )

    assert result.status == "recorded"
    assert calls[0]["write_path"] == expected
    assert calls[0]["agent_identity_confidence"] == "platform_asserted"
    # And the row's `source` matches the ingress that observed it.
    assert {event.source for event in calls[0]["batch"].events} == {expected}


def test_the_literals_in_the_ingest_call_equal_the_module_constants():
    """The write path must be a server-fixed literal at the call site
    (tests/test_commerce_ledger_write_path_authority.py reads it with `ast`), so
    it cannot be spelled as the constant. This is what keeps the two spellings
    from drifting apart."""
    from services import webflow_ledger as ledger

    source = pathlib.Path(ledger.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("webflow_")
    }

    assert ledger.WEBFLOW_WEBHOOK_WRITE_PATH == "webflow_webhook"
    assert ledger.WEBFLOW_RECONCILIATION_WRITE_PATH == "webflow_reconciliation"
    assert {"webflow_webhook", "webflow_reconciliation"} <= literals


async def test_both_write_paths_are_ledger_vocabulary_at_the_same_authority():
    """An unprovisioned store has only the sweep. If the sweep's authority were
    weaker, that store's orders would be filed below an identical provisioned
    store's — a merchant's standing would depend on whether an ensure was run."""
    from services.commerce_ledger_provenance import (
        LEDGER_AUTHORITY_BY_WRITE_PATH,
        WritePath,
    )

    allowed = set(WritePath.__args__)
    assert {"webflow_webhook", "webflow_reconciliation"} <= allowed
    assert LEDGER_AUTHORITY_BY_WRITE_PATH["webflow_webhook"] == "platform"
    assert LEDGER_AUTHORITY_BY_WRITE_PATH["webflow_reconciliation"] == "platform"


async def test_a_test_order_is_an_ignored_result_not_an_exception(monkeypatch):
    """Nothing went wrong; there is simply nothing to record. An exception here
    would become a 5xx and make Webflow retry a delivery forever."""
    from services.webflow_ledger import record_webflow_order

    calls = _capture(monkeypatch)

    result = await record_webflow_order(
        merchant_id=MERCHANT_ID,
        store_id=STORE_ID,
        order=_order(metadata={"isTest": True}),
        from_webhook=True,
    )

    assert result.status == "ignored"
    assert "test_order" in result.reason
    assert calls == []


async def test_a_malformed_money_value_propagates_as_a_ValueError(monkeypatch):
    """The counterpart. A 100x-ambiguous amount is NOT an ignorable observation:
    the receiver turns this into a 422 and the sweep counts it as invalid."""
    from services.webflow_ledger import record_webflow_order

    _capture(monkeypatch)

    with pytest.raises(ValueError):
        await record_webflow_order(
            merchant_id=MERCHANT_ID,
            store_id=STORE_ID,
            order=_order(customerPaid={"unit": "USD", "value": "58.98"}),
            from_webhook=True,
        )


async def test_no_money_lock_is_taken_anywhere_in_this_integration():
    """Not an omission: a property of Webflow.

    Shoplazza and Squarespace hold `order_money_read_modify_write_lock` because
    their refund figure is CUMULATIVE and the amount to record is a delta against
    what Pivota already stored — a read-modify-write, and a raced pair of those
    inflates refunded GMV. Webflow refunds are full-order only, so two concurrent
    observations emit the IDENTICAL row under one deterministic key and the
    ledger's first-write-wins collapses them. If this assertion ever has to
    change, it means partial refunds arrived and the delta machinery is needed.
    """
    from services import webflow_ledger, webflow_order_sweep

    called_by_module = {}
    for module in (webflow_ledger, webflow_order_sweep):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {
            getattr(node.func, "id", getattr(node.func, "attr", None))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        called_by_module[module.__name__] = called
        assert "order_money_read_modify_write_lock" not in called
        assert "recorded_refund_amount_cents" not in called

    # THE POSITIVE COUNTERPART, and it is not decoration. Every assertion above
    # is an absence, and an absence is exactly what a broken extractor produces:
    # a walk that resolved no call name at all — a renamed attribute, an `ast`
    # shape this comprehension does not reach, a module that failed to parse into
    # anything — would pass all four while proving nothing. So the SAME extracted
    # set has to contain the call this integration certainly does make.
    assert "ingest_merchant_event_batch" in called_by_module["services.webflow_ledger"], (
        "the AST walk found no ingest call in the one module that exists to make "
        "it — the extractor is broken, so the absences above prove nothing"
    )
    assert "record_webflow_order" in called_by_module["services.webflow_order_sweep"]


async def test_the_summary_shape_is_the_same_whatever_happened(monkeypatch):
    from services.webflow_ledger import WebflowIngestResult

    recorded = WebflowIngestResult(status="recorded", accepted=2, duplicates=1)
    ignored = WebflowIngestResult(status="ignored", reason="test_order: ...")

    assert recorded.as_summary(trigger_type="ecomm_new_order") == {
        "status": "recorded",
        "platform": "webflow",
        "accepted": 2,
        "duplicates": 1,
        "trigger_type": "ecomm_new_order",
    }
    body = ignored.as_summary()
    assert body["accepted"] == 0 and body["duplicates"] == 0
    assert body["reason"].startswith("test_order")


async def test_an_unreadable_refund_is_recorded_as_a_named_reason_not_an_exception(
    monkeypatch, caplog
):
    """The order lands, the missing refund is NAMED.

    Raising here dropped `order.created` with the refund: the receiver answered
    422, Webflow retries a 422 into the same 422 until it gives up, and the order
    never reached the ledger at all. Under-reporting money OUT is bad; losing the
    purchase as well is strictly worse.
    """
    import logging

    from services.webflow_event_adapter import REFUND_AMOUNT_UNREADABLE
    from services.webflow_ledger import record_webflow_order

    calls = _capture(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="webflow_ledger"):
        result = await record_webflow_order(
            merchant_id=MERCHANT_ID,
            store_id=STORE_ID,
            order=_order(
                status="refunded",
                refundedOn="2026-09-03T10:00:00.000Z",
                customerPaid={"unit": "USD", "value": 0},
            ),
            from_webhook=True,
        )

    assert result.status == "recorded"
    assert len(result.ignored_reasons) == 1
    assert result.ignored_reasons[0].startswith(REFUND_AMOUNT_UNREADABLE)
    # The batch that DID reach the ledger carries the order and no refund row.
    types = {event.event_type for event in calls[0]["batch"].events}
    assert "order.created" in types
    assert "refund.succeeded" not in types
    # And it is audible: money out that this bridge could not record must not be
    # a silent field on a success response.
    assert any(
        REFUND_AMOUNT_UNREADABLE in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), [r.getMessage() for r in caplog.records]
    # The summary carries it too, so a webhook caller sees it without reading logs.
    assert result.as_summary()["ignored_reasons"] == list(result.ignored_reasons)


async def test_a_fully_mapped_order_reports_no_ignored_reasons(monkeypatch):
    """The counterpart: `ignored_reasons` must be empty on the ordinary path, or
    the sweep counter it feeds would count every order."""
    from services.webflow_ledger import record_webflow_order

    _capture(monkeypatch)

    result = await record_webflow_order(
        merchant_id=MERCHANT_ID,
        store_id=STORE_ID,
        order=_order(
            status="refunded",
            refundedOn="2026-09-03T10:00:00.000Z",
            customerPaid={"unit": "USD", "value": 5898},
        ),
        from_webhook=False,
    )

    assert result.ignored_reasons == ()
    assert "ignored_reasons" not in result.as_summary()
