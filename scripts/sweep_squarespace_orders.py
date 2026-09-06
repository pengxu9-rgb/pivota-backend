#!/usr/bin/env python3
"""Reconcile Squarespace orders into the canonical commerce ledger.

For a Squarespace site connected with a per-site Developer API key this is the
ONLY telemetry path: webhook subscriptions are an OAuth (Developer Platform)
surface and an API key cannot create one. For an OAuth-connected site it is the
recovery path for a delivery Squarespace dropped or Pivota answered 5xx to.

DRY RUN BY DEFAULT, like every other job script in this repo
(scripts/sweep_commerce_ledger_synthetic.py is the pattern): the run lists and
classifies orders, writes nothing, and leaves the cursor where it was until
`--apply`.

    python -m scripts.sweep_squarespace_orders                      # every active store, dry run
    python -m scripts.sweep_squarespace_orders --apply
    python -m scripts.sweep_squarespace_orders --store-id store_x --apply
    python -m scripts.sweep_squarespace_orders --store-id store_x \
        --modified-before 2026-02-01T00:00:00Z --apply   # pin the window's end

WHY A SCRIPT *AND* A ROUTE. `POST /integrations/squarespace/{store_id}/reconcile`
is the same sweep behind merchant-or-admin auth. Both exist because neither
scheduling surface in this repo actually ships this lane on merge: CI deploys no
Cloud Run job for it, and the APScheduler lane runs inside a service that is not
auto-deployed. See the scheduling section of docs/SQUARESPACE_TELEMETRY.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from db.database import database
from services.squarespace_order_sweep import (
    DEFAULT_INITIAL_LOOKBACK_DAYS,
    DEFAULT_MAX_PAGES,
    DEFAULT_OVERLAP_MINUTES,
    sweep_all_squarespace_stores,
)


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        result = await sweep_all_squarespace_stores(
            apply=args.apply,
            overlap_minutes=args.overlap_minutes,
            initial_lookback_days=args.initial_lookback_days,
            max_pages=args.max_pages,
            modified_before=args.modified_before,
            store_ids=args.store_id or None,
        )
    finally:
        if own:
            await database.disconnect()

    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    if result.get("dry_run"):
        print(
            f"\nDRY RUN — nothing was written and no cursor moved. "
            f"{result.get('ignored', 0)} order(s) would have been mapped across "
            f"{result.get('processed', 0)} store(s). Re-run with --apply.",
            flush=True,
        )
    truncated = [
        store["store_id"] for store in result.get("stores", []) if store.get("truncated")
    ]
    if truncated:
        # NOT "re-run and it will sort itself out with a wider window" — that
        # was the frozen-cursor bug. A truncated run records the end it could
        # not reach and the NEXT run halves towards it, so repeated runs
        # converge instead of re-reading the same page-cap prefix forever.
        print(
            "\nNOTE: the page cap stopped these stores before their window was "
            f"fully read: {', '.join(truncated)}. The next run BISECTS towards "
            "the end it could not reach, so re-running makes progress; raise "
            "--max-pages, or pin --modified-before, to converge faster.",
            flush=True,
        )
    # A partial failure is a non-zero exit so a scheduled run is visibly red.
    return 0 if result.get("status") == "success" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write to the ledger and advance the cursor (default: dry run)",
    )
    parser.add_argument(
        "--store-id",
        action="append",
        default=[],
        help="a specific store to sweep; repeatable. Default: every active "
        "Squarespace store.",
    )
    parser.add_argument(
        "--overlap-minutes",
        type=int,
        default=DEFAULT_OVERLAP_MINUTES,
        help="how far before the stored cursor the window starts (default: %(default)s)",
    )
    parser.add_argument(
        "--initial-lookback-days",
        type=int,
        default=DEFAULT_INITIAL_LOOKBACK_DAYS,
        help="window for a store with no cursor yet (default: %(default)s)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help="page cap per store per run (default: %(default)s)",
    )
    parser.add_argument(
        "--modified-before",
        default=None,
        help="ISO-8601 upper bound for this run's window (the operator escape "
        "hatch over the automatic bisect). Default: now, or the bisected "
        "midpoint when the previous run truncated.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
