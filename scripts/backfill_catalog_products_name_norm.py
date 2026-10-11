"""Backfill catalog_products.name_norm / own_name_norm (migration 260) in bounded, resumable batches.

The two columns are stamped by the triggers of migration 260 from ONE SQL function
(`catalog_products_identity_fold`, the gateway's `identitySql` character for character). This script
never restates that rule: it TOUCHES each unstamped row (`SET title = title`; the UPDATE trigger fires
on `NEW.name_norm IS NULL`) and lets the trigger stamp it, so a row the script backfilled and a row a
writer wrote are folded by the same code. It refuses to run when the triggers are absent.

Safe next to the live writers (drain, refresh, sync): each batch is its own transaction with
`SET LOCAL lock_timeout` and `SET LOCAL statement_timeout`, and picks its rows `FOR UPDATE SKIP
LOCKED`, so it never queues behind a writer's row lock and cannot deadlock with one. A batch that
finds every candidate row locked is reported as `all_locked` (and retried after the pause), which is
different from `stalled` (rows were stamped by the UPDATE but the unstamped count did not fall: the
trigger is not doing its job).

Resumable by construction (every batch takes the next rows `WHERE name_norm IS NULL OR own_name_norm
IS NULL`, ordered by product_key), bounded (`--max-batches`), throttled (`--sleep-ms`), and a DRY RUN
unless `--apply` is given.

`--refold-all`: after a CREATE OR REPLACE of the fold function every stored value is stale and NOT
NULL, so the NULL-driven path would never revisit it. This mode walks the whole table by product_key
(`--after-key` resumes from a cursor the report prints as `last_key`) and sets both columns to NULL in
the same UPDATE, which the UPDATE trigger turns into a fresh fold in the same statement. No SKIP
LOCKED here (a skipped row would be silently left stale); a lock timeout on a batch is reported and
the same cursor is retried after the pause.

Run it on prod from a one-off job (the DB is VPC-only), with pivota-pg under 35% CPU and no index
build or other backfill running, and BEFORE migration 261 (the index is built over stamped rows):

    bash scripts/ops/run_oneoff_job.sh scripts/backfill_catalog_products_name_norm.py            # dry run
    bash scripts/ops/run_oneoff_job.sh scripts/backfill_catalog_products_name_norm.py --apply --batch-size 1000 --max-batches 10 --sleep-ms 500

~28k rows (prod, 2026-10-10): 28 batches of 1,000; each batch is one UPDATE whose trigger runs two
regexp folds per row (~0.3 s). Re-run until `remaining` is 0; a re-run after that touches nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

TRIGGER_NAMES = ("trg_catalog_products_stamp_name_norm_insert", "trg_catalog_products_stamp_name_norm_update")
LOCK_TIMEOUT = "2s"
STATEMENT_TIMEOUT = "30s"

COUNT_SQL = "SELECT count(*) FROM catalog_products WHERE name_norm IS NULL OR own_name_norm IS NULL"

TRIGGERS_PRESENT_SQL = """
SELECT count(DISTINCT t.tgname)
FROM pg_trigger t
WHERE t.tgrelid = to_regclass('catalog_products')
  AND NOT t.tgisinternal
  AND t.tgname = ANY(CAST(:names AS text[]))
"""

# The row is touched, not folded: the UPDATE trigger (WHEN NEW.name_norm IS NULL ...) stamps both
# columns. Candidates are taken FOR UPDATE SKIP LOCKED, so a row a writer holds is left for a later
# batch instead of being waited on.
BATCH_SQL = """
WITH picked AS (
  SELECT product_key
  FROM catalog_products
  WHERE name_norm IS NULL OR own_name_norm IS NULL
  ORDER BY product_key
  LIMIT :batch_size
  FOR UPDATE SKIP LOCKED
), stamped AS (
  UPDATE catalog_products p
  SET title = p.title
  FROM picked
  WHERE p.product_key = picked.product_key
  RETURNING 1
)
SELECT (SELECT count(*) FROM picked) AS picked, (SELECT count(*) FROM stamped) AS stamped
"""

# --refold-all: every row, by key, both columns set to NULL so the UPDATE trigger re-folds them in
# the same statement. Plain FOR UPDATE (no SKIP LOCKED): a skipped row would stay stale unnoticed.
REFOLD_BATCH_SQL = """
WITH picked AS (
  SELECT product_key
  FROM catalog_products
  WHERE product_key > :after_key
  ORDER BY product_key
  LIMIT :batch_size
  FOR UPDATE
), refolded AS (
  UPDATE catalog_products p
  SET name_norm = NULL, own_name_norm = NULL
  FROM picked
  WHERE p.product_key = picked.product_key
  RETURNING p.product_key
)
SELECT (SELECT count(*) FROM picked) AS picked, (SELECT count(*) FROM refolded) AS refolded,
       (SELECT max(product_key) FROM picked) AS last_key
"""


class TriggerMissing(RuntimeError):
    """The stamping triggers are not on catalog_products: migration 260 has not been applied here."""


def _is_lock_timeout(err: BaseException) -> bool:
    name = type(err).__name__
    text = str(err).lower()
    return "LockNotAvailable" in name or "lock timeout" in text or "canceling statement due to lock timeout" in text


async def _batch(db, sql: str, values: Dict[str, Any]):
    async with db.transaction():
        await db.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        await db.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        return await db.fetch_one(sql, values)


async def _require_triggers(db) -> None:
    present = int(await db.fetch_val(TRIGGERS_PRESENT_SQL, {"names": list(TRIGGER_NAMES)}))
    if present != len(TRIGGER_NAMES):
        raise TriggerMissing(
            f"{present} of {len(TRIGGER_NAMES)} stamping triggers on catalog_products; apply migration 260 first"
        )


async def run_backfill(
    *, apply: bool, batch_size: int = 1000, max_batches: int = 10, sleep_ms: int = 500, db=database
) -> Dict[str, Any]:
    batch_size = max(1, int(batch_size))
    max_batches = max(1, int(max_batches))
    await _require_triggers(db)
    before = int(await db.fetch_val(COUNT_SQL))
    report: Dict[str, Any] = {
        "mode": "backfill_unstamped",
        "apply": apply,
        "batch_size": batch_size,
        "max_batches": max_batches,
        "sleep_ms": sleep_ms,
        "lock_timeout": LOCK_TIMEOUT,
        "statement_timeout": STATEMENT_TIMEOUT,
        "unstamped_before": before,
        "batches_needed": (before + batch_size - 1) // batch_size,
        "batches_run": 0,
        "rows_touched": 0,
        "all_locked_batches": 0,
        "lock_timeouts": 0,
    }
    if not apply:
        report["remaining"] = before
        return report
    started = time.time()
    remaining = before
    last_outcome = None
    for _ in range(max_batches):
        if remaining <= 0:
            break
        report["batches_run"] += 1
        try:
            row = await _batch(db, BATCH_SQL, {"batch_size": batch_size})
        except Exception as err:  # noqa: BLE001 - a lock timeout is an expected outcome, reported
            if not _is_lock_timeout(err):
                raise
            report["lock_timeouts"] += 1
            last_outcome = "lock_timeout"
            if sleep_ms > 0:
                await asyncio.sleep(sleep_ms / 1000.0)
            continue
        picked = int(row["picked"] or 0)
        after = int(await db.fetch_val(COUNT_SQL))
        if picked == 0:
            # Candidates exist (remaining > 0) but every one is locked by a writer right now.
            report["all_locked_batches"] += 1
            last_outcome = "all_locked"
        else:
            touched = remaining - after
            report["rows_touched"] += max(0, touched)
            last_outcome = "stamped"
            if touched <= 0:
                # Rows were picked and updated, yet none got stamped: the trigger is not doing its job.
                report["stalled"] = True
                remaining = after
                break
        remaining = after
        if remaining > 0 and sleep_ms > 0:
            await asyncio.sleep(sleep_ms / 1000.0)
    report["remaining"] = remaining
    # The run ENDED on rows a writer holds: nothing wrong, come back later (exit 3 tells the operator).
    report["all_locked"] = bool(remaining > 0 and last_outcome == "all_locked")
    report["elapsed_s"] = round(time.time() - started, 2)
    return report


async def run_refold_all(
    *, apply: bool, batch_size: int = 1000, max_batches: int = 10, sleep_ms: int = 500, after_key: str = "", db=database
) -> Dict[str, Any]:
    batch_size = max(1, int(batch_size))
    max_batches = max(1, int(max_batches))
    await _require_triggers(db)
    total = int(await db.fetch_val("SELECT count(*) FROM catalog_products WHERE product_key > :k", {"k": after_key}))
    report: Dict[str, Any] = {
        "mode": "refold_all",
        "apply": apply,
        "batch_size": batch_size,
        "max_batches": max_batches,
        "sleep_ms": sleep_ms,
        "lock_timeout": LOCK_TIMEOUT,
        "statement_timeout": STATEMENT_TIMEOUT,
        "after_key": after_key,
        "rows_ahead": total,
        "batches_needed": (total + batch_size - 1) // batch_size,
        "batches_run": 0,
        "rows_refolded": 0,
        "lock_timeouts": 0,
        "last_key": after_key,
    }
    if not apply:
        return report
    started = time.time()
    cursor: Optional[str] = after_key
    for _ in range(max_batches):
        report["batches_run"] += 1
        try:
            row = await _batch(db, REFOLD_BATCH_SQL, {"after_key": cursor, "batch_size": batch_size})
        except Exception as err:  # noqa: BLE001 - a lock timeout is an expected outcome, reported
            if not _is_lock_timeout(err):
                raise
            report["lock_timeouts"] += 1
            if sleep_ms > 0:
                await asyncio.sleep(sleep_ms / 1000.0)
            continue  # the same cursor again
        picked = int(row["picked"] or 0)
        if picked == 0:
            report["done"] = True
            break
        report["rows_refolded"] += int(row["refolded"] or 0)
        cursor = row["last_key"]
        report["last_key"] = cursor
        if sleep_ms > 0:
            await asyncio.sleep(sleep_ms / 1000.0)
    report["remaining_after_cursor"] = int(
        await db.fetch_val("SELECT count(*) FROM catalog_products WHERE product_key > :k", {"k": report["last_key"]})
    )
    report["elapsed_s"] = round(time.time() - started, 2)
    return report


async def _main(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        if args.refold_all:
            return await run_refold_all(
                apply=args.apply, batch_size=args.batch_size, max_batches=args.max_batches,
                sleep_ms=args.sleep_ms, after_key=args.after_key,
            )
        return await run_backfill(
            apply=args.apply, batch_size=args.batch_size, max_batches=args.max_batches, sleep_ms=args.sleep_ms
        )
    finally:
        await database.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="touch the rows (default: dry run, counts only)")
    parser.add_argument("--batch-size", type=int, default=1000, help="rows per batch (one transaction each)")
    parser.add_argument("--max-batches", type=int, default=10, help="stop after this many batches; re-run to continue")
    parser.add_argument("--sleep-ms", type=int, default=500, help="pause between batches")
    parser.add_argument("--refold-all", action="store_true", help="re-fold EVERY row (after a CREATE OR REPLACE of the fold)")
    parser.add_argument("--after-key", default="", help="--refold-all cursor: resume after this product_key")
    cli = parser.parse_args()
    result = asyncio.run(_main(cli))
    print(json.dumps(result, indent=2, default=str))
    if result.get("stalled"):
        print("backfill stalled: rows were picked and updated but not stamped; check the triggers", file=sys.stderr)
        sys.exit(2)
    if result.get("all_locked"):
        print("every candidate row was locked by a writer for the whole run; re-run later", file=sys.stderr)
        sys.exit(3)
