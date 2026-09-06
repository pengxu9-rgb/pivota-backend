"""Production-dialect gate for the Shoplazza cumulative-refund read-modify-write.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_shoplazza_refund_ledger_postgres.py

WHY REAL POSTGRES. Two things this receiver relies on are undecidable on SQLite:

* `recorded_refund_amount_cents` reads `amount_cents` out of the ledger's
  `payload` column. That column is `jsonb` here and plain `JSON` on SQLite, and
  the two dialects do not agree on whether a read hands back a mapping or the
  raw JSON text — reading `.get` off a string would silently make every
  "previously recorded" figure 0, which turns every partial refund into a
  duplicate of the whole cumulative total. The statement also has to PREPARE:
  a `synthetic IS FALSE` predicate plus a nullable `store_id` bind is exactly
  the shape that has produced `IndeterminateDatatypeError` in this repo before.
* `order_money_read_modify_write_lock` takes `pg_advisory_xact_lock`. That
  branch is DEAD on SQLite (`IS_POSTGRES` is False), so the only place its
  statement, its transaction nesting around `ingest_merchant_event_batch`, and
  the fact that it commits can be observed is here.

Empty-table gates are the norm in this job; this one populates two orders
because the arithmetic under test is about rows the receiver wrote itself.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

MERCHANT_ID = "merchant-sz"
STORE_ID = "store-sz"
ORDER_ID = "sz-order-1"
ORDER_REF = f"shoplazza:{ORDER_ID}"
WRITE_PATH = "shoplazza_webhook"

_TABLES = ("commerce_interactions", "commerce_interaction_events")

# This gate DROPS both ledger tables. Same convention as
# tests/test_commerce_ledger_write_path_authority_postgres.py: the "never point
# this at prod" promise is MADE true by refusing any non-throwaway database.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop the commerce ledger tables in database {dbname!r}; "
            "this gate must only run against a throwaway such as pivota_dialect_check"
        )


async def _drop_tables() -> None:
    from db.database import database

    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


@pytest.fixture(autouse=True)
async def _db():
    from sqlalchemy import create_engine

    from db.commerce_interactions import (
        commerce_interaction_events,
        commerce_interactions,
    )
    from db.database import database, metadata

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _drop_tables()
    engine = create_engine(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        metadata.create_all(
            engine,
            tables=[commerce_interactions, commerce_interaction_events],
            checkfirst=True,
        )
    finally:
        engine.dispose()
    yield
    await _drop_tables()
    if not was_connected and database.is_connected:
        await database.disconnect()


def _refund_order(cumulative: str):
    return {
        "order": {
            "id": ORDER_ID,
            "created_at": "2026-09-01T10:00:00Z",
            "updated_at": "2026-09-02T10:00:00Z",
            "currency": "USD",
            "total_price": "40.00",
            "financial_status": "partially_refunded",
            "total_refund_price": cumulative,
        }
    }


async def _deliver(cumulative: str, *, delivery_id: str, previously: int):
    """The mapper and the real ledger write, exactly as the receiver runs them."""
    from services.merchant_event_ingest_service import ingest_merchant_event_batch
    from services.shopline_family_event_adapter import map_shoplazza_webhook

    batch = map_shoplazza_webhook(
        _refund_order(cumulative),
        topic="orders/partially_refunded",
        delivery_id=delivery_id,
        store_id=STORE_ID,
        previously_recorded_refund_cents=previously,
    )
    return await ingest_merchant_event_batch(
        merchant_id=MERCHANT_ID,
        batch=batch,
        agent_identity_confidence="platform_asserted",
        write_path=WRITE_PATH,
    )


async def test_the_recorded_refund_read_prepares_and_totals_on_postgres():
    from services.commerce_interaction_service import recorded_refund_amount_cents

    scope = dict(
        merchant_id=MERCHANT_ID,
        store_id=STORE_ID,
        order_ref=ORDER_REF,
        write_path=WRITE_PATH,
    )
    # Executed against empty tables first: the statement has to PREPARE before
    # there is anything for it to find.
    assert await recorded_refund_amount_cents(**scope) == 0

    await _deliver("10.00", delivery_id="delivery-1", previously=0)
    assert await recorded_refund_amount_cents(**scope) == 1000

    await _deliver("25.00", delivery_id="delivery-2", previously=1000)
    # 1000 + 1500. If the jsonb read came back as text and `.get` silently
    # returned None, this would still be 0 and every later delivery would
    # re-record the whole cumulative total.
    assert await recorded_refund_amount_cents(**scope) == 2500

    # Every filter, exercised against real rows rather than an empty table.
    assert await recorded_refund_amount_cents(**{**scope, "store_id": "other"}) == 0
    assert await recorded_refund_amount_cents(**{**scope, "merchant_id": "other"}) == 0
    assert await recorded_refund_amount_cents(**{**scope, "write_path": "stripe_webhook"}) == 0
    assert await recorded_refund_amount_cents(**{**scope, "order_ref": "shoplazza:other"}) == 0
    # A NULL store_id is a different scope, not a wildcard, and the bind has to
    # compile to IS NULL rather than `= NULL` (which matches nothing but also
    # never fails).
    assert await recorded_refund_amount_cents(**{**scope, "store_id": None}) == 0


async def test_the_amount_survives_the_jsonb_round_trip_as_an_integer():
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interaction_events
    from db.database import database
    from services.commerce_interaction_service import _payload_mapping

    await _deliver("10.00", delivery_id="delivery-1", previously=0)
    rows = await database.fetch_all(
        select(commerce_interaction_events).where(
            commerce_interaction_events.c.event_type == "refund.succeeded"
        )
    )
    assert len(rows) == 1
    payload = _payload_mapping(dict(rows[0])["payload"])
    assert payload["amount_cents"] == 1000
    assert payload["refund_id"] == f"{ORDER_ID}:1000"
    assert payload["native_amount_semantics"] == "cumulative_refund_total_delta"


async def test_the_advisory_lock_branch_runs_and_the_write_inside_it_commits():
    """The branch SQLite can never reach: a real xact lock around read+write."""
    from db.database import database
    from services.commerce_interaction_service import (
        order_money_read_modify_write_lock,
        recorded_refund_amount_cents,
    )

    scope = dict(
        merchant_id=MERCHANT_ID,
        store_id=STORE_ID,
        order_ref=ORDER_REF,
        write_path=WRITE_PATH,
    )
    lock_scope = {key: value for key, value in scope.items() if key != "write_path"}
    async with order_money_read_modify_write_lock(scope="shoplazza_refund", **lock_scope):
        # Held, not merely requested: a no-op lock helper would leave this at 0
        # and the serialization claim would be vacuous.
        held = await database.fetch_val(
            """
            SELECT count(*) FROM pg_locks
             WHERE locktype = 'advisory'
               AND pid = pg_backend_pid()
               AND granted
            """
        )
        assert held >= 1
        previously = await recorded_refund_amount_cents(**scope)
        assert previously == 0
        await _deliver("10.00", delivery_id="delivery-1", previously=previously)

    # The nested transaction `record_commerce_event` opens on Postgres is a
    # SAVEPOINT inside ours; the row is only durable once the outer block above
    # has exited, so reading it here is the assertion that it committed.
    assert await recorded_refund_amount_cents(**scope) == 1000
    released = await database.fetch_val(
        """
        SELECT count(*) FROM pg_locks
         WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted
        """
    )
    assert released == 0

    # A second delivery, now serialized behind the first, sees the real baseline.
    async with order_money_read_modify_write_lock(scope="shoplazza_refund", **lock_scope):
        previously = await recorded_refund_amount_cents(**scope)
        assert previously == 1000
        await _deliver("25.00", delivery_id="delivery-2", previously=previously)
    assert await recorded_refund_amount_cents(**scope) == 2500
