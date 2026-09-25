"""Holder tracking against a real asyncpg pool: every connection out is recorded, every one returned
is forgotten, and a leaked one stays visible with the code that took it.

POSTGRES GATE because checkout and release live in asyncpg's pool; SQLite has neither.

🚨 THESE GATE FILES SHARE ONE DATABASE. This one creates no tables and writes no rows — it opens its
own `Database` objects against the same URL, like test_pool_checkout_is_bounded_postgres.py.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import random

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — checkout and release live in asyncpg",
)

THIS_FILE = os.path.basename(__file__)


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    import db.database as dbmod
    import db.pool_holders as holders

    assert dbmod._install_bounded_pool_checkout(), "patch did not install"
    fresh = holders.HolderRegistry()
    monkeypatch.setattr(holders, "REGISTRY", fresh)
    monkeypatch.setattr(dbmod, "DB_POOL_HOLDER_TRACKING", True)
    return fresh


def _in_use(db) -> int:
    pool = db._backend._pool
    return pool.get_size() - pool.get_idle_size()


async def _take_and_hold(db, entered: asyncio.Event, leave: asyncio.Event) -> None:
    async with db.connection() as conn:
        await conn.fetch_val("SELECT 1")
        entered.set()
        await leave.wait()


_TAKE_LINE = _take_and_hold.__code__.co_firstlineno + 1


@pytest.mark.asyncio
async def test_a_held_connection_is_recorded_at_the_line_that_took_it(registry) -> None:
    from databases import Database

    db = Database(DATABASE_URL, min_size=1, max_size=2)
    await db.connect()
    try:
        entered, leave = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(_take_and_hold(db, entered, leave))
        await asyncio.wait_for(entered.wait(), timeout=5)

        snap = registry.snapshot()
        assert snap["checked_out"] == 1 == _in_use(db)
        assert snap["by_site"][0]["site"].split(" <- ")[0] == f"{THIS_FILE}:{_TAKE_LINE}"

        leave.set()
        await asyncio.wait_for(task, timeout=5)
        assert registry.snapshot()["checked_out"] == 0 == _in_use(db)
    finally:
        # Release the holder even when an assertion above failed; `disconnect()` waits for it.
        leave.set()
        await asyncio.wait_for(db.disconnect(), timeout=10)


@pytest.mark.asyncio
async def test_a_leaked_connection_stays_visible_and_is_reported_once(registry, monkeypatch, caplog) -> None:
    """The 2026-09-16 shape: a checkout nobody will ever release, and no task parked on it."""
    from databases import Database

    import db.database as dbmod

    monkeypatch.setattr(dbmod, "DB_POOL_HOLDER_WARN_SECONDS", 5.0)
    db = Database(DATABASE_URL, min_size=1, max_size=3)
    await db.connect()
    try:
        leaked = db.connection()
        await leaked.__aenter__()          # entered, never exited: the leak
        del leaked

        assert registry.snapshot()["checked_out"] == 1 == _in_use(db)
        for record in registry._live.values():  # age it past the threshold
            record[1].acquired_at -= 10.0

        async def other_request() -> None:
            await db.fetch_val("SELECT 1")

        with caplog.at_level(logging.WARNING, logger="db.pool_holders"):
            for _ in range(3):
                # The warning rides on later checkouts. A FRESH context, like a new request:
                # `databases` 0.7.0 keeps the Connection in a ContextVar, so a query in this
                # task would reuse the leaked connection and never check a new one out.
                await asyncio.create_task(other_request(), context=contextvars.Context())
        overdue = [r for r in caplog.records if "without release" in r.getMessage()]
        assert len(overdue) == 1
        assert f"{THIS_FILE}:" in overdue[0].getMessage()
        assert registry.snapshot()["checked_out"] == 1 == _in_use(db)
    finally:
        # `disconnect()` would wait forever for the leaked connection to come back.
        db._backend._pool.terminate()


@pytest.mark.asyncio
async def test_the_registry_agrees_with_the_pool_through_cancellations(registry) -> None:
    """Records and pool counts must not drift under the churn prod sees: queries cut off by
    timeouts and cancelled tasks. Drift in either direction would report phantom leaks or hide
    real ones."""
    from databases import Database

    rng = random.Random(1234)
    db = Database(DATABASE_URL, min_size=1, max_size=8)
    await db.connect()
    try:
        async def one() -> None:
            try:
                await asyncio.wait_for(
                    db.fetch_all(f"SELECT pg_sleep({rng.uniform(0.0, 0.02)})"),
                    timeout=rng.uniform(0.0, 0.02),
                )
            except asyncio.TimeoutError:
                pass

        for _ in range(20):
            tasks = [asyncio.create_task(one()) for _ in range(6)]
            await asyncio.sleep(rng.uniform(0.0, 0.01))
            tasks[0].cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.2)
        assert registry.snapshot()["checked_out"] == _in_use(db)
    finally:
        await db.disconnect()


def test_release_tracking_is_installed_with_the_bounded_checkout() -> None:
    from databases.backends.postgres import PostgresConnection

    import db.database as dbmod

    assert dbmod._install_bounded_pool_checkout()
    assert getattr(PostgresConnection.release, "_pivota_tracked", False)
