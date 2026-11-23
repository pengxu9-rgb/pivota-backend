"""Against real Postgres: the developer portal reads its agent's orders and order timeline with the
portal session (GET /agents/{agent_id}/orders and /orders/events), not an API key.

Both portal views used GET /agent/v1/orders(/events) with a key the browser no longer holds, so they
failed for every developer. Pinned here:
- the owning agent's session reads its own orders (newest first) and events (oldest first), in the
  key routes' shapes, with deleted orders left out;
- another agent's session and a claim-less token are refused 403; staff are admitted;
- a read writes no agent_usage_logs row (a portal read is not an agent API call).

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_x_test_dialect_check \\
        .venv/bin/python -m pytest tests/test_agent_portal_orders_postgres.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
AGENT_ID = "agent_portal_orders_pg"
OTHER_AGENT_ID = "agent_portal_orders_pg_other"
MERCHANT_ID = "merch_portal_orders_pg"
OWN = {"role": "agent", "agent_id": AGENT_ID, "email": "dev@example.com"}
FOREIGN = {"role": "agent", "agent_id": OTHER_AGENT_ID, "email": "other@example.com"}
CLAIMLESS = {"role": "agent", "email": "dev@example.com"}
STAFF = {"role": "admin", "email": "ops@example.com"}

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
# order_id -> (agent_id, minutes after T0, is_deleted)
ORDERS = {
    "ord_portal_pg_old": (AGENT_ID, 0, False),
    "ord_portal_pg_new": (AGENT_ID, 10, False),
    "ord_portal_pg_gone": (AGENT_ID, 20, True),
    "ord_portal_pg_other": (OTHER_AGENT_ID, 30, False),
}


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write test rows to {dbname!r}; throwaway only")


async def _delete_own_rows(database):
    ids = list(ORDERS)
    for order_id in ids:
        await database.execute("DELETE FROM order_events WHERE order_id = :o", {"o": order_id})
        await database.execute("DELETE FROM orders WHERE order_id = :o", {"o": order_id})
    for agent_id in (AGENT_ID, OTHER_AGENT_ID):
        await database.execute("DELETE FROM agent_usage_logs WHERE agent_id = :a", {"a": agent_id})


async def _usage_rows(database):
    row = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM agent_usage_logs WHERE agent_id IN (:a, :b)",
        {"a": AGENT_ID, "b": OTHER_AGENT_ID},
    )
    return int(row["n"])


@pytest.fixture
async def db():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    import db.agents as agents_db
    from db.database import database
    from db.orders import orders as orders_table
    from db.products import order_events

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Create-if-absent, never drop: the dialect gate shares one database across test files.
    for table in (orders_table, order_events, agents_db.agent_usage_logs):
        await database.execute(str(CreateTable(table, if_not_exists=True).compile(dialect=postgresql.dialect())))
        for index in table.indexes:
            await database.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=postgresql.dialect())))
    await _delete_own_rows(database)

    for order_id, (agent_id, minutes, deleted) in ORDERS.items():
        created = T0 + timedelta(minutes=minutes)
        await database.execute(
            """
            INSERT INTO orders (order_id, merchant_id, customer_email, shipping_address, items, subtotal,
                                total, currency, status, payment_status, agent_id, is_deleted, created_at)
            VALUES (:order_id, :merchant_id, 'buyer@example.com', '{}', '[]', 12.5, 12.5, 'USD',
                    'pending', 'unpaid', :agent_id, :deleted, :created)
            """,
            {"order_id": order_id, "merchant_id": MERCHANT_ID, "agent_id": agent_id, "deleted": deleted, "created": created},
        )
        await database.execute(
            """
            INSERT INTO order_events (event_type, merchant_id, order_id, total_amount, currency, status, created_at)
            VALUES ('order_created', :merchant_id, :order_id, 12.5, 'USD', 'pending', :created)
            """,
            {"merchant_id": MERCHANT_ID, "order_id": order_id, "created": created.replace(tzinfo=None)},
        )
    try:
        yield database
    finally:
        try:
            await _delete_own_rows(database)
        except Exception:  # noqa: BLE001
            pass
        if not was_connected and database.is_connected:
            await database.disconnect()


def _client_as(user):
    import httpx
    from fastapi import FastAPI

    from routes.agent_portal_orders import router
    from utils.auth import get_current_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_the_owner_reads_its_own_orders_newest_first_without_deleted_ones(db):
    async with _client_as(OWN) as client:
        resp = await client.get(f"/agents/{AGENT_ID}/orders")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert [o["order_id"] for o in body["orders"]] == ["ord_portal_pg_new", "ord_portal_pg_old"]
    assert body["total"] == 2
    # The key route's shape, so the portal's existing renderer reads it as is.
    assert set(body["orders"][0]) == {"order_id", "merchant_id", "status", "payment_status", "total", "created_at"}
    assert body["orders"][0]["total"] == "12.50"
    assert await _usage_rows(db) == 0


@pytest.mark.asyncio
async def test_the_owner_reads_its_own_timeline_oldest_first_without_deleted_orders(db):
    async with _client_as(OWN) as client:
        resp = await client.get(f"/agents/{AGENT_ID}/orders/events")
        first = resp.json()
        rest = (await client.get(f"/agents/{AGENT_ID}/orders/events", params={"after_id": first["events"][0]["id"]})).json()

    assert resp.status_code == 200, resp.text
    assert first["status"] == "success"
    assert [e["order_id"] for e in first["events"]] == ["ord_portal_pg_old", "ord_portal_pg_new"]
    assert first["last_id"] == first["events"][-1]["id"]
    assert [e["order_id"] for e in rest["events"]] == ["ord_portal_pg_new"]  # after_id pages forward
    assert await _usage_rows(db) == 0


@pytest.mark.asyncio
async def test_filters_narrow_the_list(db):
    async with _client_as(OWN) as client:
        by_status = (await client.get(f"/agents/{AGENT_ID}/orders", params={"status": "completed"})).json()
        by_order = (await client.get(f"/agents/{AGENT_ID}/orders/events", params={"order_id": "ord_portal_pg_old"})).json()
        paged = (await client.get(f"/agents/{AGENT_ID}/orders", params={"limit": 1, "offset": 1})).json()

    assert by_status["orders"] == []
    assert [e["order_id"] for e in by_order["events"]] == ["ord_portal_pg_old"]
    assert [o["order_id"] for o in paged["orders"]] == ["ord_portal_pg_old"]


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [FOREIGN, CLAIMLESS], ids=["foreign_agent", "claimless_token"])
@pytest.mark.parametrize("path", ["/orders", "/orders/events"])
async def test_another_agent_or_a_claimless_token_is_refused(db, user, path):
    async with _client_as(user) as client:
        resp = await client.get(f"/agents/{AGENT_ID}{path}")

    assert resp.status_code == 403, resp.text
    assert "ord_portal_pg" not in resp.text


@pytest.mark.asyncio
async def test_staff_read_any_agents_orders(db):
    async with _client_as(STAFF) as client:
        resp = await client.get(f"/agents/{OTHER_AGENT_ID}/orders")

    assert resp.status_code == 200, resp.text
    assert [o["order_id"] for o in resp.json()["orders"]] == ["ord_portal_pg_other"]
