"""Production-dialect gate for the paid-orders-missing-a-merchant-order counter.

`_count_paid_orders_missing_merchant_order_best_effort` is the signal that pages
on a buyer being charged while the merchant never receives the order. Its SQL is
built as a function-local string and handed to `_count_sql_best_effort`, so the
repo's static PREPARE sweep cannot follow it, and a typo'd column would not fail
loudly — `_count_sql_best_effort` swallows the error into `available: False`,
which falls through to the sample-window path.

So this EXECUTES the real counter against Postgres, over rows built from the
repo's own `orders` Table object (never hand-written DDL), asserting each
conjunct actually excludes what it claims to.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_paid_missing_merchant_order_count_postgres.py

Never point this at prod.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

MERCHANT = f"merch_gate_{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
async def _db():
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import postgresql

    from db.database import database
    from db.orders import orders

    await database.connect()
    # The repo's own schema object, compiled for Postgres — so this fixture can
    # never drift from the table the counter actually queries.
    ddl = str(CreateTable(orders).compile(dialect=postgresql.dialect()))
    try:
        await database.execute(ddl)
    except Exception:
        # Another gate file in this process already created it; same metadata,
        # so the shape is identical. Never DROP — this DB is shared.
        pass
    try:
        yield database
    finally:
        await database.execute(
            "DELETE FROM orders WHERE merchant_id = :m", {"m": MERCHANT}
        )
        await database.disconnect()


async def _insert(order_id, *, paid_ago, shopify_order_id=None, metadata=None,
                  payment_status="paid"):
    from db.database import database
    from db.orders import orders

    now = datetime.now(timezone.utc)
    await database.execute(
        orders.insert().values(
            order_id=order_id,
            merchant_id=MERCHANT,
            # Every NOT NULL column without a default. Sourced from the real
            # table object, so a schema change surfaces here rather than being
            # papered over by a partial fixture.
            customer_email="gate@example.test",
            shipping_address={},
            items=[],
            subtotal=10,
            payment_status=payment_status,
            status="paid",
            total=10,
            currency="USD",
            shopify_order_id=shopify_order_id,
            metadata=metadata,
            is_deleted=False,
            created_at=now - paid_ago,
            paid_at=now - paid_ago,
        )
    )


async def _count(monkeypatch):
    """Force the Postgres branch to be the ONLY thing that can answer.

    `_count_paid_orders_missing_merchant_order_best_effort` falls through to a
    Python sample-window path whenever the SQL raises, and that fallback applies
    the same age and linked-platform filters. So without this the gate cannot
    tell whether the SQL ran or merely failed quietly into the fallback — it
    would stay green with the executed statement completely broken, which is the
    exact failure this file exists to prevent.
    """
    import routes.order_routes as order_routes

    async def fallback_must_not_be_reached(**kwargs):
        raise AssertionError(
            "fell through to the sample-window path — the Postgres SQL did not "
            "answer, so this gate proved nothing about it"
        )

    monkeypatch.setattr(
        order_routes,
        "_fetch_paid_orders_missing_merchant_order",
        fallback_must_not_be_reached,
    )
    assert order_routes.IS_POSTGRES is True, "gate must run against Postgres"

    return await order_routes._count_paid_orders_missing_merchant_order_best_effort(
        merchant_id=MERCHANT
    )


async def test_the_real_sql_executes_and_counts_only_genuine_gaps(monkeypatch):
    """Each conjunct is exercised by a row it must exclude."""
    old = timedelta(hours=6)

    # The one that should count: paid, old, no merchant order anywhere.
    await _insert("ORD_GATE_STUCK", paid_ago=old)
    # Still inside the sync window — healthy, must not page.
    await _insert("ORD_GATE_IN_FLIGHT", paid_ago=timedelta(seconds=5))
    # Delivered to Shopify.
    await _insert("ORD_GATE_ON_SHOPIFY", paid_ago=old, shopify_order_id="shop-1")
    # Delivered to a non-Shopify platform: shopify_order_id is empty, but the
    # merchant HAS the order. This is the conjunct that stops false pages.
    await _insert(
        "ORD_GATE_ON_WOO",
        paid_ago=old,
        metadata={"merchant_order": {"platform_order_id": "woo-9"}},
    )
    # Refunded orders leave the population entirely.
    await _insert("ORD_GATE_REFUNDED", paid_ago=old, payment_status="refunded")

    result = await _count(monkeypatch)

    assert result["available"] is True, result
    assert result["count"] == 1, result


async def test_a_failure_marker_does_not_change_the_count(monkeypatch):
    """The whole point of this counter: it must not require the marker that
    `paid_merchant_order_failed_count` requires."""
    old = timedelta(hours=6)
    await _insert(
        "ORD_GATE_MARKED",
        paid_ago=old,
        metadata={"merchant_order": {"status": "paid_merchant_order_failed"}},
    )
    await _insert("ORD_GATE_UNMARKED", paid_ago=old)

    result = await _count(monkeypatch)

    assert result["available"] is True, result
    assert result["count"] == 2, result


async def test_an_empty_population_counts_zero_rather_than_erroring(monkeypatch):
    result = await _count(monkeypatch)
    assert result == {"count": 0, "available": True}, result
