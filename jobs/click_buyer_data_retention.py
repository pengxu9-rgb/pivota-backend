"""Buyer IP / user-agent retention on click rows (FIX_PLAN O-08).

`/r` writes the buyer's IP and user agent onto `outbound_click_events` (one row per follow) and
`surface_click_events` (the per-click-id accumulator), and nothing ever removed them. This job
DROPS both (sets them NULL) on rows older than CLICK_BUYER_DATA_RETENTION_DAYS (default 30).

Drop, not hash: an unsalted hash of an IPv4 address is reversible by enumerating the 2^32 space,
so it is not anonymisation, and a salted one needs a new secret. Nothing reads these columns back
for attribution (the click id carries it), so the row keeps its value without them.

DORMANT unless CLICK_BUYER_DATA_RETENTION_ENABLED is literally 'true'. It is NOT registered on the
scheduler: arming it is a separate step (a Cloud Run job, or an entry in services/audit_scheduler.py)
with its own go. Bounded: at most BATCH rows per statement and MAX_BATCHES statements per table per
run, so a first run over a large backlog finishes over several runs.

    python -m jobs.click_buyer_data_retention            # dry run: counts only, writes nothing
    python -m jobs.click_buyer_data_retention --apply    # needs the flag as well
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

ENABLED_FLAG = "CLICK_BUYER_DATA_RETENTION_ENABLED"
DAYS_ENV = "CLICK_BUYER_DATA_RETENTION_DAYS"
DEFAULT_DAYS = 30
MIN_DAYS = 1
BATCH = 1000
MAX_BATCHES = 50


def is_enabled() -> bool:
    return (os.getenv(ENABLED_FLAG) or "").strip().lower() == "true"


def retention_days() -> int:
    """N from CLICK_BUYER_DATA_RETENTION_DAYS; the default on anything unparsable or below 1."""
    raw = (os.getenv(DAYS_ENV) or "").strip()
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_DAYS
    return days if days >= MIN_DAYS else DEFAULT_DAYS


# outbound_click_events.created_at is a naive (UTC) timestamp; surface_click_events' are aware.
# The surface row is updated on every follow, so its LAST write is what the age counts from.
_TABLES = {
    "outbound_click_events": {
        "key": "id",
        "age": "created_at",
        "naive": True,
    },
    "surface_click_events": {
        "key": "click_id",
        "age": "COALESCE(last_click_at, last_impression_at, updated_at, created_at)",
        "naive": False,
    },
}


def _cutoff(now: datetime, days: int, naive: bool) -> datetime:
    cutoff = now.astimezone(timezone.utc) - timedelta(days=days)
    return cutoff.replace(tzinfo=None) if naive else cutoff


def _candidates_sql(table: str, spec: Dict[str, Any]) -> str:
    return (
        f"SELECT {spec['key']} FROM {table} "
        f"WHERE {spec['age']} < :cutoff AND (ip IS NOT NULL OR user_agent IS NOT NULL) "
        f"ORDER BY {spec['key']} LIMIT :batch"
    )


async def run_click_buyer_data_retention(
    *, apply: bool = True, now: Optional[datetime] = None, database: Any = None
) -> Dict[str, Any]:
    """Null ip/user_agent on click rows older than N days. Returns a per-table summary.

    `apply=False` (or the flag off) counts the eligible rows of the first batch and writes nothing.
    """
    if database is None:
        from db.database import database as _db

        database = _db
    now = now or datetime.now(timezone.utc)
    days = retention_days()
    armed = apply and is_enabled()
    summary: Dict[str, Any] = {
        "enabled": is_enabled(),
        "applied": armed,
        "retention_days": days,
        "tables": {},
    }
    for table, spec in _TABLES.items():
        cutoff = _cutoff(now, days, spec["naive"])
        select_sql = _candidates_sql(table, spec)
        params = {"cutoff": cutoff, "batch": BATCH}
        if not armed:
            rows = await database.fetch_all(select_sql, params)
            summary["tables"][table] = {"eligible_in_first_batch": len(rows or []), "cleared": 0}
            continue
        cleared = 0
        batches = 0
        update_sql = (
            f"UPDATE {table} SET ip = NULL, user_agent = NULL "
            f"WHERE {spec['key']} IN ({select_sql}) RETURNING {spec['key']}"
        )
        while batches < MAX_BATCHES:
            rows = await database.fetch_all(update_sql, params)
            n = len(rows or [])
            if not n:
                break
            cleared += n
            batches += 1
            if n < BATCH:
                break
        summary["tables"][table] = {"cleared": cleared, "batches": batches}
    if armed or summary["enabled"]:
        from utils.logger import logger as operator_logger

        operator_logger.warning("click_buyer_data_retention: %s", json.dumps(summary, default=str))
    return summary


async def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write (needs the flag too)")
    args = parser.parse_args(argv)
    from db.database import database

    await database.connect()
    try:
        summary = await run_click_buyer_data_retention(apply=args.apply, database=database)
    finally:
        await database.disconnect()
    print(json.dumps(summary, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(_main()))
