"""Every agent API request writes exactly ONE agent_usage_logs row.

UsageLoggerMiddleware writes a row for every authenticated /agent/v1 request, and many handlers also
call `log_agent_request`. Until this fix both inserted, so a request whose handler logged wrote two
rows (prod, 7 days to 2026-11-12: 864 usage rows for 432 GET /agent/v1/products/search requests), and
the per-agent rate limit and daily quota, which COUNT these rows, charged such a request twice.

These tests drive real routes from routes/agent_api.py through an app with the real middleware
installed. The rows land in an in-memory SQLite copy of `agent_usage_logs` (the shared
`database` object's calls are pointed at it for the test only), so the middleware's INSERT, the
handler's late UPDATE and the request_id unique constraint all run as SQL.

- GET /agent/v1/orders/{id}, found: the handler logs from a background task, after the middleware
  has written the row. One row, carrying the handler's merchant_id and order_id.
- GET /agent/v1/orders/{id}, not found: the handler awaits `log_agent_request` with the 404 and
  re-raises, before the middleware writes. One row, status 404, the handler's error_message.
- GET /agent/v1/analytics/summary: the handler does not log. One row (the middleware's).
- A handler that logs on a path outside /agent/v1, where the middleware writes nothing: one row.
- The row's request_id is minted by the middleware, never the caller's X-Request-Id: a repeated
  header still gives one row per request, each with its own handler fields. (The over-long header
  case needs Postgres, which enforces VARCHAR(100): tests/test_agent_usage_log_one_row_per_request_postgres.py.)
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI, Request
from sqlalchemy.pool import StaticPool

import db.agents as agents_db
import db.database as dbm
import routes.agent_api as agent_api
import routes.agent_auth as agent_auth
from middleware.usage_logger import UsageLoggerMiddleware

AGENT_ID = "agent_test"  # the agent the test-key bypass in get_agent_context authenticates
HEADERS = {"X-API-Key": "test-agent-key"}
MERCHANT = "merch_usage_rows_01"
OTHER_MERCHANT = "merch_usage_rows_02"
MINTED_REQUEST_ID = re.compile(r"^req_[0-9a-f]{32}$")


@pytest.fixture()
def usage_table(monkeypatch):
    """An in-memory agent_usage_logs; the shared database object's calls run against it."""
    engine = sa.create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    meta = sa.MetaData()
    agents_db.agent_usage_logs.to_metadata(meta)
    meta.create_all(engine)

    def _statement(query: Any, values: Any):
        if isinstance(query, str):
            return sa.text(query), dict(values or {})
        return query, None

    async def execute(query, values=None):
        stmt, params = _statement(query, values)
        with engine.begin() as conn:
            result = conn.execute(stmt, params) if params is not None else conn.execute(stmt)
            return result.rowcount

    async def fetch_all(query, values=None):
        stmt, params = _statement(query, values)
        with engine.begin() as conn:
            result = conn.execute(stmt, params) if params is not None else conn.execute(stmt)
            return [dict(r._mapping) for r in result]

    async def fetch_one(query, values=None):
        rows = await fetch_all(query, values)
        return rows[0] if rows else None

    async def fetch_val(query, values=None, column=0):
        row = await fetch_one(query, values)
        return list(row.values())[column] if row else None

    for name, fn in (
        ("execute", execute),
        ("fetch_all", fetch_all),
        ("fetch_one", fetch_one),
        ("fetch_val", fetch_val),
    ):
        monkeypatch.setattr(dbm.database, name, fn)
    # No failure webhooks and no agent stats writes leave this test.
    import middleware.usage_logger as usage_logger

    async def _no_webhook(payload):
        return None

    async def _no_stats(*_a, **_kw):
        return None

    monkeypatch.setattr(usage_logger, "_emit_request_failure_event", _no_webhook)
    monkeypatch.setattr(agent_auth, "update_agent_stats", _no_stats)
    yield engine
    engine.dispose()


def _rows(engine) -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r._mapping)
            for r in conn.execute(sa.text("SELECT * FROM agent_usage_logs ORDER BY id"))
        ]


async def _settle() -> None:
    # The middleware's INSERT and the handler's UPDATE are fire-and-forget tasks on this loop.
    for _ in range(50):
        await asyncio.sleep(0)


def _order(order_id: str) -> Dict[str, Any]:
    now = datetime(2026, 11, 20, 12, 0, 0)
    return {
        "order_id": order_id,
        "merchant_id": MERCHANT,
        "agent_id": AGENT_ID,
        "customer_email": "buyer@example.invalid",
        "items": [],
        "shipping_address": {},
        "status": "confirmed",
        "payment_status": "paid",
        "fulfillment_status": "pending",
        "total": 10,
        "currency": "USD",
        "created_at": now,
        "metadata": {},
    }


@pytest.fixture()
def app(monkeypatch, usage_table) -> FastAPI:
    known = {
        "ord_usage_found": _order("ord_usage_found"),
        "ord_usage_other": {**_order("ord_usage_other"), "merchant_id": OTHER_MERCHANT},
    }

    async def get_order(order_id):
        return known.get(order_id)

    monkeypatch.setattr(agent_api, "get_order", get_order)

    app = FastAPI()
    app.include_router(agent_api.router)

    @app.get("/agent/v2/usage-probe")
    async def handler_logs_outside_v1(context=Depends(agent_auth.get_agent_context)):
        await agent_auth.log_agent_request(context=context, status_code=200, merchant_id=MERCHANT)
        return {"ok": True}

    app.add_middleware(UsageLoggerMiddleware)
    return app


async def _get(app: FastAPI, path: str, **kw) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers={**HEADERS, **kw.pop("headers", {})}, **kw)
    await _settle()
    return response


async def test_a_handler_that_logs_after_the_response_writes_one_row_with_its_fields(app, usage_table):
    response = await _get(app, "/agent/v1/orders/ord_usage_found")

    assert response.status_code == 200
    rows = _rows(usage_table)
    assert len(rows) == 1, rows
    row = rows[0]
    assert (row["agent_id"], row["endpoint"], row["method"], row["status_code"]) == (
        AGENT_ID,
        "/agent/v1/orders/ord_usage_found",
        "GET",
        200,
    )
    # The handler's fields reached the middleware's row (late UPDATE by request_id).
    assert row["merchant_id"] == MERCHANT
    assert row["order_id"] == "ord_usage_found"
    assert row["request_id"]


async def test_a_failing_request_whose_handler_logs_writes_one_row(app, usage_table):
    response = await _get(app, "/agent/v1/orders/ord_usage_missing")

    assert response.status_code == 404
    rows = _rows(usage_table)
    assert len(rows) == 1, rows
    row = rows[0]
    assert (row["endpoint"], row["status_code"]) == ("/agent/v1/orders/ord_usage_missing", 404)
    # Awaited before the response: merged into the middleware's INSERT.
    assert row["error_message"] == "Order not found"


async def test_a_handler_that_does_not_log_writes_one_row(app, usage_table):
    response = await _get(app, "/agent/v1/analytics/summary")

    assert response.status_code == 200
    rows = _rows(usage_table)
    assert len(rows) == 1, rows
    assert (rows[0]["endpoint"], rows[0]["status_code"]) == ("/agent/v1/analytics/summary", 200)
    assert rows[0]["merchant_id"] is None


async def test_a_handler_that_logs_outside_v1_still_writes_its_one_row(app, usage_table):
    response = await _get(app, "/agent/v2/usage-probe")

    assert response.status_code == 200
    rows = _rows(usage_table)
    assert len(rows) == 1, rows
    assert (rows[0]["endpoint"], rows[0]["merchant_id"]) == ("/agent/v2/usage-probe", MERCHANT)


async def test_each_request_counts_once_toward_the_agents_rate_window(app, usage_table):
    """The per-agent rate limit and daily quota COUNT these rows: N requests are N, not up to 2N."""
    for path in (
        "/agent/v1/orders/ord_usage_found",
        "/agent/v1/orders/ord_usage_missing",
        "/agent/v1/analytics/summary",
        "/agent/v1/orders/ord_usage_found",
    ):
        await _get(app, path)

    rows = _rows(usage_table)
    assert len(rows) == 4, [(r["endpoint"], r["status_code"]) for r in rows]
    assert len({r["request_id"] for r in rows}) == 4
    assert {r["agent_id"] for r in rows} == {AGENT_ID}


async def test_a_handler_call_on_a_request_the_middleware_does_not_log_writes_the_handler_row(
    usage_table,
):
    """No agent on request.state (the middleware writes nothing): the handler's row is the row."""
    app = FastAPI()

    @app.get("/agent/v1/usage-probe-unattributed")
    async def unattributed(request: Request):
        context = SimpleNamespace(agent_id=AGENT_ID, request=request, response_time_ms=1, request_id=None)
        await agent_auth.log_agent_request(context=context, status_code=200, merchant_id=MERCHANT)
        return {"ok": True}

    app.add_middleware(UsageLoggerMiddleware)
    response = await _get(app, "/agent/v1/usage-probe-unattributed")

    assert response.status_code == 200
    rows = _rows(usage_table)
    assert len(rows) == 1, rows
    assert (rows[0]["endpoint"], rows[0]["merchant_id"]) == ("/agent/v1/usage-probe-unattributed", MERCHANT)


async def test_a_repeated_caller_request_id_still_gives_each_request_its_own_row(app, usage_table):
    """A retrying client that reuses one X-Request-Id: each request keeps its own row and its own
    handler fields (the caller's id used to be the row's id, so the unique request_id collapsed the
    requests into one row and the second's late UPDATE overwrote the first's merchant/order)."""
    shared = {"X-Request-Id": "client-retry-0001"}
    for order_id in ("ord_usage_found", "ord_usage_other", "ord_usage_found"):
        response = await _get(app, f"/agent/v1/orders/{order_id}", headers=shared)
        assert response.status_code == 200

    rows = _rows(usage_table)
    assert [(r["order_id"], r["merchant_id"]) for r in rows] == [
        ("ord_usage_found", MERCHANT),
        ("ord_usage_other", OTHER_MERCHANT),
        ("ord_usage_found", MERCHANT),
    ]
    assert len({r["request_id"] for r in rows}) == 3
    assert all(MINTED_REQUEST_ID.match(r["request_id"]) for r in rows), rows


async def test_the_rows_request_id_is_minted_not_the_callers_header(app, usage_table):
    long_header = "x" * 101  # longer than agent_usage_logs.request_id (VARCHAR(100))
    for path in ("/agent/v1/orders/ord_usage_found", "/agent/v1/orders/ord_usage_missing", "/agent/v1/analytics/summary"):
        await _get(app, path, headers={"X-Request-Id": long_header})

    rows = _rows(usage_table)
    assert len(rows) == 3, rows
    assert all(MINTED_REQUEST_ID.match(r["request_id"]) for r in rows), rows
