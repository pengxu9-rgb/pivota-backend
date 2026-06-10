"""BUG-2 fix: the hosted-checkout create_order reuses an order-backed checkout intent's order instead of
minting a duplicate. Tests the _reuse_order_from_checkout_intent helper's gating."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import pytest

import routes.agent_shop_gateway as gw
import routes.buyer_api as buyer_api
import db.orders as orders_module


def _run(coro):
    return asyncio.run(coro)


def _install(monkeypatch, *, token_payload, intent_order_id, order_row):
    monkeypatch.setattr(buyer_api, "verify_checkout_token", lambda t: token_payload)

    async def fake_fetch_one(query, values=None):
        if intent_order_id is None:
            return None
        return {"order_id": intent_order_id}

    monkeypatch.setattr(gw.database, "fetch_one", fake_fetch_one)

    async def fake_get_order(order_id):
        return order_row

    monkeypatch.setattr(orders_module, "get_order", fake_get_order)


def test_reuses_payable_order_from_intent(monkeypatch):
    _install(
        monkeypatch,
        token_payload={"intent_id": "ci_123"},
        intent_order_id="ORD_20CC",
        order_row={
            "order_id": "ORD_20CC",
            "merchant_id": "merch_x",
            "total": 1.69,
            "currency": "USD",
            "status": "pending",
            "payment_status": "awaiting_payment",
            "psp_used": "stripe",
            "payment_intent_id": None,
            "client_secret": None,
        },
    )
    out = _run(gw._reuse_order_from_checkout_intent("v1.tok.sig"))
    assert out is not None
    assert out["order_id"] == "ORD_20CC"
    assert out["reused_existing_order"] is True
    assert out["total_amount"] == 1.69


def test_no_token_falls_through(monkeypatch):
    assert _run(gw._reuse_order_from_checkout_intent(None)) is None
    assert _run(gw._reuse_order_from_checkout_intent("")) is None


def test_intent_without_order_id_falls_through(monkeypatch):
    _install(monkeypatch, token_payload={"intent_id": "ci_123"}, intent_order_id=None, order_row=None)
    assert _run(gw._reuse_order_from_checkout_intent("v1.tok.sig")) is None


def test_token_without_intent_id_falls_through(monkeypatch):
    _install(monkeypatch, token_payload={"items": []}, intent_order_id="ORD_X", order_row={"order_id": "ORD_X"})
    assert _run(gw._reuse_order_from_checkout_intent("v1.tok.sig")) is None


def test_terminal_paid_order_is_not_reused(monkeypatch):
    _install(
        monkeypatch,
        token_payload={"intent_id": "ci_123"},
        intent_order_id="ORD_PAID",
        order_row={"order_id": "ORD_PAID", "status": "pending", "payment_status": "paid"},
    )
    # A paid order must NOT be resumed (would let a second pay attempt hit a settled order).
    assert _run(gw._reuse_order_from_checkout_intent("v1.tok.sig")) is None


def test_cancelled_order_is_not_reused(monkeypatch):
    _install(
        monkeypatch,
        token_payload={"intent_id": "ci_123"},
        intent_order_id="ORD_CANCELLED",
        order_row={"order_id": "ORD_CANCELLED", "status": "cancelled", "payment_status": "unpaid"},
    )
    assert _run(gw._reuse_order_from_checkout_intent("v1.tok.sig")) is None


def test_decode_failure_falls_through(monkeypatch):
    def boom(_t):
        raise ValueError("bad token")

    monkeypatch.setattr(buyer_api, "verify_checkout_token", boom)
    # Must never block order creation on a reuse attempt.
    assert _run(gw._reuse_order_from_checkout_intent("v1.bad.sig")) is None
