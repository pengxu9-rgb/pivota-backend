"""Where each scheduled cart-proof refresh left off: one row per (lane, domain).

The reader and writer is jobs/reap_cart_proof_refresh.py; the record is
db/migrations/250_reap_cart_proof_refresh_cursors.sql, and `ensure_table()` runs the identical CREATE at
first use (migrations do not self-apply in prod). One statement for both dialects: nothing in it is
Postgres-only.

Every function takes the database object it runs on, so the job can be driven against a stub.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from db._ddl_guard import apply_ddl_statements
from db.schema_guard import guarded_statements

logger = logging.getLogger(__name__)

TABLE = "reap_cart_proof_refresh_cursors"

# Mirrors db/migrations/250_reap_cart_proof_refresh_cursors.sql, character for character inside the statement
# (tests/test_reap_cart_proof_refresh_cursors.py compares them).
_CREATE = """CREATE TABLE IF NOT EXISTS reap_cart_proof_refresh_cursors (
  lane               TEXT NOT NULL,
  domain             TEXT NOT NULL,
  next_cursor        TEXT,
  last_status        TEXT NOT NULL,
  last_completed_at  TIMESTAMPTZ,
  blocked_until      TIMESTAMPTZ,
  crash_count        INTEGER NOT NULL DEFAULT 0,
  updated_at         TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (lane, domain)
)"""

_DDL_STATEMENTS = guarded_statements([_CREATE])

SELECT_CURSORS_SQL = """
    SELECT domain, next_cursor, last_status, last_completed_at, blocked_until, crash_count, updated_at
      FROM reap_cart_proof_refresh_cursors
     WHERE lane = :lane
"""

# `last_completed_at` is only ever moved forward by a completed walk: a later row that did not complete one
# (NULL) keeps the stored value, so the mirror lane's ordering remembers when a store was last walked fully.
UPSERT_CURSOR_SQL = """
    INSERT INTO reap_cart_proof_refresh_cursors
        (lane, domain, next_cursor, last_status, last_completed_at, blocked_until, crash_count, updated_at)
    VALUES
        (:lane, :domain, :next_cursor, :last_status, :last_completed_at, :blocked_until, :crash_count, :updated_at)
    ON CONFLICT (lane, domain) DO UPDATE SET
        next_cursor = excluded.next_cursor,
        last_status = excluded.last_status,
        blocked_until = excluded.blocked_until,
        crash_count = excluded.crash_count,
        last_completed_at = COALESCE(excluded.last_completed_at, reap_cart_proof_refresh_cursors.last_completed_at),
        updated_at = excluded.updated_at
"""


@dataclass(frozen=True)
class CursorRow:
    next_cursor: Optional[str]
    last_status: str
    last_completed_at: Optional[datetime]
    updated_at: Optional[datetime]
    blocked_until: Optional[datetime] = None
    crash_count: int = 0


async def ensure_table(db: Any) -> bool:
    """Create the table if it is missing. True once every statement applied."""
    return await apply_ddl_statements(_DDL_STATEMENTS, label="ensure_reap_cart_proof_refresh_cursors",
                                      logger=logger, execute=db.execute)


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    try:  # SQLite hands a TIMESTAMPTZ back as text
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def load(db: Any, lane: str, *, table_must_exist: bool) -> Dict[str, CursorRow]:
    """domain -> its row. A dry run on an environment where no apply ever ran has no table: that reads as
    no cursors (every store from its first page)."""
    try:
        rows = await db.fetch_all(SELECT_CURSORS_SQL, {"lane": lane})
    except Exception:
        if table_must_exist:
            raise
        logger.warning("%s is not readable; every store starts at its first page", TABLE)
        return {}
    out: Dict[str, CursorRow] = {}
    for raw in rows or []:
        row = dict(raw)
        out[str(row["domain"])] = CursorRow(
            next_cursor=row.get("next_cursor"), last_status=str(row.get("last_status") or ""),
            last_completed_at=_as_datetime(row.get("last_completed_at")),
            updated_at=_as_datetime(row.get("updated_at")),
            blocked_until=_as_datetime(row.get("blocked_until")),
            crash_count=int(row.get("crash_count") or 0))
    return out


async def save(db: Any, *, lane: str, domain: str, next_cursor: Optional[str], last_status: str,
               completed_at: Optional[datetime], now: datetime, blocked_until: Optional[datetime] = None,
               crash_count: int = 0) -> None:
    await db.execute(UPSERT_CURSOR_SQL, {
        "lane": lane, "domain": domain, "next_cursor": next_cursor, "last_status": last_status,
        "last_completed_at": completed_at, "blocked_until": blocked_until, "crash_count": int(crash_count),
        "updated_at": now,
    })


__all__ = ["TABLE", "CursorRow", "ensure_table", "load", "save", "SELECT_CURSORS_SQL", "UPSERT_CURSOR_SQL"]
