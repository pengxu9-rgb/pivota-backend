"""The agent fulfillment routes answer only for the calling agent's own orders.

`GET /agent/v1/fulfillment/orders/in-transit` listed the 50 latest shipped orders of EVERY agent,
buyer emails included, to any key whose merchant scope is unrestricted (`allowed_merchants` None),
which is every self-serve key. `GET /agent/v1/fulfillment/track/{order_id}` likewise answered for
any order id on a merchant the key could reach. Both are pinned here against a real table:
the handlers' own queries run on an in-memory SQLite copy of `orders`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import sqlalchemy as sa
from fastapi import BackgroundTasks, HTTPException

import routes.fulfillment_api as fa
from db.orders import orders

AGENT_A = "agent_aaaaaaaa"
AGENT_B = "agent_bbbbbbbb"
MERCHANT = "merch_shared01"


def _row(order_id: str, agent_id: Optional[str], *, status: str = "shipped", deleted: bool = False) -> Dict[str, Any]:
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    return {
        "order_id": order_id, "merchant_id": MERCHANT, "agent_id": agent_id,
        "customer_email": f"{order_id}-buyer.example", "shipping_address": {}, "items": [],
        "subtotal": 10, "total": 10, "currency": "USD", "fulfillment_status": status,
        "tracking_number": f"T-{order_id}", "carrier": "c", "shipped_at": now, "created_at": now,
        "payment_status": "paid", "is_deleted": deleted,
    }


ROWS = [
    _row("ord_a1", AGENT_A), _row("ord_a2", AGENT_A),
    _row("ord_b1", AGENT_B),
    _row("ord_none", None),
    _row("ord_a_pending", AGENT_A, status="pending"),
    _row("ord_a_deleted", AGENT_A, deleted=True),
]


@pytest.fixture()
def table(monkeypatch):
    engine = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    orders.to_metadata(meta)
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(meta.tables["orders"].insert(), ROWS)

    async def fetch_all(query, values=None):
        with engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(query)]

    async def get_order(order_id):
        return next((dict(r) for r in ROWS if r["order_id"] == order_id and not r["is_deleted"]), None)

    async def log_agent_request(**_kw):
        return None

    import db.database as dbm

    monkeypatch.setattr(dbm.database, "fetch_all", fetch_all)
    monkeypatch.setattr(fa, "get_order", get_order)
    monkeypatch.setattr(fa, "log_agent_request", log_agent_request)
    return engine


def _ctx(agent_id: str, allowed: Optional[List[str]] = None) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id, allowed_merchants=allowed,
        can_access_merchant=lambda m: allowed is None or m in allowed,
    )


def _in_transit(agent_id: str, **kw: Any) -> List[str]:
    import asyncio

    out = asyncio.run(fa.get_in_transit_orders(context=_ctx(agent_id, kw.pop("allowed", None)), **kw))
    return sorted(o["order_id"] for o in out["orders"])


def test_in_transit_lists_only_the_calling_agents_shipped_orders(table) -> None:
    assert _in_transit(AGENT_A) == ["ord_a1", "ord_a2"]
    assert _in_transit(AGENT_B) == ["ord_b1"]


def test_in_transit_never_returns_another_agents_buyer_email(table) -> None:
    import asyncio

    out = asyncio.run(fa.get_in_transit_orders(context=_ctx(AGENT_B)))
    emails = {o["customer_email"] for o in out["orders"]}
    assert emails == {"ord_b1-buyer.example"}


def test_in_transit_with_a_merchant_filter_is_still_the_callers_own(table) -> None:
    assert _in_transit(AGENT_B, merchant_id=MERCHANT) == ["ord_b1"]


def test_an_agent_with_no_orders_or_no_id_sees_nothing(table) -> None:
    assert _in_transit("agent_cccccccc") == []
    assert _in_transit("") == []


def _track(order_id: str, agent_id: str) -> Dict[str, Any]:
    import asyncio

    return asyncio.run(fa.track_order_fulfillment(order_id=order_id, background_tasks=BackgroundTasks(),
                                                  context=_ctx(agent_id)))


def test_track_answers_the_owner(table) -> None:
    assert _track("ord_a1", AGENT_A)["tracking"]["tracking_number"] == "T-ord_a1"


@pytest.mark.parametrize("order_id, agent_id", [("ord_b1", AGENT_A), ("ord_a1", AGENT_B), ("ord_none", AGENT_A)])
def test_track_refuses_an_order_the_caller_did_not_place_as_not_found(table, order_id: str, agent_id: str) -> None:
    with pytest.raises(HTTPException) as err:
        _track(order_id, agent_id)
    assert err.value.status_code == 404


@pytest.mark.parametrize(
    "order, agent_id, expected",
    [({"agent_id": AGENT_A}, AGENT_A, True), ({"agent_id": AGENT_A}, AGENT_B, False),
     ({"agent_id": None}, AGENT_A, False), ({"agent_id": ""}, "", False), ({}, AGENT_A, False),
     ({"agent_id": AGENT_A}, "", False)],
)
def test_owned_by_caller(order, agent_id, expected) -> None:
    assert fa._owned_by_caller(order, _ctx(agent_id)) is expected
