"""Production-dialect gate for the Squarespace cumulative-refund arithmetic.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_squarespace_ledger_postgres.py

WHY REAL POSTGRES. Three things this integration relies on are undecidable on
SQLite, and one of them is NEW here.

* `recorded_refund_amount_cents` now takes a SEQUENCE of write paths for
  Squarespace, which compiles to `write_path IN (...)` instead of `= $n`. That
  is a different prepared statement from the one the Shoplazza gate covers, and
  it sits next to a `synthetic IS FALSE` predicate and a nullable `store_id`
  bind — exactly the shape that has produced `IndeterminateDatatypeError` in
  this repo before. Nothing on SQLite can tell whether it PREPAREs.
* the read pulls `amount_cents` and `currency` out of the ledger's `payload`
  column, which is `jsonb` here and plain `JSON` on SQLite. The two dialects
  disagree about whether a read hands back a mapping or the raw JSON text, and
  reading `.get` off a string would silently make every baseline 0 — turning
  every re-observation of an order into a duplicate of its whole cumulative
  refund total.
* `order_money_read_modify_write_lock` takes `pg_advisory_xact_lock`. That
  branch is DEAD on SQLite (`IS_POSTGRES` is False), so the only place its
  statement, its transaction nesting around `ingest_merchant_event_batch`, and
  the fact that it actually EXCLUDES another backend can be observed is here.

HOW IT IS DRIVEN. Through the real receiver (a real HMAC) and the real sweep,
the same way `tests/test_squarespace_ledger_end_to_end.py` drives them on
SQLite. Composing lock -> read -> map -> ingest inside the test would make it
blind to every mistake the ingresses themselves could make.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

SECRET = "sq-subscription-secret"
MERCHANT_ID = "merchant-sq"
STORE_ID = "store-sq"
WEBSITE_ID = "site-aaaa"
ORDER_ID = "sq-order-1"
ORDER_REF = f"squarespace:{ORDER_ID}"
PATH = f"/webhooks/squarespace/{STORE_ID}"
CURRENCY = "USD"
WEBHOOK = "squarespace_webhook"
SWEEP = "squarespace_reconciliation"
BOTH = (WEBHOOK, SWEEP)

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
_TABLES = ("commerce_interactions", "commerce_interaction_events")

# This gate DROPS both ledger tables. Same convention as its siblings: the
# "never point this at prod" promise is MADE true by refusing any non-throwaway.
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


# -- fixtures ---------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def _order(*, refunded=None, modified=None):
    order = {
        "id": ORDER_ID,
        "orderNumber": "00042",
        "createdOn": _iso(_NOW - timedelta(days=2)),
        "modifiedOn": modified or _iso(_NOW - timedelta(days=2)),
        "testmode": False,
        "fulfillmentStatus": "PENDING",
        "grandTotal": {"value": "40.00", "currency": CURRENCY},
    }
    if refunded is not None:
        order["refundedTotal"] = {"value": refunded, "currency": CURRENCY}
    return order


_CREDENTIALS = {
    "api_key": "sq-api-key",
    "website_id": WEBSITE_ID,
    "webhook_secret": SECRET,
}

_SCOPE = dict(
    merchant_id=MERCHANT_ID,
    store_id=STORE_ID,
    order_ref=ORDER_REF,
)


def _webhook_app(monkeypatch, order):
    from fastapi import FastAPI

    from routes import squarespace_webhooks as route

    class FakeStores:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "domain": "shop.example",
                "api_key": json.dumps(_CREDENTIALS),
            }

    async def fake_fetch(*, access_token, order_id, **kwargs):
        return order

    monkeypatch.setattr(route, "database", FakeStores())
    monkeypatch.setattr(route, "fetch_squarespace_order", fake_fetch)
    route._SEEN_NOTIFICATIONS.clear()

    app = FastAPI()
    app.include_router(route.router)
    return app


async def _deliver(app, *, notification_id):
    """One signed delivery, driven in THIS event loop.

    `httpx.ASGITransport` rather than `TestClient`, for the reason the Shoplazza
    gate records: `TestClient` drives the app from a portal thread with its own
    event loop, while the asyncpg pool under `db.database.database` is bound to
    this one, and the first delivery through it does not fail — it HANGS.
    """
    import httpx

    raw = json.dumps(
        {
            "id": notification_id,
            "topic": "order.update",
            "websiteId": WEBSITE_ID,
            "subscriptionId": "sub-1",
            "data": {"orderId": ORDER_ID},
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            PATH,
            content=raw,
            headers={
                "Squarespace-Signature": signature,
                "Content-Type": "application/json",
            },
        )


async def _sweep(monkeypatch, orders):
    from services import squarespace_order_sweep as sweep

    class _Response:
        status_code = 200
        content = b"{}"

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        async def get(self, url, headers=None, params=None):
            # The sweep proves the credential's SITE once per run before it
            # lists anything.
            if "/authorization/website" in str(url):
                return _Response({"id": WEBSITE_ID})
            return _Response({"result": orders, "pagination": {}})

        async def aclose(self):
            return None

    async def fake_find(store_id):
        return {
            "store_id": STORE_ID,
            "merchant_id": MERCHANT_ID,
            "domain": "shop.example",
            "api_key": json.dumps(_CREDENTIALS),
        }

    async def fake_merge(*, store_id, updates=None, **kwargs):
        return {**_CREDENTIALS, **(updates or {})}

    monkeypatch.setattr(sweep, "find_squarespace_store", fake_find)
    monkeypatch.setattr(sweep, "merge_squarespace_credentials", fake_merge)
    return await sweep.sweep_squarespace_store(store_id=STORE_ID, client=_Client())


async def _refund_rows():
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interaction_events
    from db.database import database
    from services.commerce_interaction_service import _payload_mapping

    rows = [
        dict(row)
        for row in await database.fetch_all(
            select(commerce_interaction_events).where(
                commerce_interaction_events.c.event_type == "refund.succeeded"
            )
        )
    ]
    for row in rows:
        row["payload"] = _payload_mapping(row["payload"])
    return sorted(rows, key=lambda row: row["payload"]["amount_cents"])


# -- a genuinely separate backend -------------------------------------------


def _lock_key(order_ref: str) -> str:
    """The exact string `order_money_read_modify_write_lock` hashes."""
    return f"squarespace_refund|{MERCHANT_ID}|{STORE_ID}|{order_ref}"


async def _another_backend_can_take(order_ref: str) -> bool:
    """Could a DIFFERENT Postgres session take this order's advisory lock now?

    The only shape of assertion that can distinguish a real lock from a no-op:
    `pg_locks WHERE pid = pg_backend_pid()` is a statement about our OWN
    backend, and serialization is by definition a claim about other backends.
    `pg_try_advisory_xact_lock` never waits, so a held lock answers False rather
    than deadlocking the test.
    """
    import asyncpg

    connection = await asyncpg.connect(
        DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        return bool(
            await connection.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtext($1))", _lock_key(order_ref)
            )
        )
    finally:
        await connection.close()


# -- the tests ---------------------------------------------------------------


async def test_the_multi_write_path_read_prepares_and_totals_on_postgres(monkeypatch):
    """The `IN (...)` form is a different prepared statement from the `=` form.

    Both are exercised against EMPTY tables first: the statement has to PREPARE
    before there is anything for it to find, which is where an
    IndeterminateDatatypeError would surface.
    """
    from services.commerce_interaction_service import recorded_refund_amount_cents

    assert await recorded_refund_amount_cents(**_SCOPE, write_path=BOTH) == 0
    assert await recorded_refund_amount_cents(**_SCOPE, write_path=WEBHOOK) == 0
    assert (
        await recorded_refund_amount_cents(**_SCOPE, write_path=BOTH, currency=CURRENCY)
        == 0
    )
    # A NULL store_id is a different scope, not a wildcard: the bind has to
    # compile to IS NULL rather than `= NULL`, which matches nothing but also
    # never fails.
    assert (
        await recorded_refund_amount_cents(
            **{**_SCOPE, "store_id": None}, write_path=BOTH
        )
        == 0
    )

    app = _webhook_app(monkeypatch, _order(refunded="10.00"))
    delivered = await _deliver(app, notification_id="n-1")
    assert delivered.status_code == 200, delivered.text

    await _sweep(
        monkeypatch,
        [_order(refunded="25.00", modified=_iso(_NOW - timedelta(days=1)))],
    )

    # 1000 from the webhook + 1500 from the sweep. If the jsonb read came back
    # as text and `.get` silently returned None, this would be 0 and every
    # observation would re-record the whole cumulative total.
    assert await recorded_refund_amount_cents(**_SCOPE, write_path=BOTH) == 2500
    # Each path alone sees only its own row — which is exactly why the callers
    # must pass BOTH.
    assert await recorded_refund_amount_cents(**_SCOPE, write_path=WEBHOOK) == 1000
    assert await recorded_refund_amount_cents(**_SCOPE, write_path=SWEEP) == 1500
    # Every other filter, exercised against real rows rather than an empty table.
    assert (
        await recorded_refund_amount_cents(
            **{**_SCOPE, "store_id": "other"}, write_path=BOTH
        )
        == 0
    )
    assert (
        await recorded_refund_amount_cents(
            **{**_SCOPE, "merchant_id": "other"}, write_path=BOTH
        )
        == 0
    )
    assert (
        await recorded_refund_amount_cents(**_SCOPE, write_path=("stripe_webhook",)) == 0
    )
    assert (
        await recorded_refund_amount_cents(
            **{**_SCOPE, "order_ref": "squarespace:other"}, write_path=BOTH
        )
        == 0
    )
    # The currency narrowing reads out of the same jsonb payload.
    assert (
        await recorded_refund_amount_cents(**_SCOPE, write_path=BOTH, currency="usd")
        == 2500
    )
    assert (
        await recorded_refund_amount_cents(**_SCOPE, write_path=BOTH, currency="EUR") == 0
    )


async def test_a_webhook_then_a_sweep_record_their_deltas_and_one_paid_row(monkeypatch):
    """The receiver's and the sweep's own arithmetic, on the production dialect."""
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interaction_events, commerce_interactions
    from db.database import database

    app = _webhook_app(monkeypatch, _order(refunded="10.00"))
    assert (await _deliver(app, notification_id="n-1")).status_code == 200

    swept = await _sweep(
        monkeypatch,
        [_order(refunded="25.00", modified=_iso(_NOW - timedelta(days=1)))],
    )
    assert swept["accepted"] == 1  # the refund delta only; the rest deduped

    rows = await _refund_rows()
    assert [row["payload"]["amount_cents"] for row in rows] == [1000, 1500]
    assert [row["payload"]["refund_id"] for row in rows] == [
        f"{ORDER_ID}:1000",
        f"{ORDER_ID}:2500",
    ]
    assert {row["write_path"] for row in rows} == {WEBHOOK, SWEEP}
    assert {row["authority"] for row in rows} == {"platform"}
    assert {row["order_ref"] for row in rows} == {ORDER_REF}

    paid = [
        dict(row)
        for row in await database.fetch_all(
            select(commerce_interaction_events).where(
                commerce_interaction_events.c.event_type == "order.paid"
            )
        )
    ]
    assert len(paid) == 1, "two ingresses of one order produced two paid rows"

    interactions = [
        dict(row) for row in await database.fetch_all(select(commerce_interactions))
    ]
    assert len(interactions) == 1
    assert interactions[0]["order_ref"] == ORDER_REF


async def test_the_money_lock_excludes_another_backend_for_this_order(monkeypatch):
    """A statement about OTHER sessions, which is what serialization means.

    A helper that took the lock and released it immediately, or took one on the
    wrong key, would satisfy any assertion about our own backend.
    """
    from services.commerce_interaction_service import order_money_read_modify_write_lock

    assert await _another_backend_can_take(ORDER_REF) is True

    async with order_money_read_modify_write_lock(
        merchant_id=MERCHANT_ID,
        store_id=STORE_ID,
        order_ref=ORDER_REF,
        scope="squarespace_refund",
    ):
        assert await _another_backend_can_take(ORDER_REF) is False
        # A DIFFERENT order is not blocked: the lock is per-order, so one busy
        # order must not serialise a whole store's deliveries.
        assert await _another_backend_can_take("squarespace:other") is True

    assert await _another_backend_can_take(ORDER_REF) is True


async def test_the_receiver_takes_the_lock_around_its_own_read(monkeypatch):
    """Proves the LOCK is held by the code path that does the read, not merely
    that the helper works when called by a test."""
    from routes import squarespace_webhooks as route

    observed = []
    real_read = route.record_squarespace_order

    from services import squarespace_ledger as ledger

    real_ledger_read = ledger.recorded_refund_amount_cents

    async def probing_read(**kwargs):
        observed.append(await _another_backend_can_take(ORDER_REF))
        return await real_ledger_read(**kwargs)

    monkeypatch.setattr(ledger, "recorded_refund_amount_cents", probing_read)

    app = _webhook_app(monkeypatch, _order(refunded="10.00"))
    response = await _deliver(app, notification_id="n-lock")

    assert response.status_code == 200, response.text
    assert observed == [False], (
        "the baseline read ran without the order's advisory lock held"
    )
    assert real_read is route.record_squarespace_order


async def test_an_order_with_nothing_refunded_takes_no_lock(monkeypatch):
    """The hot path. Taking an advisory transaction lock for every order.create
    would serialise a store's whole delivery stream behind one order."""
    from services import squarespace_ledger as ledger

    taken = []
    real_lock = ledger.order_money_read_modify_write_lock

    def spying_lock(**kwargs):
        taken.append(kwargs)
        return real_lock(**kwargs)

    monkeypatch.setattr(ledger, "order_money_read_modify_write_lock", spying_lock)

    app = _webhook_app(monkeypatch, _order())
    response = await _deliver(app, notification_id="n-nolock")

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 2
    assert taken == []


# ---------------------------------------------------------------------------
# The credential blob's critical section.
#
# `merge_squarespace_credentials` is a read-modify-write over ONE cell that
# holds the API key, the OAuth token, the website binding, the once-shown
# webhook secret and the reconciliation cursor. Without a row lock, two writers
# both read the pre-write blob and the second silently discards the first: a
# sweep's cursor write landing between `ensure`'s read and its write erases the
# `webhook_secret`, after which every delivery 401s and no reconnect can
# recover it, because Squarespace shows that secret exactly once.
#
# SQLite cannot observe any of that — it has no `FOR UPDATE`, and `databases`
# serializes everything onto one connection — so the claim can only be pinned
# HERE, with genuinely separate backends.


# `merchant_stores` may already exist in this throwaway database, left by a
# sibling gate with a NARROWER column set. So the table is created if absent and
# the columns this test needs are added if missing — never dropped and rebuilt,
# which would silently delete another gate's fixture. Cleanup removes only THIS
# test's row for the same reason.
_STORES_DDL = (
    """
    CREATE TABLE IF NOT EXISTS merchant_stores (
        store_id TEXT PRIMARY KEY,
        merchant_id TEXT,
        platform TEXT,
        domain TEXT,
        status TEXT
    )
    """,
    "ALTER TABLE merchant_stores ADD COLUMN IF NOT EXISTS name TEXT",
    "ALTER TABLE merchant_stores ADD COLUMN IF NOT EXISTS api_key TEXT",
    "ALTER TABLE merchant_stores ADD COLUMN IF NOT EXISTS last_sync TIMESTAMPTZ",
    "ALTER TABLE merchant_stores ADD COLUMN IF NOT EXISTS connected_at TIMESTAMPTZ",
)

_SEED_BLOB = {
    "api_key": "live-key",
    "website_id": WEBSITE_ID,
    "webhook_secret": "shown-exactly-once",
    "reconciliation": {"orders_cursor": "2026-09-01T00:00:00.000Z"},
}


async def _merge_fixture():
    """A `merchant_stores` row plus two INDEPENDENT backends to race on it.

    Separate `databases.Database` objects, not two tasks on the shared handle:
    `databases` shares one connection across child tasks, so two coroutines on
    the same handle would serialize in the client and prove nothing about the
    database. Each of these owns its own pool, so a lock is the only thing that
    can order them.
    """
    import databases

    from db.database import database

    for statement in _STORES_DDL:
        await database.execute(statement)
    await database.execute(
        "DELETE FROM merchant_stores WHERE store_id = :store_id",
        {"store_id": STORE_ID},
    )
    await database.execute(
        "INSERT INTO merchant_stores (store_id, merchant_id, platform, api_key)"
        " VALUES (:store_id, :merchant_id, 'squarespace', :api_key)",
        {
            "store_id": STORE_ID,
            "merchant_id": MERCHANT_ID,
            "api_key": json.dumps(_SEED_BLOB),
        },
    )
    first = databases.Database(DATABASE_URL)
    second = databases.Database(DATABASE_URL)
    await first.connect()
    await second.connect()
    return first, second


async def _stored_blob():
    from db.database import database

    row = await database.fetch_one(
        "SELECT api_key FROM merchant_stores WHERE store_id = :store_id",
        {"store_id": STORE_ID},
    )
    return json.loads(dict(row)["api_key"])


async def test_two_concurrent_merges_serialize_and_neither_write_is_lost():
    """The lost update, reproduced and then closed.

    A third connection holds the row while both merges are launched, so their
    interleaving is decided rather than raced:

    * WITH `SELECT ... FOR UPDATE` inside the merge, both merges block on that
      lock. They run one after the other, and the second one READS what the
      first committed — so the secret and the cursor both survive.
    * WITHOUT it, a plain `SELECT` does not block on a row lock in Postgres
      (readers never block writers under MVCC). Both merges read the ORIGINAL
      blob immediately, then queue on the UPDATE, and whichever commits last
      overwrites the other's key entirely.

    The `asyncio.sleep` is what makes that deterministic: it guarantees both
    merges have reached their blocking point before the holder lets go.
    """
    import asyncio

    from db.database import database
    from services.squarespace_connection import merge_squarespace_credentials

    first, second = await _merge_fixture()
    try:
        holder_released = asyncio.Event()

        async def _hold_the_row():
            async with database.transaction():
                await database.fetch_one(
                    "SELECT api_key FROM merchant_stores"
                    " WHERE store_id = :store_id FOR UPDATE",
                    {"store_id": STORE_ID},
                )
                await holder_released.wait()

        holder = asyncio.create_task(_hold_the_row())
        await asyncio.sleep(0.2)

        # `ensure` rotating the subscription secret...
        ensure = asyncio.create_task(
            merge_squarespace_credentials(
                store_id=STORE_ID,
                updates={"webhook_secret": "rotated-secret"},
                db=first,
            )
        )
        # ...and a sweep persisting its cursor, at the same instant.
        sweep = asyncio.create_task(
            merge_squarespace_credentials(
                store_id=STORE_ID,
                updates={
                    "reconciliation": {"orders_cursor": "2026-09-05T00:00:00.000Z"}
                },
                db=second,
            )
        )

        # Both are now parked. Under a plain SELECT they have ALREADY read the
        # stale blob by this point, which is precisely the bug.
        await asyncio.sleep(0.4)
        holder_released.set()
        await holder
        await asyncio.wait_for(asyncio.gather(ensure, sweep), timeout=20)

        blob = await _stored_blob()
        assert blob["webhook_secret"] == "rotated-secret", (
            "the sweep's cursor write erased the rotated webhook secret — the "
            "merge is not serialized, and Squarespace shows that secret once"
        )
        assert blob["reconciliation"] == {
            "orders_cursor": "2026-09-05T00:00:00.000Z"
        }, "the ensure erased the sweep's cursor"
        # And nothing either writer was not asked to touch moved.
        assert blob["api_key"] == "live-key"
        assert blob["website_id"] == WEBSITE_ID
    finally:
        await first.disconnect()
        await second.disconnect()
        await database.execute(
            "DELETE FROM merchant_stores WHERE store_id = :store_id",
            {"store_id": STORE_ID},
        )


async def test_a_second_merge_WAITS_for_the_first_rather_than_reading_past_it():
    """The mechanism behind the test above, asserted directly.

    Without this, "neither write was lost" could hold by luck of scheduling on
    a fast machine. What must be true is that a merge whose row is locked
    elsewhere does not COMPLETE until the lock is released.
    """
    import asyncio

    from db.database import database
    from services.squarespace_connection import merge_squarespace_credentials

    first, _second = await _merge_fixture()
    try:
        released = asyncio.Event()

        async def _hold_the_row():
            async with database.transaction():
                await database.fetch_one(
                    "SELECT api_key FROM merchant_stores"
                    " WHERE store_id = :store_id FOR UPDATE",
                    {"store_id": STORE_ID},
                )
                await released.wait()

        holder = asyncio.create_task(_hold_the_row())
        await asyncio.sleep(0.2)

        merging = asyncio.create_task(
            merge_squarespace_credentials(
                store_id=STORE_ID, updates={"webhook_secret": "rotated"}, db=first
            )
        )
        await asyncio.sleep(0.5)
        assert not merging.done(), (
            "the merge completed while another backend held the row — it is not "
            "taking the lock"
        )

        released.set()
        await holder
        persisted = await asyncio.wait_for(merging, timeout=20)
        assert persisted["webhook_secret"] == "rotated"
    finally:
        await first.disconnect()
        await _second.disconnect()
        await database.execute(
            "DELETE FROM merchant_stores WHERE store_id = :store_id",
            {"store_id": STORE_ID},
        )
