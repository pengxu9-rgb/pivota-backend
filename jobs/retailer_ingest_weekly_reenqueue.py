"""Hourly tick (worker audit_scheduler) and CLI for the retailer-ingest freshness re-enqueue.

The logic and its rules live in services/retailer_ingest/weekly_reenqueue.py. This file is the two ways
to run it:

  * the scheduler tick, `run_retailer_ingest_weekly_reenqueue_tick`: does NOTHING (no DB access) unless
    RETAILER_INGEST_WEEKLY_REENQUEUE_ENABLED=1; with RETAILER_INGEST_WEEKLY_REENQUEUE_DRY_RUN=1 it plans
    and logs what it would queue and writes nothing. One WARNING summary line per run.
  * the CLI, for an operator: `--dry-run` works with the flag off (it reads, never writes) and prints one
    JSON line per cohort ({"kind": "decision", ...}) and then the summary ({"kind": "summary", ...});
    without `--dry-run` it refuses unless the flag is on. `--now` without an offset is UTC.

    python -m jobs.retailer_ingest_weekly_reenqueue --dry-run
    python -m jobs.retailer_ingest_weekly_reenqueue --dry-run --host shop.example.com --now 2026-10-21T10:00

WHY THE WORKER, NOT THE DRAIN'S TICK. The enqueue is database-only (it crawls nothing, so the crawl egress
does not matter), and the worker deploys on merge, so the enqueuer and its flag move with the backend
instead of with the drain's pinned, hand-rolled image. The drain stays a consumer: pausing its trigger
pauses crawling without touching the schedule, and the open-job unique index keeps a paused drain from
piling up more than one queued job per cohort.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional

from services.retailer_ingest import weekly_reenqueue as reenqueue

logger = logging.getLogger(__name__)


async def run_retailer_ingest_weekly_reenqueue_tick(db: Any = None) -> Optional[dict]:
    """APScheduler entry point (hourly). Flag off -> returns None having touched nothing."""
    if not reenqueue.enabled():
        logger.debug("retailer_ingest_weekly_reenqueue: disabled via %s", reenqueue.ENABLED_ENV)
        return None
    if db is None:
        from db.database import database as db
    summary = await reenqueue.run_reenqueue(db=db, dry_run=reenqueue.dry_run_from_env(),
                                            excluded=reenqueue.excluded_hosts_from_env(),
                                            max_per_run=reenqueue.max_per_run_from_env(),
                                            interval_hours=reenqueue.interval_hours_from_env())
    reenqueue.log_summary(summary)
    return summary


async def _main(args: argparse.Namespace) -> int:
    from db.database import database

    if not args.dry_run and not reenqueue.enabled():
        print(json.dumps({"outcome": "disabled", "flag": reenqueue.ENABLED_ENV,
                          "hint": "pass --dry-run to see what it would queue"}))
        return 2
    now = parse_now(args.now) if args.now else None
    await database.connect()
    try:
        summary = await reenqueue.run_reenqueue(db=database, now=now, dry_run=args.dry_run,
                                                excluded=reenqueue.excluded_hosts_from_env(),
                                                max_per_run=reenqueue.max_per_run_from_env(),
                                                interval_hours=reenqueue.interval_hours_from_env(),
                                                hosts=args.host)
    finally:
        await database.disconnect()
    for decision in summary["decisions"]:
        line = {"kind": "decision", **{k: v for k, v in decision.items() if k != "options"}}
        print(json.dumps(line, default=str, sort_keys=True))
    line = {"kind": "summary", **{k: v for k, v in summary.items() if k != "decisions"}}
    print(json.dumps(line, default=str, sort_keys=True))
    return 0


def parse_now(raw: str) -> datetime:
    """An ISO instant; one without an offset is UTC (the schedule compares aware times)."""
    at = datetime.fromisoformat(raw)
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="plan and print; write nothing (flag not needed)")
    parser.add_argument("--host", action="append", help="only this host (repeatable)")
    parser.add_argument("--now", help="evaluate the schedule at this ISO instant, UTC unless it says (dry runs)")
    args = parser.parse_args(argv)
    if args.now and not args.dry_run:
        parser.error("--now is for dry runs only")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
