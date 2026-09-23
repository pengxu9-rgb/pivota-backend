"""What money a Shopify refunds/create moved, as the attribution edge counts it.

The edge writes are covered on real Postgres in test_shopify_refund_attribution_edge_postgres.py.
"""

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import shopify_commerce_event_ingest as ingest  # noqa: E402


def _tx(tx_id, amount, currency="USD", kind="refund", status="success"):
    return {"id": tx_id, "kind": kind, "status": status, "amount": amount, "currency": currency}


def test_sums_only_successful_refund_transactions_once_each():
    refund = {"transactions": [
        _tx(1, "10.50"),
        _tx(1, "10.50"),  # the same transaction listed twice
        _tx(2, "4.25"),
        _tx(3, "99.00", status="pending"),
        _tx(4, "99.00", kind="sale"),
    ]}
    assert ingest.shopify_refund_money(refund) == (Decimal("14.75"), "USD", None)


def test_refuses_rather_than_counting_part_of_a_refund():
    assert ingest.shopify_refund_money({"transactions": [_tx(1, "5"), _tx(2, "5", "EUR")]})[2] == (
        "mixed_or_missing_currency"
    )
    assert ingest.shopify_refund_money({"transactions": [_tx(1, "5", currency=None)]})[2] == (
        "mixed_or_missing_currency"
    )
    assert ingest.shopify_refund_money({"transactions": [_tx(1, "5"), _tx(2, "abc")]})[2] == "unreadable_amount"
    assert ingest.shopify_refund_money({"transactions": [_tx(1, "NaN")]})[2] == "unreadable_amount"
    assert ingest.shopify_refund_money({"transactions": []})[2] == "no_successful_refund_transaction"


async def test_a_failing_edge_write_never_escapes_to_the_webhook(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(ingest, "apply_external_order_refund", _boom)
    result = await ingest.apply_shopify_refund_to_attribution_edges(
        merchant_id="m1", payload={"id": 9, "order_id": 7, "transactions": [_tx(1, "5")]}
    )
    assert result == {"status": "error", "reason": "RuntimeError"}


async def test_the_refund_is_keyed_by_the_shopify_refund_id_in_major_units(monkeypatch):
    seen = {}

    async def _record(**kwargs):
        seen.update(kwargs)
        return [{"edge_id": "cae_ext_1", "status": "applied", "applied_cents": 1250}]

    monkeypatch.setattr(ingest, "apply_external_order_refund", _record)
    result = await ingest.apply_shopify_refund_to_attribution_edges(
        merchant_id="m1", payload={"id": 9, "order_id": 7, "transactions": [_tx(1, "12.50")]}
    )
    assert result["status"] == "applied"
    assert seen == {
        "merchant_id": "m1",
        "external_order_id": "7",
        "refund_id": "shopify:9",
        "amount": Decimal("12.50"),
        "currency": "USD",
    }
