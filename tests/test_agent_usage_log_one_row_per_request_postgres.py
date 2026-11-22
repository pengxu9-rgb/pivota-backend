"""One agent_usage_logs row per /agent/v1 request whatever X-Request-Id the caller sends, on Postgres.

    createdb usage_one_row_test
    DATABASE_URL=postgresql://localhost/usage_one_row_test \\
        .venv/bin/python -m pytest tests/test_agent_usage_log_one_row_per_request_postgres.py

WHY POSTGRES: `agent_usage_logs.request_id` is VARCHAR(100) UNIQUE, and only a real engine enforces
either. When the row's request_id was the caller's X-Request-Id header:
- a header longer than 100 characters failed the INSERT (swallowed: usage logging is best-effort),
  so the request wrote NO row and the per-agent rate limit and daily quota, which COUNT these rows,
  never saw it;
- a header repeated across requests (a retrying client) collided on the unique request_id, so the
  requests collapsed into one row, and the late UPDATE of the second request wrote its merchant and
  order onto the first request's row.
The middleware now mints every row's request_id (middleware/usage_logger.py). The SQLite file
(tests/test_agent_usage_log_one_row_per_request.py) holds the full route matrix.

The dialect gate runs every test_*_postgres.py file against ONE database: this file creates
agent_usage_logs only if absent, never drops it, and deletes only rows on its own endpoints.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Any, Dict, List

import httpx
import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate")

from fastapi import Depends, FastAPI  # noqa: E402

import routes.agent_api as agent_api  # noqa: E402
import routes.agent_auth as agent_auth  # noqa: E402
from middleware.usage_logger import UsageLoggerMiddleware  # noqa: E402

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
AGENT_ID = "agent_test"  # the agent the test-key bypass in get_agent_context authenticates
HEADERS = {"X-API-Key": "test-agent-key"}
ORDER_PREFIX = "ord_usage_one_row_pg_"
PROBE_PREFIX = "/agent/v1/usage-one-row-pg-probe/"
MINTED_REQUEST_ID = re.compile(r"^req_[0-9a-f]{32}$")
LONG_HEADER = "x" * 101  # one past agent_usage_logs.request_id's VARCHAR(100)
ORDERS = {
    f"{ORDER_PREFIX}a": "merch_usage_one_row_pg_a",
    f"{ORDER_PREFIX}b": "merch_usage_one_row_pg_b",
}
MISSING_ORDER = f"{ORDER_PREFIX}missing"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write test rows to {dbname!r}; throwaway only")


async def _delete_own_rows(database) -> None:
    await database.execute(
        "DELETE FROM agent_usage_logs WHERE endpoint LIKE :orders OR endpoint LIKE :probe",
        {"orders": f"/agent/v1/orders/{ORDER_PREFIX}%", "probe": f"{PROBE_PREFIX}%"},
    )


async def _own_rows(database) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        "SELECT endpoint, status_code, request_id, merchant_id, order_id FROM agent_usage_logs "
        "WHERE endpoint LIKE :orders OR endpoint LIKE :probe ORDER BY id",
        {"orders": f"/agent/v1/orders/{ORDER_PREFIX}%", "probe": f"{PROBE_PREFIX}%"},
    )
    return [dict(r._mapping) for r in rows]


@pytest.fixture
async def db(monkeypatch):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    import db.agents as agents_db
    from db.database import database

    _assert_throwaway_database()

    async def _no_webhook(payload):
        return None

    async def _no_stats(*_a, **_kw):
        return None

    import middleware.usage_logger as usage_logger

    monkeypatch.setattr(usage_logger, "_emit_request_failure_event", _no_webhook)
    monkeypatch.setattr(agent_auth, "update_agent_stats", _no_stats)

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    table = agents_db.agent_usage_logs
    await database.execute(str(CreateTable(table, if_not_exists=True).compile(dialect=postgresql.dialect())))
    for index in table.indexes:
        await database.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=postgresql.dialect())))
    await _delete_own_rows(database)
    try:
        yield database
    finally:
        try:
            await _delete_own_rows(database)
        except Exception:  # noqa: BLE001
            pass
        # `databases` shares one connection process-wide: disconnect only what this fixture opened.
        if not was_connected and database.is_connected:
            await database.disconnect()


def _order(order_id: str) -> Dict[str, Any]:
    return {
        "order_id": order_id,
        "merchant_id": ORDERS[order_id],
        "agent_id": AGENT_ID,
        "customer_email": "buyer@example.invalid",
        "items": [],
        "shipping_address": {},
        "status": "confirmed",
        "payment_status": "paid",
        "fulfillment_status": "pending",
        "total": 10,
        "currency": "USD",
        "created_at": datetime(2026, 11, 20, 12, 0, 0),
        "metadata": {},
    }


@pytest.fixture
def app(monkeypatch, db) -> FastAPI:
    async def get_order(order_id):
        return _order(order_id) if order_id in ORDERS else None

    monkeypatch.setattr(agent_api, "get_order", get_order)

    app = FastAPI()
    app.include_router(agent_api.router)

    @app.get(PROBE_PREFIX + "{tag}")
    async def probe_that_does_not_log(tag: str, context=Depends(agent_auth.get_agent_context)):
        return {"ok": tag}

    app.add_middleware(UsageLoggerMiddleware)
    return app


async def _get(app: FastAPI, path: str, request_id: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers={**HEADERS, "X-Request-Id": request_id})
    # The INSERT and the late UPDATE are fire-and-forget tasks; on a real connection they need a
    # few real round trips, not just loop turns.
    for _ in range(40):
        await asyncio.sleep(0.01)
    return response


async def test_a_caller_request_id_over_100_chars_still_leaves_one_row_per_request(app, db):
    found, other = list(ORDERS)
    for path, status in (
        (f"/agent/v1/orders/{found}", 200),  # the handler logs late: UPDATE after the INSERT
        (f"/agent/v1/orders/{MISSING_ORDER}", 404),  # the handler logs before the response: merged INSERT
        (f"{PROBE_PREFIX}long", 200),  # the handler does not log: the middleware's row alone
    ):
        response = await _get(app, path, LONG_HEADER)
        assert response.status_code == status, (path, response.text)

    rows = await _own_rows(db)
    assert [(r["endpoint"], r["status_code"]) for r in rows] == [
        (f"/agent/v1/orders/{found}", 200),
        (f"/agent/v1/orders/{MISSING_ORDER}", 404),
        (f"{PROBE_PREFIX}long", 200),
    ], rows
    assert (rows[0]["merchant_id"], rows[0]["order_id"]) == (ORDERS[found], found)
    assert all(MINTED_REQUEST_ID.match(r["request_id"]) for r in rows), rows


async def test_a_repeated_caller_request_id_keeps_each_requests_row_and_fields(app, db):
    found, other = list(ORDERS)
    sequence = [found, other, found, other, found]
    for order_id in sequence:
        response = await _get(app, f"/agent/v1/orders/{order_id}", "client-retry-0001")
        assert response.status_code == 200, response.text

    rows = await _own_rows(db)
    assert [(r["order_id"], r["merchant_id"]) for r in rows] == [(o, ORDERS[o]) for o in sequence], rows
    assert len({r["request_id"] for r in rows}) == len(sequence)
    assert all(MINTED_REQUEST_ID.match(r["request_id"]) for r in rows), rows
