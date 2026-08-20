"""A saturated pool must FAIL, not hang forever.

WHAT THIS PREVENTS (2026-08-20). `databases` 0.7.0 checks a connection out with
a bare `await self._database._pool.acquire()`, and `asyncpg.Pool.acquire`
defaults to `timeout=None` — wait forever. So a full pool did not degrade, it
stopped. One report query averaging 125s over 1,374 calls filled all 20 slots
and every scheduler job hung silently until it burned its own deadline: 705
`maximum number of running instances`, 66 `JobDeadlineExceeded`, and **zero
database errors**, because nothing failed — it just never returned. HTTP starved
alongside them and the sitemap cron took a 504.

The silence is the dangerous part. A bounded checkout converts that into a
loud, attributable `TimeoutError` on the callers that cannot be served, while
the ones holding connections finish normally.

POSTGRES GATE because the behaviour lives in asyncpg's pool. The SQLite backend
has no pool and no checkout, so a SQLite test would assert nothing.

🚨 THESE GATE FILES SHARE ONE DATABASE. This one creates no tables and writes no
rows — it only opens its own `Database` objects against the same URL.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — the pool lives in asyncpg",
)


@pytest.mark.asyncio
async def test_a_saturated_pool_times_out_instead_of_hanging(monkeypatch) -> None:
    """Hold every slot, then prove a further caller fails on a deadline.

    The deadline is shortened to 0.5s so the test is fast; the patched acquire
    reads the module global at call time, which is what makes that possible.
    The PRODUCTION default (30s) is asserted separately below — a test that only
    ever ran with a tiny value would not notice the default going to zero.
    """
    from databases import Database

    import db.database as dbmod

    assert dbmod._install_bounded_pool_checkout(), "patch did not install"
    monkeypatch.setattr(dbmod, "DB_POOL_CHECKOUT_TIMEOUT_SECONDS", 0.5)

    db = Database(DATABASE_URL, min_size=1, max_size=1)
    await db.connect()
    try:
        holder_started = asyncio.Event()

        async def holder() -> None:
            # Occupies the single slot for longer than the checkout deadline.
            async with db.connection():
                holder_started.set()
                await asyncio.sleep(3.0)

        task = asyncio.create_task(holder())
        await asyncio.wait_for(holder_started.wait(), timeout=5)

        # A second caller in its OWN context cannot inherit the held connection,
        # so it must queue for the pool — and now give up.
        captured: list = []

        async def starved() -> str:
            t0 = time.monotonic()
            try:
                await db.fetch_val("SELECT 1")
                return "served"
            except (asyncio.TimeoutError, TimeoutError) as exc:
                captured.append(exc)
                return f"timed_out_after_{time.monotonic() - t0:.1f}s"

        # A separate task: it gets its own copy of the context, so it cannot
        # inherit the Connection the holder pinned and must queue for the pool.
        result = await asyncio.wait_for(asyncio.create_task(starved()), timeout=8)
        await task
        # It must have failed on the POOL's 0.5s deadline, well before the
        # holder released at 3s — otherwise it was simply served late.
        assert "timed_out_after_0" in result or "timed_out_after_1" in result, result

        # The TYPE the patch really raises — asserted on the live exception, not
        # on one built by hand. A hand-built PoolCheckoutTimeout proves only that
        # the class exists; a mutant raising a bare TimeoutError from the patch
        # survived exactly that weaker test. A bare TimeoutError has an empty
        # message, matches no substring classifier, and reaches clients as a 500.
        from utils.transient_errors import PoolCheckoutTimeout, is_asyncpg_busy_error

        assert captured, "no exception captured"
        assert isinstance(captured[0], PoolCheckoutTimeout), (
            f"pool saturation raised {type(captured[0]).__name__}, which no "
            "classifier recognises — it would surface as a 500, not a 503"
        )
        assert is_asyncpg_busy_error(captured[0]), "not routed to the 503 vocabulary"

        assert result.startswith("timed_out"), (
            f"expected a bounded failure, got {result!r} — an unbounded acquire "
            "would have blocked until the holder released"
        )
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_the_deadline_does_not_fire_on_a_healthy_pool() -> None:
    """The bound must be invisible when slots are available.

    NOT a guard against too-small a deadline — a mutant setting the default to
    0.1s leaves this GREEN (`test_the_production_default_is_a_real_bound` is
    what catches that). What this kills is "acquire always raises": a patch that
    turned every checkout into a timeout would pass the saturation test above
    and fail here.
    """
    from databases import Database

    db = Database(DATABASE_URL, min_size=2, max_size=5)
    await db.connect()
    try:
        results = await asyncio.gather(*[db.fetch_val("SELECT 1") for _ in range(10)])
        assert results == [1] * 10
    finally:
        await db.disconnect()


def test_the_patch_preserves_the_asserts_recovery_depends_on() -> None:
    """`_pool_is_provably_dead` keys off "DatabaseBackend is not running".

    That assert is the only thing distinguishing a dead pool from a slow one in
    utils/database_readiness, so a patch that dropped it would silently disable
    request-path recovery for the 12.5h-outage shape.
    """
    import inspect

    import db.database  # noqa: F401 — installs the patch; do NOT rely on test order
    from databases.backends.postgres import PostgresConnection

    src = inspect.getsource(PostgresConnection.acquire)
    assert "Connection is already acquired" in src
    assert "DatabaseBackend is not running" in src
    assert "timeout=" in src, "checkout is unbounded again"


def test_the_production_default_is_a_real_bound() -> None:
    """A default of 0 would fail every query; a huge one restores the hang."""
    import db.database as dbmod

    assert 1.0 <= dbmod.DB_POOL_CHECKOUT_TIMEOUT_SECONDS <= 120.0, (
        f"implausible checkout deadline: {dbmod.DB_POOL_CHECKOUT_TIMEOUT_SECONDS}"
    )


def test_the_process_actually_installs_the_patch_on_import() -> None:
    """Importing `db.database` with a Postgres URL must leave checkout bounded.

    THE TEST THIS FILE WAS MISSING, and its absence was the sharpest finding in
    review: replacing `if IS_POSTGRES:` with `if False:` left all four other
    tests passing, because test 1 calls `_install_bounded_pool_checkout()`
    itself. They proved the FUNCTION works and never that the PROCESS runs it —
    on a change whose entire value is being installed.

    A subprocess is required: this interpreter has already imported the module,
    so any in-process check is satisfied by whoever imported it first.
    """
    import subprocess
    import sys

    probe = (
        "import db.database;"
        "from databases.backends.postgres import PostgresConnection as C;"
        "import sys;"
        "sys.exit(0 if getattr(C.acquire, '_pivota_bounded', False) else 3)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        env={**os.environ, "DATABASE_URL": DATABASE_URL},
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "importing db.database did not bound pool checkout "
        f"(rc={proc.returncode}): {proc.stderr.decode()[-400:]}"
    )


def test_a_checkout_timeout_is_classified_retryable_not_a_server_error() -> None:
    """Saturation must reach clients as 503 + Retry-After, never a 500.

    asyncpg raises a BARE `TimeoutError` whose `str()` is empty, and every
    classifier in utils.transient_errors matches on substrings — so before the
    typed `PoolCheckoutTimeout` this sailed past all of them into
    `_handle_unexpected_error` and surfaced as 500 INTERNAL_SERVER_ERROR. That
    is loud in the WRONG channel: it pages as a code bug rather than DB
    capacity, and it hands agent/partner clients a non-retryable status for an
    entirely retryable state.
    """
    from utils.transient_errors import (
        PoolCheckoutTimeout,
        db_busy_http_exception,
        is_asyncpg_busy_error,
        is_pool_checkout_timeout,
    )

    err = PoolCheckoutTimeout("timed out waiting 120.0s for a database connection")

    assert isinstance(err, TimeoutError), (
        "must stay a TimeoutError subclass so callers already catching it "
        "(the canonical route's _bounded_db) keep working"
    )
    assert is_pool_checkout_timeout(err)
    assert is_asyncpg_busy_error(err), "not routed to the existing 503 vocabulary"
    # and it survives being wrapped, which is how it reaches most handlers
    wrapped = RuntimeError("query failed")
    wrapped.__cause__ = err
    assert is_asyncpg_busy_error(wrapped)
    assert db_busy_http_exception().status_code == 503
