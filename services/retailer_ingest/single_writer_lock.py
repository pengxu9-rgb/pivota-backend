"""Single-writer advisory lock for the StyleKorean retailer-ingest CLIs.

Two concurrent ingests against the Railway public proxy caused connect-timeout
storms (2026-07-16). These CLIs are singletons by design, so guard the whole DB
phase with a Postgres session-level advisory lock: the second run's
`pg_try_advisory_lock` returns false and it exits cleanly instead of piling onto
the pool.

The lock is acquired on a PINNED connection held open for the run's lifetime
(`databases` reuses that connection for every nested `database.execute` in the
same task), so the advisory lock — which is session-scoped — is genuinely held
across all the ingest work, and mutual exclusion holds even though other queries
would otherwise be scattered across pool connections.

SQLite (unit tests) has no advisory locks; the lock is a no-op there.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator

from db.database import IS_POSTGRES

logger = logging.getLogger("retailer_ingest.single_writer_lock")

LOCK_NAME = "stylekorean_retailer_ingest"


class SingleWriterLockError(RuntimeError):
    """Raised when another retailer-ingest run already holds the lock."""


@contextlib.asynccontextmanager
async def retailer_ingest_lock(database: Any, *, lock_name: str = LOCK_NAME) -> AsyncIterator[bool]:
    """Hold the single-writer advisory lock for the enclosed DB work.

    Yields True when the lock is held (Postgres) or when locking is skipped
    (non-Postgres). Raises SingleWriterLockError if another run holds the lock."""
    if not IS_POSTGRES:
        # sqlite / tests: advisory locks don't exist — never block.
        yield False
        return

    async with database.connection() as conn:
        row = await conn.fetch_one(
            "SELECT pg_try_advisory_lock(hashtext(:k)) AS locked", {"k": lock_name}
        )
        acquired = bool(row and dict(row).get("locked"))
        if not acquired:
            raise SingleWriterLockError("another retailer-ingest run holds the lock")
        logger.info("retailer_ingest_lock acquired (%s)", lock_name)
        try:
            yield True
        finally:
            try:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext(:k))", {"k": lock_name}
                )
                logger.info("retailer_ingest_lock released (%s)", lock_name)
            except Exception as exc:  # noqa: BLE001 — closing the conn releases it anyway
                logger.warning("retailer_ingest_lock unlock failed (conn close will release): %s", str(exc)[:200])
