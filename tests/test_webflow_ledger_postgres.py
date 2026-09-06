"""Production-dialect gate for the Webflow bridge.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_webflow_ledger_postgres.py

WHY REAL POSTGRES. Two things this integration relies on are undecidable on
SQLite.

* The ledger rows are read back out of the `payload` column, which is `jsonb`
  here and plain `JSON` on SQLite. The two dialects disagree about whether a
  read hands back a mapping or the raw JSON text, and an amount read out of a
  string would be silently wrong — which for THIS integration means a money
  figure, since Webflow amounts are minor-unit integers carried through with no
  conversion at all.
* `merge_store_credentials` takes `SELECT ... FOR UPDATE` on the store row, and
  that branch is DEAD on SQLite (`IS_POSTGRES` is False). The only place its
  statement, and the fact that it actually EXCLUDES another backend, can be
  observed is here, with genuinely separate connection pools. What is at stake
  in the Webflow blob specifically is the `url_secret`: it is baked into the URL
  registered AT WEBFLOW, so a lost update does not rotate a secret, it leaves
  Webflow delivering to a path the receiver can only 401 until someone
  re-provisions.

HOW IT IS DRIVEN. Through the real receiver and the real sweep, the same way
`tests/test_webflow_ledger_end_to_end.py` drives them on SQLite. Composing
map -> ingest inside the test would make it blind to every mistake the ingresses
themselves could make.

`httpx.ASGITransport` rather than `TestClient`: `TestClient` drives the app from
a portal thread with its own event loop, while the asyncpg pool under
`db.database.database` is bound to this one, and the first delivery through it
does not fail — it HANGS.
"""

from __future__ import annotations

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

URL_SECRET = "wf-url-secret-value"
MERCHANT_ID = "merchant-wf"
STORE_ID = "store-wf-pg"
SITE_ID = "5f1a0000000000000000aaaa"
ORDER_ID = "0000-0001"
ORDER_REF = f"webflow:{ORDER_ID}"
PATH = f"/webhooks/webflow/{STORE_ID}/{URL_SECRET}"
WEBHOOK = "webflow_webhook"
SWEEP = "webflow_reconciliation"

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


def _order(*, status="unfulfilled", refunded=None):
    order = {
        "orderId": ORDER_ID,
        "status": status,
        "acceptedOn": _iso(_NOW - timedelta(days=2)),
        # 5898 minor units == $58.98, carried with NO conversion.
        "customerPaid": {"unit": "USD", "value": 5898, "string": "$58.98"},
    }
    if refunded is not None:
        order["refundedOn"] = refunded
    return order


_CREDENTIALS = {
    "api_token": "wf-token",
    "site_id": SITE_ID,
    "url_secret": URL_SECRET,
}


def _webhook_app(monkeypatch, order):
    from fastapi import FastAPI

    from routes import webflow_webhooks as route

    class FakeStores:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "domain": "shop.webflow.io",
                "api_key": json.dumps(_CREDENTIALS),
            }

    async def fake_fetch(*, api_token, site_id, order_id, **kwargs):
        return order

    monkeypatch.setattr(route, "database", FakeStores())
    monkeypatch.setattr(route, "fetch_webflow_order", fake_fetch)
    monkeypatch.delenv("WEBFLOW_CLIENT_SECRET", raising=False)
    route._SEEN_DELIVERIES.clear()

    app = FastAPI()
    app.include_router(route.router)
    return app


async def _deliver(app, *, nonce="1"):
    """One delivery, driven in THIS event loop (see the module docstring)."""
    import httpx

    raw = json.dumps(
        {
            "triggerType": "ecomm_order_changed",
            "siteId": SITE_ID,
            "deliveryNonce": nonce,
            "payload": {"orderId": ORDER_ID},
        },
        separators=(",", ":"),
    ).encode()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            PATH, content=raw, headers={"Content-Type": "application/json"}
        )


async def _sweep(monkeypatch, orders, *, lanes=None):
    from services import webflow_order_sweep as sweep

    class _Response:
        status_code = 200
        content = b"{}"
        headers: dict = {}

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        async def get(self, url, headers=None, params=None):
            if "/orders" not in str(url):
                return _Response({"id": SITE_ID, "displayName": "Shop"})
            status = (params or {}).get("status")
            rows = orders if status is None else [
                row for row in orders if row.get("status") == status
            ]
            return _Response({"orders": rows, "pagination": {}})

        async def aclose(self):
            return None

    async def fake_find(store_id):
        return {
            "store_id": STORE_ID,
            "merchant_id": MERCHANT_ID,
            "domain": "shop.webflow.io",
            "api_key": json.dumps(_CREDENTIALS),
        }

    async def fake_merge(*, store_id, updates=None, **kwargs):
        return {**_CREDENTIALS, **(updates or {})}

    monkeypatch.setattr(sweep, "find_webflow_store", fake_find)
    monkeypatch.setattr(sweep, "merge_webflow_credentials", fake_merge)
    return await sweep.sweep_webflow_store(
        store_id=STORE_ID, client=_Client(), lanes=lanes
    )


async def _rows(event_type):
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interaction_events
    from db.database import database
    from services.commerce_interaction_service import _payload_mapping

    rows = [
        dict(row)
        for row in await database.fetch_all(
            select(commerce_interaction_events).where(
                commerce_interaction_events.c.event_type == event_type
            )
        )
    ]
    for row in rows:
        row["payload"] = _payload_mapping(row["payload"])
    return rows


# -- the ledger, on the production dialect -----------------------------------


async def test_a_webhook_and_a_sweep_of_one_order_give_one_paid_row(monkeypatch):
    """The dedupe, the money, and the jsonb read, all on real Postgres.

    If the jsonb payload came back as text and `.get` silently returned None,
    the amount assertion below would be the thing that noticed.
    """
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interactions
    from db.database import database

    order = _order()
    app = _webhook_app(monkeypatch, order)
    delivered = await _deliver(app)
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["accepted"] == 2

    swept = await _sweep(monkeypatch, [order], lanes=["orders"])
    assert swept["accepted"] == 0
    assert swept["duplicates"] == 2

    paid = await _rows("order.paid")
    assert len(paid) == 1, "two ingresses of one order produced two paid rows"
    # 5898 minor units. A `* 100` anywhere would read 589800 here.
    assert paid[0]["payload"]["amount_cents"] == 5898
    assert paid[0]["payload"]["currency"] == "USD"
    assert paid[0]["write_path"] == WEBHOOK
    assert paid[0]["authority"] == "platform"
    assert paid[0]["order_ref"] == ORDER_REF

    interactions = [
        dict(row) for row in await database.fetch_all(select(commerce_interactions))
    ]
    assert len(interactions) == 1
    assert interactions[0]["order_ref"] == ORDER_REF


async def test_a_full_order_refund_is_one_row_however_often_it_is_observed(monkeypatch):
    """No cumulative arithmetic, so no baseline read and no lock — and therefore
    nothing to race. Both ingresses emit the IDENTICAL row and the ledger's
    unique index collapses them."""
    refunded = _order(status="refunded", refunded=_iso(_NOW - timedelta(days=1)))
    app = _webhook_app(monkeypatch, refunded)

    assert (await _deliver(app, nonce="1")).status_code == 200
    assert (await _deliver(app, nonce="2")).json()["duplicates"] == 3
    await _sweep(monkeypatch, [refunded], lanes=["refunded"])
    await _sweep(monkeypatch, [refunded], lanes=["refunded"])

    refunds = await _rows("refund.succeeded")
    assert len(refunds) == 1
    assert refunds[0]["payload"]["amount_cents"] == 5898
    assert refunds[0]["payload"]["refund_id"] == f"{ORDER_ID}:refund"
    assert len(await _rows("order.paid")) == 1


async def test_a_dispute_lost_shares_the_refunds_key_on_the_real_dialect(monkeypatch):
    """An order that MOVES from refunded to dispute-lost across observations.

    Two keys would put two refund rows in this table, and the funnel sums refund
    rows: the money out would read double.
    """
    refunded = _order(status="refunded", refunded=_iso(_NOW - timedelta(days=1)))
    assert (await _deliver(_webhook_app(monkeypatch, refunded))).status_code == 200

    lost = _order(status="dispute-lost")
    lost["disputedOn"] = _iso(_NOW - timedelta(hours=2))
    assert (await _deliver(_webhook_app(monkeypatch, lost), nonce="2")).json()[
        "accepted"
    ] == 0

    refunds = await _rows("refund.succeeded")
    assert len(refunds) == 1
    assert refunds[0]["payload"]["amount_cents"] == 5898


# ---------------------------------------------------------------------------
# The credential blob's critical section.
#
# `merge_store_credentials` is a read-modify-write over ONE cell that holds the
# API token, the site binding, the URL secret and every lane's cursor. Without a
# row lock, two writers both read the pre-write blob and the second silently
# discards the first: a sweep's cursor write landing between `ensure`'s read and
# its write erases the `url_secret`, after which every delivery 401s and only a
# re-provision recovers it — because the secret Webflow is delivering to is in a
# URL Pivota registered, not in anything Webflow will tell us.
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
    "api_token": "live-token",
    "site_id": SITE_ID,
    "url_secret": "registered-at-webflow",
    "reconciliation": {"orders": {"cursor": "2026-09-01T00:00:00.000Z"}},
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
        " VALUES (:store_id, :merchant_id, 'webflow', :api_key)",
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


async def _cleanup_store():
    from db.database import database

    await database.execute(
        "DELETE FROM merchant_stores WHERE store_id = :store_id",
        {"store_id": STORE_ID},
    )


async def test_two_concurrent_merges_serialize_and_neither_write_is_lost():
    """The lost update, reproduced and then closed.

    A third connection holds the row while both merges are launched, so their
    interleaving is decided rather than raced:

    * WITH `SELECT ... FOR UPDATE` inside the merge, both merges block on that
      lock. They run one after the other, and the second one READS what the
      first committed — so the URL secret and the cursor both survive.
    * WITHOUT it, a plain `SELECT` does not block on a row lock in Postgres
      (readers never block writers under MVCC). Both merges read the ORIGINAL
      blob immediately, then queue on the UPDATE, and whichever commits last
      overwrites the other's key entirely.

    The `asyncio.sleep` is what makes that deterministic: it guarantees both
    merges have reached their blocking point before the holder lets go.
    """
    import asyncio

    from db.database import database
    from services.webflow_connection import merge_webflow_credentials

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

        # `ensure` rotating the URL secret...
        ensure = asyncio.create_task(
            merge_webflow_credentials(
                store_id=STORE_ID,
                updates={"url_secret": "rotated-secret"},
                db=first,
            )
        )
        # ...and a sweep persisting its cursors, at the same instant.
        sweep = asyncio.create_task(
            merge_webflow_credentials(
                store_id=STORE_ID,
                updates={
                    "reconciliation": {
                        "orders": {"cursor": "2026-09-05T00:00:00.000Z"},
                        "refunded": {"cursor": "2026-09-05T00:00:00.000Z"},
                    }
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
        assert blob["url_secret"] == "rotated-secret", (
            "the sweep's cursor write erased the rotated URL secret — the merge "
            "is not serialized, and that secret is baked into the URL Webflow "
            "delivers to"
        )
        assert blob["reconciliation"]["refunded"] == {
            "cursor": "2026-09-05T00:00:00.000Z"
        }, "the ensure erased the sweep's cursors"
        # And nothing either writer was not asked to touch moved.
        assert blob["api_token"] == "live-token"
        assert blob["site_id"] == SITE_ID
    finally:
        await first.disconnect()
        await second.disconnect()
        await _cleanup_store()


async def test_a_second_merge_WAITS_for_the_first_rather_than_reading_past_it():
    """The mechanism behind the test above, asserted directly.

    Without this, "neither write was lost" could hold by luck of scheduling on a
    fast machine. What must be true is that a merge whose row is locked elsewhere
    does not COMPLETE until the lock is released.
    """
    import asyncio

    from db.database import database
    from services.webflow_connection import merge_webflow_credentials

    first, second = await _merge_fixture()
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
            merge_webflow_credentials(
                store_id=STORE_ID, updates={"url_secret": "rotated"}, db=first
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
        assert persisted["url_secret"] == "rotated"
    finally:
        await first.disconnect()
        await second.disconnect()
        await _cleanup_store()


async def test_a_reconnect_to_another_site_drops_the_credential_inside_the_lock():
    """Connect's drop is a `mutate` INSIDE the merge's critical section.

    Run here rather than only on SQLite because that is the only place the
    critical section exists at all: the drop and the write have to be one
    transaction, or a concurrent sweep can resurrect the credential between them.
    """
    from services.webflow_connection import (
        drop_site_scoped_keys,
        merge_webflow_credentials,
        webflow_read_tokens,
    )

    first, second = await _merge_fixture()
    try:

        def _reconnect(blob):
            drop_site_scoped_keys(blob)
            blob.update({"api_token": "NEW-token", "site_id": "site-NEW"})
            return blob

        persisted = await merge_webflow_credentials(
            store_id=STORE_ID, mutate=_reconnect, mark_connected=True, db=first
        )

        assert persisted == {"api_token": "NEW-token", "site_id": "site-NEW"}
        assert webflow_read_tokens(persisted) == ["NEW-token"]
        assert await _stored_blob() == persisted
    finally:
        await first.disconnect()
        await second.disconnect()
        await _cleanup_store()
