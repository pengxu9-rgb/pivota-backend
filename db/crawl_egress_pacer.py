"""db/crawl_egress_pacer.py -- the shared crawl-egress request schedule (migration 251).

ONE STATEMENT PER LEASE. `lease_slots` is a single upsert that advances the bucket's schedule by
`slots / rate` seconds and returns where the leased run of slots ENDS plus the DB clock at that
moment. The caller (services/shopify_edge_pacer.py) turns that into `slots` evenly spaced local
start times. Nothing else is ever read or written, so the table's whole load is one indexed
single-row upsert per lease — see that module for the worst-case queries-per-second.

THE DB CLOCK, NOT THE PROCESS CLOCK. Every instant in the row is the database's own
(`clock_timestamp()` on Postgres), and the statement returns the DB clock alongside the schedule,
so a process only ever uses the DIFFERENCE of two DB readings. Two Cloud Run tasks with skewed wall
clocks still share one timeline.

TWO DIALECTS, ONE SHAPE. Postgres (prod, the dialect gate) spells "now" as
`EXTRACT(EPOCH FROM clock_timestamp())` and the max as GREATEST; SQLite (hermetic tests only) as
`julianday('now')` and MAX. The CREATE is character-for-character the migration on both, and a
test pins that.

NO `_DDL_LOCK`. The one caller single-flights its lease per process (only one lease is ever in
flight, see `shopify_edge_pacer._refill`), so nothing here contends; a module-level asyncio.Lock
would only add the bound-to-another-loop hazard documented against the other `_DDL_LOCK`s.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from db._ddl_guard import apply_ddl_statements
from db.database import IS_POSTGRES
from db.schema_guard import guarded_statements

logger = logging.getLogger(__name__)

TABLE = "crawl_egress_pacer"

_DDL_READY = False

_CREATE = """
    CREATE TABLE IF NOT EXISTS crawl_egress_pacer (
      bucket           TEXT PRIMARY KEY,
      next_free_epoch  DOUBLE PRECISION NOT NULL,
      leases           BIGINT NOT NULL DEFAULT 0,
      updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """

_DDL_STATEMENTS = guarded_statements([_CREATE])

_PG_NOW = "CAST(EXTRACT(EPOCH FROM clock_timestamp()) AS DOUBLE PRECISION)"
_SQLITE_NOW = "((julianday('now') - 2440587.5) * 86400.0)"

_LEASE_TEMPLATE = """
    INSERT INTO crawl_egress_pacer AS p (bucket, next_free_epoch, leases, updated_at)
    VALUES (:bucket, {now} + CAST(:span AS DOUBLE PRECISION), 1, CURRENT_TIMESTAMP)
    ON CONFLICT (bucket) DO UPDATE
       SET next_free_epoch = {greatest}(p.next_free_epoch, {now}) + CAST(:span AS DOUBLE PRECISION),
           leases = p.leases + 1,
           updated_at = CURRENT_TIMESTAMP
    RETURNING next_free_epoch, {now} AS db_now
    """

LEASE_SQL_POSTGRES = _LEASE_TEMPLATE.format(now=_PG_NOW, greatest="GREATEST")
LEASE_SQL_SQLITE = _LEASE_TEMPLATE.format(now=_SQLITE_NOW, greatest="MAX")


async def ensure_table() -> bool:
    """Create the table if it is missing. True once it is known to exist; memoized only after the
    statement succeeded, so a failure retries (paced by `apply_ddl_statements`' cooldown)."""
    global _DDL_READY
    if _DDL_READY:
        return True
    from db.database import database

    _DDL_READY = await apply_ddl_statements(
        _DDL_STATEMENTS,
        label="ensure_crawl_egress_pacer",
        logger=logger,
        execute=database.execute,
    )
    return _DDL_READY


async def lease_slots(bucket: str, *, slots: int, rate_per_s: float) -> Tuple[float, float]:
    """Reserve `slots` request slots on `bucket` at `rate_per_s`. ONE round-trip.

    Returns `(start_epoch, db_now_epoch)`, both DB-clock seconds: the leased slots start at
    `start_epoch + i / rate_per_s` for i in range(slots). `start_epoch` is never earlier than the
    DB's "now" at the time of the statement (GREATEST), so an idle bucket never hands out slots in
    the past — there is no stored burst to spend.

    Raises on any DB error; the caller decides what failing open means.
    """
    if slots < 1 or not rate_per_s > 0:
        raise ValueError("a lease needs at least one slot and a positive rate")
    # A failed CREATE does not stop the lease. Two crawl jobs starting together both run the
    # first-use CREATE, and Postgres can fail the loser of that race (a duplicate pg_type row)
    # although the table now exists. The upsert below is the real test: it raises if the table is
    # truly missing, and the caller fails open on that.
    await ensure_table()
    from db.database import database

    span = float(slots) / float(rate_per_s)
    row = await database.fetch_one(
        LEASE_SQL_POSTGRES if IS_POSTGRES else LEASE_SQL_SQLITE,
        {"bucket": bucket, "span": span},
    )
    if row is None:
        raise RuntimeError("crawl_egress_pacer lease returned no row")
    mapping = getattr(row, "_mapping", row)
    next_free = float(mapping["next_free_epoch"])
    db_now = float(mapping["db_now"])
    return next_free - span, db_now


def reset_for_tests(ready: Optional[bool] = None) -> None:
    """Forget the memoized DDL state. Tests only."""
    global _DDL_READY
    _DDL_READY = bool(ready)
