"""The ``refund.succeeded`` ledger event for an attribution edge that carries metadata, on real Postgres.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_refund_event_test \\
        .venv/bin/python -m pytest tests/test_attribution_refund_event_postgres.py

WHY REAL POSTGRES. `_ATTRIBUTE_REFUND_QUERY` is a text query, so its RETURNING
row carries no SQLAlchemy column types, and db/database.py registers no asyncpg
JSON codec: the edge's JSONB ``metadata`` comes back as a *str*. The event
builder spread it with ``**``, which raised ``TypeError: 'str' object is not a
mapping`` for every edge whose metadata was not NULL, before the ledger write
was ever reached. tests/test_attach_refund_attribution_cents.py fakes the
database and hands back a dict, so it cannot see this.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = ("commerce_interaction_events", "commerce_interactions", "commerce_attribution_edges")

# The shape the closure writes (services/commerce_attribution_service.py, close_external_order_conversion).
_EDGE_METADATA = {
    "agent_source": "partner_purchase",
    "partner_provenance": {"partner_reported": True, "purchase_id": "rp_abc"},
    "converting_merchant_id": "brand.example",
}


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _build_schema(database):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.commerce_attribution import commerce_attribution_edges
    from db.commerce_interactions import commerce_interaction_events, commerce_interactions

    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for table in (commerce_attribution_edges, commerce_interactions, commerce_interaction_events):
        await database.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
    # Migration 109's columns, which the refund UPDATE writes.
    await database.execute(
        "ALTER TABLE commerce_attribution_edges "
        "ADD COLUMN refund_amount_cents BIGINT NOT NULL DEFAULT 0, ADD COLUMN refunded_at TIMESTAMPTZ"
    )


@pytest.fixture(autouse=True)
async def db():
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _build_schema(database)
    try:
        yield database
    finally:
        # DROP, not DELETE: these are reduced shapes (no migration-only indexes); a later gate
        # file's `metadata.create_all(checkfirst=True)` would otherwise skip them.
        for table in _TABLES:
            try:
                await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            except Exception:  # noqa: BLE001 -- best-effort cleanup must not mask the test result
                continue
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _insert_edge(db, *, edge_id="edge_1", order_id="ord_1", metadata=_EDGE_METADATA):
    await db.execute(
        "INSERT INTO commerce_attribution_edges "
        "(edge_id, merchant_id, click_id, order_id, surface, refund_ids, refund_count, "
        " refunded_amount, metadata) "
        "VALUES (:edge_id, 'brand.example', 'clk_1', :order_id, 'agent', '[]'::jsonb, 0, 0, "
        " CAST(:metadata AS JSONB))",
        {"edge_id": edge_id, "order_id": order_id,
         "metadata": json.dumps(metadata) if metadata is not None else None},
    )


async def _refund_events(db):
    rows = await db.fetch_all(
        "SELECT merchant_id, upstream_idempotency_key, payload FROM commerce_interaction_events "
        "WHERE event_type = 'refund.succeeded'"
    )
    return [
        {**dict(r), "payload": r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])}
        for r in rows
    ]


async def test_the_returning_row_really_hands_back_metadata_as_a_str(db):
    """The premise. If this ever flips (a JSON codec in db/database.py), the coercion is moot."""
    from services.commerce_attribution_service import apply_attribution_refund_rows

    await _insert_edge(db)
    rows = await apply_attribution_refund_rows(order_id="ord_1", refund_id="re_1", amount="12.50")
    assert len(rows) == 1
    assert isinstance(rows[0]["metadata"], str)


async def test_a_refund_on_an_edge_with_metadata_records_the_event(db):
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _insert_edge(db)
    first = await attach_refund_to_attribution_edge(order_id="ord_1", refund_id="re_1", amount="12.50")

    assert first is not None and first["edge_count"] == 1
    events = await _refund_events(db)
    assert len(events) == 1
    event = events[0]
    assert event["merchant_id"] == "brand.example"
    assert event["upstream_idempotency_key"] == "refund:re_1"
    payload = event["payload"]
    assert payload["order_id"] == "ord_1" and payload["refund_id"] == "re_1"
    assert payload["refunded_amount"] == "12.50" and payload["edge_count"] == 1
    # The edge's own metadata still rides along, as it did for the dict-shaped rows the spread assumed.
    assert payload["agent_source"] == "partner_purchase"
    assert payload["partner_provenance"] == {"partner_reported": True, "purchase_id": "rp_abc"}


async def test_the_partner_adjustment_path_records_the_event_too(db):
    """The second caller: the adapter runs the UPDATE in its own transaction, then emits."""
    from services.commerce_attribution_service import (
        apply_attribution_refund_rows,
        emit_attribution_refund_event,
    )

    await _insert_edge(db)
    rows = await apply_attribution_refund_rows(order_id="ord_1", refund_id="reap:evt_1", amount="15")
    await emit_attribution_refund_event(rows, order_id="ord_1", refund_id="reap:evt_1", amount="15")

    events = await _refund_events(db)
    assert [e["upstream_idempotency_key"] for e in events] == ["refund:reap:evt_1"]
    assert events[0]["payload"]["partner_provenance"]["purchase_id"] == "rp_abc"


async def test_an_edge_with_null_metadata_still_records_the_event(db):
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _insert_edge(db, metadata=None)
    await attach_refund_to_attribution_edge(order_id="ord_1", refund_id="re_1", amount="5")

    events = await _refund_events(db)
    assert len(events) == 1 and events[0]["payload"]["order_id"] == "ord_1"


async def test_a_redelivered_refund_records_one_event(db):
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _insert_edge(db, edge_id="edge_1")
    await _insert_edge(db, edge_id="edge_2")
    for _ in range(2):
        await attach_refund_to_attribution_edge(order_id="ord_1", refund_id="re_1", amount="5")

    events = await _refund_events(db)
    assert len(events) == 1
    assert events[0]["payload"]["edge_count"] == 2
