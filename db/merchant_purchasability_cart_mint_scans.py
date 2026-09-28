"""The purchasability sweep's CART-MINT population, cached: one row per scan of the seed table.

WHY THIS EXISTS (2026-09-28). The sweep's cart-mint lane (jobs/merchant_purchasability_sweep.
`_cart_mint_lane`) pages every active seed — ~23.8k rows with their `seed_data` — through the
cart minter. The sweep runs 21 times a day on the 2-vCPU prod primary, where database load
already caused serving timeouts on 2026-09-26/27. Peng asked for the scan to run at most once a
day and for the other runs to read its result. The job is a fresh process every hour, so the
result has to live in the database; this table is that cache.

One row per ATTEMPT, complete or not, so the job can tell "scanned today" from "tried and
failed an hour ago" (see the policy in the sweep module). `hosts` is the lane's result as JSON
TEXT — `[[host, market, seeds], ...]`, a few KB — so the same SQL runs on SQLite and Postgres.
Rows are pruned after `RETENTION_DAYS`.

Migrations do not self-apply in prod; `ensure_table()` runs the same CREATE at first use, exactly
like db/scheduler_job_slots.py. db/migrations/245_merchant_purchasability_cart_mint_scans.sql is
the record, and what the SQL gates plan against.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from db._ddl_guard import apply_ddl_statements
from db.schema_guard import guarded_statements

logger = logging.getLogger(__name__)

TABLE = "merchant_purchasability_cart_mint_scans"
RETENTION_DAYS = 14

_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

# Mirrors db/migrations/245_merchant_purchasability_cart_mint_scans.sql, character for character
# inside the statement (tests/test_merchant_purchasability_postgres.py compares them).
_DDL_STATEMENTS = guarded_statements([
    """
    CREATE TABLE IF NOT EXISTS merchant_purchasability_cart_mint_scans (
      scan_id        TEXT PRIMARY KEY,
      scanned_at     TIMESTAMPTZ NOT NULL,
      complete       BOOLEAN NOT NULL,
      reason         TEXT,
      seeds_scanned  INTEGER NOT NULL DEFAULT 0,
      cart_seeds     INTEGER NOT NULL DEFAULT 0,
      elapsed_ms     INTEGER NOT NULL DEFAULT 0,
      hosts          TEXT NOT NULL
    )
    """,
])

_LATEST_SQL = """
SELECT scan_id, scanned_at, complete, reason, seeds_scanned, cart_seeds, elapsed_ms, hosts
  FROM merchant_purchasability_cart_mint_scans
 ORDER BY scanned_at DESC
 LIMIT 1
"""

_LATEST_COMPLETE_SQL = """
SELECT scan_id, scanned_at, complete, reason, seeds_scanned, cart_seeds, elapsed_ms, hosts
  FROM merchant_purchasability_cart_mint_scans
 WHERE complete = :complete
 ORDER BY scanned_at DESC
 LIMIT 1
"""

_INSERT_SQL = """
INSERT INTO merchant_purchasability_cart_mint_scans
       (scan_id, scanned_at, complete, reason, seeds_scanned, cart_seeds, elapsed_ms, hosts)
VALUES (:scan_id, :scanned_at, :complete, :reason, :seeds_scanned, :cart_seeds, :elapsed_ms, :hosts)
RETURNING scan_id
"""

_PRUNE_SQL = """
DELETE FROM merchant_purchasability_cart_mint_scans WHERE scanned_at < :before
"""


async def ensure_table() -> bool:
    """Create the table if it is missing. True once it is known to exist. Memoizes only after
    the statement succeeded, so a failure retries on a later call."""
    global _DDL_READY
    if _DDL_READY:
        return True
    async with _DDL_LOCK:
        if _DDL_READY:
            return True
        from db.database import database

        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_merchant_purchasability_cart_mint_scans",
            logger=logger,
            execute=database.execute,
        )
    return _DDL_READY


def _aware(value: Any) -> Optional[datetime]:
    """A stored timestamp as an aware UTC datetime. asyncpg hands back an aware datetime; SQLite
    hands back the ISO text it was given."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _row(record: Any) -> Dict[str, Any]:
    row = dict(record)
    row["scanned_at"] = _aware(row.get("scanned_at"))
    row["complete"] = bool(row.get("complete"))
    try:
        hosts = json.loads(row.get("hosts") or "[]")
    except (TypeError, ValueError):
        hosts = []
    row["hosts"] = {
        (str(h[0]), str(h[1])): int(h[2])
        for h in hosts if isinstance(h, list) and len(h) == 3
    }
    return row


async def latest(*, complete_only: bool) -> Optional[Dict[str, Any]]:
    """The newest scan row (optionally the newest COMPLETE one), or None. RAISES on a read error:
    the sweep treats an unreadable cache as "cannot tell", never as "nothing cached"."""
    from db.database import database

    if complete_only:
        record = await database.fetch_one(_LATEST_COMPLETE_SQL, {"complete": True})
    else:
        record = await database.fetch_one(_LATEST_SQL)
    return _row(record) if record is not None else None


async def record(
    *,
    scan_id: str,
    scanned_at: datetime,
    complete: bool,
    reason: str,
    seeds_scanned: int,
    cart_seeds: int,
    elapsed_ms: int,
    hosts: Dict[Tuple[str, str], int],
) -> None:
    """Write one scan, then prune rows past retention. RAISES if the insert did not land."""
    from db.database import database

    payload = json.dumps(sorted([host, market, int(seeds)] for (host, market), seeds in hosts.items()))
    landed = await database.fetch_one(_INSERT_SQL, {
        "scan_id": scan_id, "scanned_at": scanned_at, "complete": bool(complete),
        "reason": reason or None, "seeds_scanned": int(seeds_scanned),
        "cart_seeds": int(cart_seeds), "elapsed_ms": int(elapsed_ms), "hosts": payload,
    })
    if landed is None:
        raise RuntimeError("the cart-mint scan row did not land")
    try:
        await database.execute(_PRUNE_SQL, {"before": scanned_at - timedelta(days=RETENTION_DAYS)})
    except Exception as exc:  # noqa: BLE001 — pruning is housekeeping; the scan is recorded
        logger.warning("merchant_purchasability_cart_mint_scans: prune failed (error_type=%s)",
                       type(exc).__name__)


def _reset_for_tests() -> None:
    global _DDL_READY
    _DDL_READY = False


__all__: List[str] = ["TABLE", "RETENTION_DAYS", "ensure_table", "latest", "record"]
