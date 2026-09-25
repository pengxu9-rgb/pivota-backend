"""`update_order_status(metadata=...)` merges into the stored metadata on real Postgres.

WHAT WAS WRONG. `update_order_status` reads the existing row with a raw TEXT query
(`SELECT ... metadata FROM orders ...`) and only merged `existing_raw` when it was a dict. On
this stack (`databases` 0.7 + asyncpg, and db/database.py registers no JSON codec) a JSON/JSONB
column read through a text query comes back as a Python `str`; only a SQLAlchemy-typed select
(`orders.select()`) decodes it. So `existing_metadata` was always `{}` in prod, and the
"additive" merge REPLACED the whole column: a webhook/aftercare handler writing
`metadata={"refund": ...}` erased `merchant_order`, `payment_recovery` and every other key.

WHY POSTGRES. The SQLite suite never sees the defect: the str-vs-dict shape is the asyncpg
driver's, so a SQLite (or mocked) test is green on the broken code. The fix is a local coerce,
NOT an asyncpg JSON codec — a codec double-encodes every `CAST(:x AS JSONB)` + `json.dumps`
write and makes SQLAlchemy-typed JSON reads raise.

Named `test_*_postgres.py`, so .github/workflows/postgres-dialect-gate.yml discovers it.

    createdb pivota_order_meta_merge_test
    DATABASE_URL=postgresql://localhost/pivota_order_meta_merge_test \\
        pytest tests/test_order_status_metadata_merge_postgres.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
MERCHANT = f"metamerge_{uuid.uuid4().hex[:8]}"

MERCHANT_ORDER = {"platform": "shopify", "id": "gid://shopify/Order/1", "name": "#1001"}
PAYMENT_RECOVERY = {"attempts": 2, "last_error": "card_declined"}


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write orders in {dbname!r}; throwaway only")


@pytest.fixture(autouse=True)
async def db():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.database import database
    from db.orders import orders

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Compiled from the model so the fixture cannot drift from the table update_order_status
    # writes. Never DROP: `orders` is shared with the other gate files in this process.
    try:
        await database.execute(str(CreateTable(orders).compile(dialect=postgresql.dialect())))
    except Exception:  # noqa: BLE001 -- another gate file already built the same table
        pass
    try:
        yield database
    finally:
        await database.execute("DELETE FROM orders WHERE merchant_id = :m", {"m": MERCHANT})
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _order(db, metadata_sql: str | None) -> str:
    """One order whose metadata is written by raw SQL, as the JSONB-cast writers store it."""
    order_id = f"ORD_{uuid.uuid4().hex[:12]}"
    await db.execute(
        "INSERT INTO orders (order_id, merchant_id, customer_email, shipping_address, items, "
        "subtotal, total, currency, status, payment_status, is_deleted, metadata) "
        "VALUES (:o, :m, 'buyer@example.com', CAST('{}' AS JSON), CAST('[]' AS JSON), "
        ":t, :t, 'USD', 'pending', 'paid', false, CAST(:md AS JSON))",
        {"o": order_id, "m": MERCHANT, "t": Decimal("10.00"), "md": metadata_sql},
    )
    return order_id


async def _metadata(db, order_id: str):
    # Decode in SQL-agnostic Python so the assertion does not depend on the driver's shape.
    row = await db.fetch_one(
        "SELECT metadata::text AS md FROM orders WHERE order_id = :o", {"o": order_id}
    )
    return None if row["md"] is None else json.loads(row["md"])


async def test_the_text_read_hands_back_a_str_which_is_why_the_merge_needs_a_coerce(db):
    """The premise, pinned: if a codec is ever registered this flips, and the coerce is moot."""
    order_id = await _order(db, json.dumps({"merchant_order": MERCHANT_ORDER}))
    row = await db.fetch_one("SELECT metadata FROM orders WHERE order_id = :o", {"o": order_id})
    assert isinstance(row["metadata"], str)


async def test_a_delta_metadata_write_keeps_the_keys_already_on_the_order(db):
    from db.orders import update_order_status

    order_id = await _order(
        db, json.dumps({"merchant_order": MERCHANT_ORDER, "payment_recovery": PAYMENT_RECOVERY})
    )

    await update_order_status(order_id, "processing", metadata={"x": 1})

    assert await _metadata(db, order_id) == {
        "merchant_order": MERCHANT_ORDER,
        "payment_recovery": PAYMENT_RECOVERY,
        "x": 1,
    }


async def test_the_callers_value_wins_on_a_shared_key(db):
    from db.orders import update_order_status

    order_id = await _order(db, json.dumps({"merchant_order": MERCHANT_ORDER, "x": 0}))

    await update_order_status(order_id, "processing", metadata={"x": 1})

    assert await _metadata(db, order_id) == {"merchant_order": MERCHANT_ORDER, "x": 1}


async def test_two_successive_delta_writes_accumulate(db):
    """The webhook shape: refund, then a later event, each passing only its own key."""
    from db.orders import update_order_status

    order_id = await _order(db, json.dumps({"merchant_order": MERCHANT_ORDER}))

    await update_order_status(order_id, "processing", metadata={"refund": {"amount": 5}})
    await update_order_status(order_id, "processing", metadata={"dispute": {"id": "dp_1"}})

    assert await _metadata(db, order_id) == {
        "merchant_order": MERCHANT_ORDER,
        "refund": {"amount": 5},
        "dispute": {"id": "dp_1"},
    }


@pytest.mark.parametrize(
    "stored",
    [
        None,  # SQL NULL
        "null",  # JSON null
        json.dumps("a string scalar"),  # what a double-encoded write leaves behind
        json.dumps([1, 2]),
    ],
    ids=["sql-null", "json-null", "json-string-scalar", "json-array"],
)
async def test_a_non_object_stored_value_is_replaced_by_the_delta(db, stored):
    from db.orders import update_order_status

    order_id = await _order(db, stored)

    await update_order_status(order_id, "processing", metadata={"x": 1})

    assert await _metadata(db, order_id) == {"x": 1}
