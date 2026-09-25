#!/usr/bin/env python3
"""Delete aged synthetic (ops-canary) rows from the canonical commerce ledger.

The ops telemetry canary writes an eight-event chain per run and nothing has
ever deleted one. This is the sweep migration 214's partial index was built
for. It touches ONLY probe rows — `synthetic IS TRUE`, plus the pre-213 shape
`surface = 'ops_canary'` — and only their interactions once no event of any
kind is left pointing at them.

DRY RUN BY DEFAULT, like every other Cloud Run job in this repo
(scripts/backfill_offer_market_currency.py is the pattern): the run reports
what it would delete and writes nothing until `--apply`.

    python -m scripts.sweep_commerce_ledger_synthetic --older-than-days 7
    python -m scripts.sweep_commerce_ledger_synthetic --older-than-days 7 --apply

`--report-horizon-days N` answers a different question and never deletes: how
much REAL commerce history sits behind an N-day horizon, per merchant. PR-0.9
deliberately ships no retention policy for real rows; this is the measurement
the decision needs.

Scheduling is ops config, not code. See the Retention section of
docs/UNIVERSAL_COMMERCE_EVENTS.md for the job command and cadence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import List, Optional

from db.database import database
from services.commerce_ledger_retention import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_BATCHES,
    DEFAULT_OLDER_THAN_DAYS,
    report_ledger_retention,
    sweep_synthetic_events,
)


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        if args.report_horizon_days is not None:
            result = await report_ledger_retention(horizon_days=args.report_horizon_days)
            result["mode"] = "report"
        else:
            result = await sweep_synthetic_events(
                older_than_days=args.older_than_days,
                batch_size=args.batch_size,
                apply=args.apply,
                max_batches=args.max_batches,
            )
            result["mode"] = "sweep"
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
        if result.get("mode") == "sweep" and result.get("dry_run"):
            print(
                "\nDRY RUN — nothing was deleted. Re-run with --apply to delete "
                f"{result.get('events_deleted', 0)} events and "
                f"{result.get('interactions_deleted', 0)} interactions.",
                flush=True,
            )
        if result.get("truncated"):
            print(
                f"\nNOTE: stopped at --max-batches {args.max_batches}; more rows remain. "
                "Re-run to continue.",
                flush=True,
            )
        return 0
    finally:
        if own:
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sweep aged synthetic/ops-canary rows from the commerce ledger."
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=DEFAULT_OLDER_THAN_DAYS,
        help=f"delete probe rows whose occurred_at is older than this (default {DEFAULT_OLDER_THAN_DAYS})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"rows per batch; one transaction per batch (default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=DEFAULT_MAX_BATCHES,
        help=f"stop after this many batches (default {DEFAULT_MAX_BATCHES})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete the rows (else dry-run, which is the default)",
    )
    parser.add_argument(
        "--report-horizon-days",
        type=int,
        default=None,
        metavar="N",
        help=(
            "report REAL ledger volume older than N days, per merchant, and exit. "
            "Deletes nothing and ignores the sweep flags."
        ),
    )
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
