"""Run the next due (brand, retailer) ingest stage: one crawl per execution.

Runs as a Cloud Run Job on a Cloud Scheduler trigger (`infra/gcp/setup_scheduler.sh`), on the
crawl-egress subnet, NOT as an APScheduler entry in the `worker` service: that service leaves from
the default NAT, whose address is the payment IP, and a retailer crawl must never share it.

ONE JOB PER EXECUTION, on purpose. 2026-09-23: four retailer crawls in parallel from the crawl NAT
drew Shopify 429s on every store, and back-to-back crawls kept drawing them. The scheduler cadence
is the spacing; this entrypoint never loops.

It does nothing unless RETAILER_INGEST_DRAIN_ENABLED=1, so creating the job is not arming it.

Exit codes: 0 = a stage ran and was recorded (whatever its outcome) or nothing was due;
1 = an unexpected error (recorded on the job as `failed`), so the execution fails and alerts.

    python -m jobs.retailer_ingest_drain            # one stage
    python -m jobs.retailer_ingest_drain --lease 3600
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any, Dict

from db.database import database
from db import retailer_ingest as ledger
from services.retailer_ingest.pipeline import run_stage

logger = logging.getLogger(__name__)

#: Greppable, one line per execution: the log-based metrics and alerts key on it.
SUMMARY_MARKER = "retailer_ingest_drain: "


async def drain_once(*, lease_seconds: int, db: Any = None) -> Dict[str, Any]:
    db = db or database
    job = await ledger.claim_due_job(lease_seconds=lease_seconds, db=db)
    if not job:
        return {"outcome": "idle"}
    return await run_stage(job, db=db)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Longer than the slowest stage (a 20k-product crawl at 4s/page + apply): an expired lease makes
    # the job claimable again, so it must not expire under a stage that is still running.
    parser.add_argument("--lease", type=int, default=int(os.getenv("RETAILER_INGEST_LEASE_SECONDS", "4200")))
    args = parser.parse_args()

    if str(os.getenv("RETAILER_INGEST_DRAIN_ENABLED", "")).strip() != "1":
        print(SUMMARY_MARKER + json.dumps({"outcome": "disabled"}))
        return 0

    async def _run() -> Dict[str, Any]:
        await database.connect()
        try:
            return await drain_once(lease_seconds=args.lease)
        finally:
            await database.disconnect()

    try:
        summary = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 -- run_stage already recorded it on the job
        print(SUMMARY_MARKER + json.dumps({"outcome": "error", "error": f"{type(exc).__name__}: {exc}"[:1000]}))
        logger.exception("retailer ingest drain failed")
        return 1
    print(SUMMARY_MARKER + json.dumps(summary, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
