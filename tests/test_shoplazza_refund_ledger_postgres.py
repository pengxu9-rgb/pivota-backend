"""Production-dialect gate for the Shoplazza cumulative-refund read-modify-write.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_shoplazza_refund_ledger_postgres.py

WHY REAL POSTGRES. Three things this receiver relies on are undecidable on SQLite:

* `recorded_refund_amount_cents` reads `amount_cents` and `currency` out of the
  ledger's `payload` column. That column is `jsonb` here and plain `JSON` on
  SQLite, and the two dialects do not agree on whether a read hands back a
  mapping or the raw JSON text — reading `.get` off a string would silently make
  every "previously recorded" figure 0, which turns every partial refund into a
  duplicate of the whole cumulative total. The statement also has to PREPARE:
  a `synthetic IS FALSE` predicate plus a nullable `store_id` bind is exactly
  the shape that has produced `IndeterminateDatatypeError` in this repo before.
* `order_money_read_modify_write_lock` takes `pg_advisory_xact_lock`. That
  branch is DEAD on SQLite (`IS_POSTGRES` is False), so the only place its
  statement, its transaction nesting around `ingest_merchant_event_batch`, and
  the fact that it commits can be observed is here.
* the lock's whole purpose is to keep a SECOND session out. On SQLite there is
  no lock at all and the raced pair inflates refunded GMV (see
  `tests/test_shoplazza_refund_ledger_end_to_end.py::
  test_a_raced_pair_of_different_totals_inflates_the_refunded_gmv`). Proving the
  lock actually excludes anyone needs a second backend, which only exists here.

HOW IT IS DRIVEN. Through the REAL route, with a real HMAC, exactly as
`tests/test_shoplazza_refund_ledger_end_to_end.py` drives it on SQLite. An
earlier version of this gate composed lock → read → map → ingest itself, which
made it blind to every mistake the RECEIVER could make — passing a stale
baseline, deriving the event id from the delivery id — and made its lock
assertion (`pg_locks WHERE pid = pg_backend_pid()`) a statement about our own
backend rather than about serialization.

Empty-table gates are the norm in this job; this one populates real orders
because the arithmetic under test is about rows the receiver wrote itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

APP_SECRET = "app-secret"
DOMAIN = "demo.myshoplaza.com"
MERCHANT_ID = "merchant-sz"
STORE_ID = "store-sz"
ORDER_ID = "sz-order-1"
ORDER_REF = f"shoplazza:{ORDER_ID}"
WRITE_PATH = "shoplazza_webhook"
PATH = f"/webhooks/shoplazza/{STORE_ID}"
CURRENCY = "USD"

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


# -- the real route, with a real signature ----------------------------------


def _order(**overrides):
    order = {
        "id": ORDER_ID,
        "created_at": "2026-09-01T10:00:00Z",
        "placed_at": "2026-09-01T10:01:00Z",
        "updated_at": "2026-09-01T10:01:00Z",
        "currency": CURRENCY,
        "total_price": "40.00",
        "real_total_paid": "40.00",
        "financial_status": "paid",
        "customer": {"id": "buyer-2"},
    }
    order.update(overrides)
    return order


def _refund_order(cumulative: str, updated_at: str = "2026-09-02T10:00:00Z", **overrides):
    return _order(
        updated_at=updated_at,
        financial_status="partially_refunded",
        total_refund_price=cumulative,
        **overrides,
    )


def _app(monkeypatch):
    """The router itself. Only the `merchant_stores` lookup is a double."""
    from fastapi import FastAPI

    from routes import shopline_family_webhooks as route

    class FakeStores:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "domain": DOMAIN,
                "api_key": json.dumps({"app_secret": APP_SECRET}),
            }

    monkeypatch.setattr(route, "database", FakeStores())
    app = FastAPI()
    app.include_router(route.router)
    return app


async def _post(app, order, *, topic, delivery_id):
    """One signed delivery, driven in THIS event loop.

    `httpx.ASGITransport` rather than the `TestClient` the SQLite sibling uses,
    for one measured reason: `TestClient` drives the app from a portal thread
    running its own event loop, while the asyncpg pool under
    `db.database.database` is bound to the loop this test runs in, and the
    first delivery through it does not fail — it HANGS (killed at 120s). The
    SQLite sibling gets away with `TestClient` because its aiosqlite database
    is created inside that same request.

    Everything above the transport — signature, source-domain check, the lock,
    the ledger read, the mapper, the ingest, the funnel-visible row — is the
    production path either way; only the client differs.
    """
    import httpx

    raw = json.dumps({"order": order}, separators=(",", ":")).encode("utf-8")
    signature = base64.b64encode(
        hmac.new(APP_SECRET.encode("utf-8"), raw, hashlib.sha256).digest()
    ).decode("ascii")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            PATH,
            content=raw,
            headers={
                "X-Shoplazza-Hmac-Sha256": signature,
                "X-Shoplazza-Topic": topic,
                "X-Shoplazza-Deduplication-ID": delivery_id,
                "X-Shoplazza-Shop-Domain": DOMAIN,
                "Content-Type": "application/json",
            },
        )


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


_SCOPE = dict(
    merchant_id=MERCHANT_ID,
    store_id=STORE_ID,
    order_ref=ORDER_REF,
    write_path=WRITE_PATH,
)


# -- a genuinely separate backend -------------------------------------------


def _asyncpg_dsn() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


def _lock_key(order_ref: str) -> str:
    """The exact string `order_money_read_modify_write_lock` hashes."""
    return f"shoplazza_refund|{MERCHANT_ID}|{STORE_ID}|{order_ref}"


async def _another_backend_can_take(order_ref: str) -> bool:
    """Could a DIFFERENT Postgres session take this order's advisory lock now?

    This is the only shape of assertion that can distinguish a real lock from a
    no-op helper. `pg_locks WHERE pid = pg_backend_pid()` — what this gate used
    to assert — is a statement about our OWN backend, and serialization is by
    definition a claim about other backends: a helper that took the lock and
    immediately released it, or that took a lock on the wrong key, would still
    satisfy it.

    `pg_try_advisory_xact_lock` never waits, so a held lock answers False here
    instead of deadlocking the test. Outside an explicit transaction each
    statement is its own transaction, so a lock this probe DOES take is
    released the moment the statement ends and leaves nothing behind. Each
    probe opens its own short-lived connection, which is also what makes it a
    different backend.
    """
    import asyncpg

    connection = await asyncpg.connect(_asyncpg_dsn())
    try:
        return bool(
            await connection.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtext($1))", _lock_key(order_ref)
            )
        )
    finally:
        await connection.close()


# -- the tests ---------------------------------------------------------------


async def test_the_recorded_refund_read_prepares_and_totals_on_postgres(monkeypatch):
    from services.commerce_interaction_service import recorded_refund_amount_cents

    # Executed against empty tables first: the statement has to PREPARE before
    # there is anything for it to find.
    assert await recorded_refund_amount_cents(**_SCOPE) == 0
    assert await recorded_refund_amount_cents(**_SCOPE, currency=CURRENCY) == 0

    app = _app(monkeypatch)
    await _post(app, _order(), topic="orders/paid", delivery_id="delivery-paid")
    await _post(
        app,
        _refund_order("10.00"),
        topic="orders/partially_refunded",
        delivery_id="delivery-refund-1",
    )
    assert await recorded_refund_amount_cents(**_SCOPE) == 1000

    await _post(
        app,
        _refund_order("25.00", "2026-09-03T10:00:00Z"),
        topic="orders/partially_refunded",
        delivery_id="delivery-refund-2",
    )
    # 1000 + 1500. If the jsonb read came back as text and `.get` silently
    # returned None, this would still be 0 and every later delivery would
    # re-record the whole cumulative total.
    assert await recorded_refund_amount_cents(**_SCOPE) == 2500

    # Every filter, exercised against real rows rather than an empty table.
    assert await recorded_refund_amount_cents(**{**_SCOPE, "store_id": "other"}) == 0
    assert await recorded_refund_amount_cents(**{**_SCOPE, "merchant_id": "other"}) == 0
    assert await recorded_refund_amount_cents(**{**_SCOPE, "write_path": "stripe_webhook"}) == 0
    assert await recorded_refund_amount_cents(**{**_SCOPE, "order_ref": "shoplazza:other"}) == 0
    # A NULL store_id is a different scope, not a wildcard, and the bind has to
    # compile to IS NULL rather than `= NULL` (which matches nothing but also
    # never fails).
    assert await recorded_refund_amount_cents(**{**_SCOPE, "store_id": None}) == 0
    # The currency narrowing reads out of the same jsonb payload.
    assert await recorded_refund_amount_cents(**_SCOPE, currency="usd") == 2500
    assert await recorded_refund_amount_cents(**_SCOPE, currency="EUR") == 0


async def test_two_refund_deliveries_through_the_route_record_their_deltas(monkeypatch):
    """The receiver's own arithmetic, on the production dialect.

    Composing the lock, the read and the ingest inside the test would prove the
    STATEMENTS run on Postgres and nothing about the receiver. Driving the
    route is what makes a stale baseline, or an event id keyed on the delivery
    id, fail here rather than only on SQLite.
    """
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interactions
    from db.database import database
    from routes import shopline_family_webhooks as route

    app = _app(monkeypatch)
    paid = await _post(app, _order(), topic="orders/paid", delivery_id="delivery-paid")
    assert paid.status_code == 200, paid.text

    first = await _post(
        app,
        _refund_order("10.00"),
        topic="orders/partially_refunded",
        delivery_id="delivery-refund-1",
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "recorded"
    assert first.json()["accepted"] == 1

    second = await _post(
        app,
        _refund_order("25.00", "2026-09-03T10:00:00Z"),
        topic="orders/partially_refunded",
        delivery_id="delivery-refund-2",
    )
    assert second.status_code == 200, second.text
    assert second.json()["accepted"] == 1

    rows = await _refund_rows()
    # 1500, not 2500: the receiver read back what it had already written.
    assert [row["payload"]["amount_cents"] for row in rows] == [1000, 1500]
    assert [row["payload"]["refund_id"] for row in rows] == [
        f"{ORDER_ID}:1000",
        f"{ORDER_ID}:2500",
    ]
    assert {row["payload"]["currency"] for row in rows} == {CURRENCY}
    assert {row["write_path"] for row in rows} == {WRITE_PATH}
    assert {row["order_ref"] for row in rows} == {ORDER_REF}
    assert {row["payload"]["native_amount_semantics"] for row in rows} == {
        "cumulative_refund_total_delta"
    }
    interactions = [
        dict(row) for row in await database.fetch_all(select(commerce_interactions))
    ]
    assert len(interactions) == 1

    # A redelivery of a total we already hold, mapped against a STALE baseline
    # of 0 — the shape a raced delivery takes. It must land on the SAME event
    # key and dedupe. If the event id were derived from the delivery id (or
    # from the trace id it falls back to) this would be a fresh key and a
    # second row of 1000 would be accepted, double-counting the refund.
    third_delivery_reads = []

    async def stale_read(**kwargs):
        third_delivery_reads.append(kwargs)
        return 0

    monkeypatch.setattr(route, "recorded_refund_amount_cents", stale_read)
    raced = await _post(
        app,
        _refund_order("10.00"),
        topic="orders/partially_refunded",
        delivery_id="delivery-refund-1-raced",
    )
    assert raced.status_code == 200, raced.text
    assert third_delivery_reads, "the receiver never consulted the ledger read"
    assert raced.json()["accepted"] == 0
    assert raced.json()["duplicates"] == 1
    assert len(await _refund_rows()) == 2


async def test_the_advisory_lock_excludes_a_second_backend_and_releases(monkeypatch):
    """The branch SQLite can never reach, asserted from OUTSIDE our session."""
    from db.database import database
    from routes import shopline_family_webhooks as route
    from services.commerce_interaction_service import (
        order_money_read_modify_write_lock,
        recorded_refund_amount_cents,
    )

    app = _app(monkeypatch)
    await _post(app, _order(), topic="orders/paid", delivery_id="delivery-paid")

    # Nobody holds it yet.
    assert await _another_backend_can_take(ORDER_REF) is True

    probes = {}
    real_read = route.recorded_refund_amount_cents

    async def probing_read(**kwargs):
        """Runs INSIDE the lock the route took, before the route's own write."""
        probes["same_order"] = await _another_backend_can_take(ORDER_REF)
        probes["other_order"] = await _another_backend_can_take("shoplazza:sz-order-2")
        probes["baseline"] = await real_read(**kwargs)
        return probes["baseline"]

    monkeypatch.setattr(route, "recorded_refund_amount_cents", probing_read)
    first = await _post(
        app,
        _refund_order("10.00"),
        topic="orders/partially_refunded",
        delivery_id="delivery-refund-1",
    )
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 1

    # Held against another backend for THIS order's key, and only that key: a
    # lock on the wrong key, or one taken and released, would show True here.
    assert probes["same_order"] is False
    assert probes["other_order"] is True
    assert probes["baseline"] == 0

    # Released once the request finished — an xact lock that outlived its
    # transaction would wedge every later delivery for this order.
    assert await _another_backend_can_take(ORDER_REF) is True
    # And the write inside the lock is durable: the nested transaction
    # `record_commerce_event` opens on Postgres is a SAVEPOINT inside the
    # lock's transaction, so a row readable now is a row that committed.
    assert await recorded_refund_amount_cents(**_SCOPE) == 1000

    # Belt and braces on the statement itself, outside any route: the helper
    # holds the lock for the body and drops it at the end of the block.
    lock_scope = {key: value for key, value in _SCOPE.items() if key != "write_path"}
    async with order_money_read_modify_write_lock(scope="shoplazza_refund", **lock_scope):
        assert await _another_backend_can_take(ORDER_REF) is False
        assert await database.fetch_val("SELECT 1") == 1
    assert await _another_backend_can_take(ORDER_REF) is True


async def test_a_synthetic_refund_row_does_not_reduce_the_next_delta(monkeypatch):
    """A probe row must not become a baseline that suppresses real money.

    Same claim as the SQLite sibling, re-asserted here because the exclusion is
    a `synthetic IS FALSE` predicate compiled by the Postgres dialect, and
    because a probe row that DID count would make every later refund of that
    order `refund_not_new` — an outcome that is a silent 2xx.
    """
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interaction_events
    from db.database import database
    from services.commerce_interaction_service import recorded_refund_amount_cents
    from services.merchant_event_ingest_service import ingest_merchant_event_batch
    from services.shopline_family_event_adapter import map_shoplazza_webhook

    app = _app(monkeypatch)
    await _post(app, _order(), topic="orders/paid", delivery_id="delivery-paid")

    canary = map_shoplazza_webhook(
        {"order": _refund_order("50.00")},
        topic="orders/partially_refunded",
        delivery_id="delivery-canary",
        store_id=STORE_ID,
        previously_recorded_refund_cents=0,
    )
    canary_result = await ingest_merchant_event_batch(
        merchant_id=MERCHANT_ID,
        batch=canary,
        agent_identity_confidence="platform_asserted",
        write_path=WRITE_PATH,
        synthetic=True,
    )
    assert canary_result["accepted"] == 1

    # The row is really marked synthetic, and it really is in this order's
    # scope — otherwise the exclusion below would be vacuous.
    marks = [
        dict(row)
        for row in await database.fetch_all(
            select(
                commerce_interaction_events.c.synthetic,
                commerce_interaction_events.c.order_ref,
                commerce_interaction_events.c.write_path,
            ).where(commerce_interaction_events.c.event_type == "refund.succeeded")
        )
    ]
    assert marks == [{"synthetic": True, "order_ref": ORDER_REF, "write_path": WRITE_PATH}]
    assert (await _refund_rows())[0]["payload"]["amount_cents"] == 5000
    assert await recorded_refund_amount_cents(**_SCOPE) == 0

    # A real 10.00 refund. If the 5000-cent canary counted, the delta would be
    # negative and this delivery would be ignored.
    real = await _post(
        app,
        _refund_order("10.00", "2026-09-03T10:00:00Z"),
        topic="orders/partially_refunded",
        delivery_id="delivery-refund-1",
    )
    assert real.status_code == 200, real.text
    assert real.json()["status"] == "recorded", real.text
    assert real.json()["accepted"] == 1
    recorded = [row for row in await _refund_rows() if not row["synthetic"]]
    assert [row["payload"]["amount_cents"] for row in recorded] == [1000]
    assert [row["payload"]["refund_id"] for row in recorded] == [f"{ORDER_ID}:1000"]


async def test_the_amount_survives_the_jsonb_round_trip_as_an_integer(monkeypatch):
    from services.commerce_interaction_service import _payload_mapping

    app = _app(monkeypatch)
    await _post(app, _order(), topic="orders/paid", delivery_id="delivery-paid")
    await _post(
        app,
        _refund_order("10.00"),
        topic="orders/partially_refunded",
        delivery_id="delivery-refund-1",
    )
    rows = await _refund_rows()
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert isinstance(_payload_mapping(payload), dict)
    assert payload["amount_cents"] == 1000
    assert payload["currency"] == CURRENCY
    assert payload["refund_id"] == f"{ORDER_ID}:1000"
    assert payload["native_amount_semantics"] == "cumulative_refund_total_delta"
