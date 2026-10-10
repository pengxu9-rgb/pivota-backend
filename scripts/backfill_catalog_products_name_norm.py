"""Backfill catalog_products.name_norm / own_name_norm (migration 260) in bounded, resumable batches.

The two columns are stamped by the trigger `trg_catalog_products_stamp_name_norm` on every write,
from ONE SQL function (`catalog_products_identity_fold`, the gateway's `identitySql` character for
character). This script never restates that rule: it TOUCHES each unstamped row (`SET title = title`)
and lets the trigger stamp it, so a row the script backfilled and a row a writer wrote are folded by
the same code. Which is also why it refuses to run when the trigger is absent.

Resumable by construction (every batch takes the next rows `WHERE name_norm IS NULL OR own_name_norm
IS NULL`, ordered by product_key), bounded (`--max-batches`), throttled (`--sleep-ms` between
batches), and a DRY RUN unless `--apply` is given (counts the unstamped rows and the batches it would
take, writes nothing). Each applied batch is its own transaction.

Run it on prod from a one-off job (the DB is VPC-only), with pivota-pg under 35% CPU and no index
build or other backfill running:

    bash scripts/ops/run_oneoff_job.sh scripts/backfill_catalog_products_name_norm.py            # dry run
    bash scripts/ops/run_oneoff_job.sh scripts/backfill_catalog_products_name_norm.py --apply --batch-size 1000 --max-batches 10 --sleep-ms 500

~28k rows (prod, 2026-10-10): 28 batches of 1,000; each batch is one UPDATE of 1,000 rows whose
trigger runs two regexp folds per row (~0.3 s). Re-run until `remaining` is 0; a re-run after that
touches nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

TRIGGER_NAME = "trg_catalog_products_stamp_name_norm"

COUNT_SQL = "SELECT count(*) FROM catalog_products WHERE name_norm IS NULL OR own_name_norm IS NULL"

TRIGGER_PRESENT_SQL = """
SELECT count(*)
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
WHERE t.tgname = :name AND c.relname = 'catalog_products' AND NOT t.tgisinternal
  AND c.oid = to_regclass('catalog_products')
"""

# The row is touched, not folded: the trigger stamps both columns.
BATCH_SQL = """
UPDATE catalog_products
SET title = title
WHERE product_key IN (
  SELECT product_key FROM catalog_products
  WHERE name_norm IS NULL OR own_name_norm IS NULL
  ORDER BY product_key
  LIMIT :batch_size
)
"""


class TriggerMissing(RuntimeError):
    """The stamping trigger is not on catalog_products: migration 260 has not been applied here."""


async def run_backfill(
    *, apply: bool, batch_size: int = 1000, max_batches: int = 10, sleep_ms: int = 500, db=database
) -> Dict[str, Any]:
    batch_size = max(1, int(batch_size))
    max_batches = max(1, int(max_batches))
    if int(await db.fetch_val(TRIGGER_PRESENT_SQL, {"name": TRIGGER_NAME})) != 1:
        raise TriggerMissing(f"{TRIGGER_NAME} is not on catalog_products; apply migration 260 first")
    before = int(await db.fetch_val(COUNT_SQL))
    report: Dict[str, Any] = {
        "apply": apply,
        "batch_size": batch_size,
        "max_batches": max_batches,
        "sleep_ms": sleep_ms,
        "unstamped_before": before,
        "batches_needed": (before + batch_size - 1) // batch_size,
        "batches_run": 0,
        "rows_touched": 0,
    }
    if not apply:
        report["remaining"] = before
        return report
    started = time.time()
    remaining = before
    for _ in range(max_batches):
        if remaining <= 0:
            break
        async with db.transaction():
            await db.execute(BATCH_SQL, {"batch_size": batch_size})
        after = int(await db.fetch_val(COUNT_SQL))
        touched = remaining - after
        report["batches_run"] += 1
        report["rows_touched"] += max(0, touched)
        if touched <= 0:
            # Rows were selected but none got stamped: the trigger is not doing its job here.
            report["stalled"] = True
            remaining = after
            break
        remaining = after
        if remaining > 0 and sleep_ms > 0:
            await asyncio.sleep(sleep_ms / 1000.0)
    report["remaining"] = remaining
    report["elapsed_s"] = round(time.time() - started, 2)
    return report


async def _main(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
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
    cli = parser.parse_args()
    result = asyncio.run(_main(cli))
    print(json.dumps(result, indent=2, default=str))
    if result.get("stalled"):
        print("backfill stalled: rows were selected but not stamped; check the trigger", file=sys.stderr)
        sys.exit(2)
