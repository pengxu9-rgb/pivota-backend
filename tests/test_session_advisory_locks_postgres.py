"""Postgres gate: every session advisory lock that claims to keep two runs apart is HELD for the
whole run -- the Shopify order create, the per-merchant scheduled audit, and the external-seed
materialization tick.

WHY THIS NEEDS POSTGRES. These are `pg_try_advisory_lock` session locks and SQLite has none.
Each of the three used to be taken with a bare `database.fetch_one`. Outside an
`async with database.connection():` block, databases 0.7.0 hands that raw connection straight
back to the asyncpg pool, and asyncpg's reset on release runs `pg_advisory_unlock_all()`. So the
lock was released the instant it was granted (measured 2026-09-27: a second session took it
immediately after every one of the three reported success), and the matching unlock ran on
whatever pool connection came next and no-oped.

The fixes differ on purpose:
  * order create: the outer lock is GONE. It never held, so prod's real guard was always the
    pinned `_pg_advisory_lock_best_effort` around the create, which re-reads the order once it
    holds the lock. Pinning the outer one instead would have CHANGED prod behaviour: a second
    caller would return True at once, before the first create had succeeded or failed.
  * scheduled audit + materialization: the lock is held on a DEDICATED connection
    (db/session_advisory_lock.py), and both fail CLOSED when it cannot be checked (both used
    to fail open). Pinning the pool connection would not do for the audit: the
    three concurrent audits of one tick are child tasks of one context and share its databases
    Connection, so they would share one Postgres session, and a session lock is re-entrant --
    `test_two_audits_of_one_merchant_in_one_tick_run_once` fails under a pin.

"Held" is checked the only way that means anything: a SECOND asyncpg session calls
`pg_try_advisory_lock` while the run is parked mid-work and must get false.

Every test runs in its own `sal_<hex>` schema (search_path), so nothing here can collide with the
tables other gate files build in the shared database. Lock keys are derived from per-test uuids,
except the materialization job's fixed key.

    DATABASE_URL=postgresql://localhost/pivota_<you>_dialect_check \\
        .venv/bin/python -m pytest tests/test_session_advisory_locks_postgres.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — advisory locks exist only on the production dialect",
)

#: Same markers as the sibling gate files, so CI's pivota_dialect_check RUNS these.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _asyncpg_dsn() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


async def _another_session_can_take(lock_id: int) -> bool:
    """What a second pod sees: try the lock on a brand-new session, and give it back."""
    import asyncpg

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        got = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_id))
        if got:
            await conn.execute("SELECT pg_advisory_unlock($1)", lock_id)
        return got
    finally:
        await conn.close()


async def _until(predicate, *, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for the run to reach its parking point")


@pytest.fixture
async def pg(monkeypatch):
    """A connected Database whose connections resolve unqualified names in a fresh schema; it
    replaces the app's `db.database.database` for the test."""
    import asyncpg
    from databases import Database

    import db.database as db_module

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in {dbname!r} — throwaway only")

    schema = f"sal_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(_asyncpg_dsn())
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
    finally:
        await admin.close()

    db = Database(_asyncpg_dsn(), min_size=1, max_size=4, server_settings={"search_path": schema})
    await db.connect()
    monkeypatch.setattr(db_module, "database", db)
    try:
        yield db
    finally:
        await db.disconnect()
        admin = await asyncpg.connect(_asyncpg_dsn())
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await admin.close()


# ---------------------------------------------------------------------------------------------
# Order create: the pinned lock around the Shopify POST is the one guard, and it holds.
# ---------------------------------------------------------------------------------------------


class _ShopifyStub:
    """The order row, the merchant's store and Shopify itself, scripted; the lock is real. The
    FIRST order POST parks until `release` is set, so a create is caught in flight."""

    def __init__(self, order_id: str):
        self.order_id = order_id
        self.shopify_order_id = None
        self.posts = 0
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    def install(self, monkeypatch, order_routes) -> None:
        import httpx

        import db.orders as orders_db
        import services.shopify_graphql_client as gql

        stub = self

        async def get_order(_order_id):
            return {
                "order_id": stub.order_id,
                "merchant_id": "merch_lock",
                "store_id": "store_1",
                "payment_status": "paid",
                "shopify_order_id": stub.shopify_order_id,
                "customer_email": "buyer@example.com",
                "customer_name": "Buyer",
                "shipping_address": {
                    "name": "Buyer", "address_line1": "1 Main St", "address_line2": "",
                    "city": "New Orleans", "state": "LA", "postal_code": "70118",
                    "country": "US", "phone": None,
                },
                "items": [{
                    "product_id": "p_1", "variant_id": "123", "product_title": "Test Product",
                    "quantity": 1, "unit_price": 9.99,
                }],
                "total": 9.99,
                "currency": "USD",
                "payment_intent_id": "pi_123",
                "psp_used": "stripe",
            }

        async def get_merchant_onboarding(merchant_id):
            return {"merchant_id": merchant_id, "psp_type": "stripe"}

        async def get_merchant_active_stores(merchant_id):
            return [{
                "store_id": "store_1", "merchant_id": merchant_id, "platform": "shopify",
                "domain": "shop.myshopify.com", "api_key_raw": "tok", "api_key": "tok",
                "status": "active", "source": "merchant_stores",
            }]

        async def update_fulfillment_info(**kwargs):
            stub.shopify_order_id = kwargs.get("shopify_order_id")
            return True

        async def noop(*_a, **_k):
            return None

        async def ok(**_k):
            return {"ok": True}

        async def shopify_admin_graphql(**_k):
            return {"orders": {"edges": []}}

        class _Resp:
            status_code = 201
            text = "201"
            content = b"{}"

            def json(self):
                return {"order": {"id": 999}}

        async def post(_self, _url, **_kwargs):
            stub.posts += 1
            if stub.posts == 1:
                stub.parked.set()
                await stub.release.wait()
            return _Resp()

        monkeypatch.setattr(order_routes, "get_order", get_order)
        monkeypatch.setattr(order_routes, "get_merchant_onboarding", get_merchant_onboarding)
        monkeypatch.setattr(order_routes, "get_merchant_active_stores", get_merchant_active_stores)
        monkeypatch.setattr(order_routes, "update_fulfillment_info", update_fulfillment_info)
        monkeypatch.setattr(order_routes, "log_order_event", noop)
        monkeypatch.setattr(order_routes, "ensure_external_payment_transaction_best_effort", ok)
        monkeypatch.setattr(orders_db, "update_order", lambda *_a, **_k: noop())
        monkeypatch.setattr(gql, "shopify_admin_graphql", shopify_admin_graphql)
        monkeypatch.setattr(httpx.AsyncClient, "post", post, raising=True)


@pytest.fixture
def order_routes(monkeypatch, pg):
    from routes import order_routes as module

    monkeypatch.setattr(module, "database", pg)
    monkeypatch.setattr(module, "IS_POSTGRES", True)
    return module


async def test_a_second_create_of_an_order_in_flight_does_not_post_to_shopify_again(order_routes, monkeypatch):
    """Confirm-payment and the Stripe webhook racing on one paid order: one Shopify order."""
    order_id = f"ORD_LOCK_{uuid.uuid4().hex[:8]}"
    stub = _ShopifyStub(order_id)
    stub.install(monkeypatch, order_routes)
    lock_id = order_routes._shopify_order_create_lock_key(order_id)

    first = asyncio.create_task(order_routes._create_shopify_order_impl(order_id))
    await _until(stub.parked.is_set)
    assert not await _another_session_can_take(lock_id), (
        "the create lock is not held while the Shopify POST is in flight"
    )

    second = asyncio.create_task(order_routes._create_shopify_order_impl(order_id))
    await asyncio.sleep(0.5)  # the second create is now polling for the first one's outcome
    assert stub.posts == 1

    stub.release.set()
    assert await asyncio.wait_for(first, timeout=10) is True
    assert await asyncio.wait_for(second, timeout=15) is True
    assert stub.posts == 1, "two Shopify orders were created for one paid order"
    assert await _another_session_can_take(lock_id)  # released at the end


async def test_the_create_lock_is_held_for_the_block_and_a_failure_inside_it_surfaces_as_itself(order_routes):
    lock_id = 7_000_000_000 + uuid.uuid4().int % 1_000_000

    async with order_routes._pg_advisory_lock_best_effort(lock_key=lock_id) as got:
        assert got is True
        assert not await _another_session_can_take(lock_id)
    assert await _another_session_can_take(lock_id)

    class ShopifyDown(Exception):
        pass

    # A `yield` in the old best-effort `except` turned this into
    # "RuntimeError: generator didn't stop after athrow()", which is what the order's
    # shopify_order_error event then recorded instead of the real failure.
    with pytest.raises(ShopifyDown):
        async with order_routes._pg_advisory_lock_best_effort(lock_key=lock_id):
            raise ShopifyDown("503 from Shopify")
    assert await _another_session_can_take(lock_id)  # and it still lets go


# ---------------------------------------------------------------------------------------------
# Scheduled audit: per-merchant leader election.
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def audit_job(monkeypatch, pg):
    import jobs.scheduled_audit_job as job

    return job


async def test_the_audit_lock_is_held_for_the_whole_audit(audit_job, monkeypatch):
    merchant_id = f"merch_{uuid.uuid4().hex[:8]}"
    lock_id = audit_job._advisory_lock_id_for_merchant(merchant_id)
    parked, release = asyncio.Event(), asyncio.Event()

    async def audit_body(**kwargs):
        parked.set()
        await release.wait()
        return {**kwargs["summary"], "status": "succeeded"}

    monkeypatch.setattr(audit_job, "_re_audit_merchant_locked", audit_body)
    due = {"merchant_id": merchant_id, "cadence_days": 7, "last_audit_run_id": "run-old"}

    run = asyncio.create_task(audit_job._re_audit_merchant(due))
    await _until(parked.is_set)
    assert not await _another_session_can_take(lock_id), (
        "the merchant's audit lock is not held while its audit runs"
    )
    # What a second pod's tick does with the same due merchant.
    skipped = await audit_job._re_audit_merchant(due)
    assert skipped["status"] == "skipped" and "advisory lock" in skipped["reason"]

    release.set()
    assert (await asyncio.wait_for(run, timeout=10))["status"] == "succeeded"
    assert await _another_session_can_take(lock_id)


async def test_two_audits_of_one_merchant_in_one_tick_run_once(audit_job, monkeypatch, pg):
    """The tick's audits are gathered child tasks; `_list_due_merchants` already used the
    database in the parent, so every child shares the parent's databases Connection. A lock on
    that (pinned) connection would be one Postgres session for all of them -- re-entrant, so
    both would run. On its own connection, the second one is refused."""
    merchant_id = f"merch_{uuid.uuid4().hex[:8]}"
    ran: list = []

    async def list_due():
        await pg.fetch_one("SELECT 1")  # the parent context now owns a databases Connection
        due = {"merchant_id": merchant_id, "cadence_days": 7, "last_audit_run_id": "run-old"}
        return [dict(due), dict(due)]

    async def audit_body(**kwargs):
        ran.append(kwargs["due"]["merchant_id"])
        await asyncio.sleep(0.3)  # still running when the sibling tries the lock
        return {**kwargs["summary"], "status": "succeeded"}

    monkeypatch.setattr(audit_job, "_list_due_merchants", list_due)
    monkeypatch.setattr(audit_job, "_re_audit_merchant_locked", audit_body)

    out = await audit_job.run_scheduled_audits()

    assert ran == [merchant_id], f"the merchant was audited {len(ran)} times in one tick"
    assert (out["succeeded"], out["skipped"]) == (1, 1)
    assert await _another_session_can_take(audit_job._advisory_lock_id_for_merchant(merchant_id))


async def test_an_audit_that_cannot_check_the_lock_is_not_run(audit_job, monkeypatch):
    """It used to fail OPEN (audit unlocked), and a double audit bills the merchant twice. The
    merchant is still due at the next daily tick; `error` puts it in the tick's errored count."""
    import db.session_advisory_lock as lock_module

    ran: list = []

    async def audit_body(**kwargs):
        ran.append(kwargs["due"]["merchant_id"])
        return {**kwargs["summary"], "status": "succeeded"}

    async def refused():
        raise OSError("connection refused")

    monkeypatch.setattr(audit_job, "_re_audit_merchant_locked", audit_body)
    monkeypatch.setattr(lock_module, "_open_connection", refused)

    out = await audit_job._re_audit_merchant(
        {"merchant_id": f"merch_{uuid.uuid4().hex[:8]}", "cadence_days": 7, "last_audit_run_id": "run-old"}
    )
    assert out["status"] == "error" and "lock unavailable" in out["reason"]
    assert ran == []


# ---------------------------------------------------------------------------------------------
# External-seed materialization tick.
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def materialization(monkeypatch, pg):
    import jobs.external_seed_catalog_materialization_job as job

    monkeypatch.setenv(job.ENV_ENABLED, "true")
    return job


def _stub_materialization(monkeypatch, job, *, parked=None, release=None):
    applied: list = []

    async def schema():
        return {"ok": True}

    async def missing():
        return 3

    async def with_sig():
        return 3

    async def apply(limit):
        applied.append(limit)
        if parked is not None:
            parked.set()
            await release.wait()
        return {"inserted": 3, "vertical_guard": {"should_fail": False}}

    monkeypatch.setattr(job, "_required_schema", schema)
    monkeypatch.setattr(job, "_count_missing_mirrors", missing)
    monkeypatch.setattr(job, "_count_mirrors_with_signature", with_sig)
    monkeypatch.setattr(job, "_apply_mirror", apply)
    return applied


async def test_the_materialization_lock_is_held_for_the_whole_tick(materialization, monkeypatch):
    job = materialization
    parked, release = asyncio.Event(), asyncio.Event()
    applied = _stub_materialization(monkeypatch, job, parked=parked, release=release)
    assert await _another_session_can_take(job._JOB_LOCK_ID)  # free before the tick

    tick = asyncio.create_task(job.run_external_seed_catalog_materialization_tick())
    await _until(parked.is_set)
    assert not await _another_session_can_take(job._JOB_LOCK_ID), (
        "the materialization lock is not held while the tick applies"
    )
    # Another instance's tick, fired while this one is mid-apply.
    overlap = await job.run_external_seed_catalog_materialization_tick()
    assert overlap == {"ok": True, "skipped": "lock_not_acquired", "applied": False}

    release.set()
    assert (await asyncio.wait_for(tick, timeout=10))["applied"] is True
    assert len(applied) == 1
    assert await _another_session_can_take(job._JOB_LOCK_ID)


async def test_a_materialization_tick_that_cannot_check_the_lock_does_not_apply(materialization, monkeypatch):
    """It used to fail OPEN (apply unlocked); a skipped tick costs one 15-minute interval."""
    import db.session_advisory_lock as lock_module

    job = materialization
    applied = _stub_materialization(monkeypatch, job)

    async def refused():
        raise OSError("connection refused")

    monkeypatch.setattr(lock_module, "_open_connection", refused)

    out = await job.run_external_seed_catalog_materialization_tick()
    assert out == {"ok": False, "skipped": "lock_unavailable", "applied": False}
    assert applied == []
