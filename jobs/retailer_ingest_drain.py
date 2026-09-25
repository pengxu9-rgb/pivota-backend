"""Run the due (brand, retailer) ingest stages: one crawl per host, back to back within a budget.

Runs as a Cloud Run Job on a Cloud Scheduler trigger (`infra/gcp/setup_scheduler.sh`), on the
crawl-egress subnet, NOT as an APScheduler entry in the `worker` service: that service leaves from
the default NAT, whose address is the payment IP, and a retailer crawl must never share it.

ONE CRAWL PER HOST, always; ONE STAGE AT A TIME by default. 2026-09-23: four retailer crawls in
parallel from the crawl NAT drew Shopify 429s on every store. (Back-to-back crawls drew them too, but
at the old pacing; since then every crawl is paced by CRAWL_MIN_INTERVAL_SECONDS=4, and each stage
makes the same requests whether it follows a sleep or another stage.)

LANES, when RETAILER_INGEST_MAX_LEASES > 1 (default 1; ceiling db.retailer_ingest.MAX_LEASES_CEILING).
2026-09-24: a K-beauty dry run spends 35 minutes on 200 paced PDP fetches at ONE host, and with a
single lane every other store waited behind it (~2 stages/hour, 26 queued). The */10 executions
already overlap (task timeout 3600s); a claim now lets up to N of them hold a lease at once, but
never two at the same host (lowercased, "www." dropped, whatever the cohort shape). Two applies at
different hosts may run at once (2026-09-25: an apply's re-crawl + checks took 244-2310 s, and one
apply at a time left 20 jobs waiting in apply_due); only their catalog WRITES are serial, under
db.retailer_ingest.catalog_write_lock (an apply that cannot get it within
pipeline.WRITE_LOCK_WAIT_S, or before pipeline.WRITE_MARGIN_S of this task is left, writes nothing and
returns to apply_due, outcome write_lock_busy). Each lane is its own execution with its own DB pool
(DB_POOL_MAX_SIZE, 3 in prod), so N lanes hold up to 3N connections. Host exclusivity does NOT bound
the total request rate from the one crawl NAT (pacing is per host, per process): N lanes are N times
the traffic, so arm at 2 and watch crawl_throttled before going higher. A throttled crawl ends only
its own lane's loop. "idle" covers nothing due, every lane busy, and a lost lane lock alike.

BACK TO BACK WITHIN A BUDGET, when RETAILER_INGEST_DRAIN_BUDGET_SECONDS > 0 (default 0 = exactly one
stage, the original behaviour). 2026-09-24: stages took 4-12 minutes, so one stage per 30-minute tick
left the drain idle ~70% of the time while three coverage waves queued behind it. After each stage
the execution claims the next due job if the budget has not run out; it never starts a stage past the
budget, and the budget sits well inside the task timeout so the last stage can finish. An overlapping
tick whose claim finds every lane busy (or only hosts already in flight) exits idle.

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
import time
from typing import Any, Callable, Dict, List, Optional

from db.database import database
from db import retailer_ingest as ledger
from services.retailer_ingest.pipeline import run_stage

logger = logging.getLogger(__name__)

#: Greppable, one line per execution: the log-based metrics and alerts key on it.
SUMMARY_MARKER = "retailer_ingest_drain: "


async def drain_once(*, lease_seconds: int, max_leases: int = 1, db: Any = None,
                     time_left_s: Optional[float] = None) -> Dict[str, Any]:
    """One stage, plus the lane's job counts by status on every execution -- including idle ones --
    so a store held for review raises a signal even while nothing is being claimed. `time_left_s`
    (seconds left in this task) reaches the stage, so an apply never starts a catalog write the task
    timeout would cut short."""
    db = db or database
    job = await ledger.claim_due_job(lease_seconds=lease_seconds, max_leases=max_leases, db=db)
    summary = {"outcome": "idle"} if not job else await run_stage(job, db=db, time_left_s=time_left_s)
    summary["jobs"] = await ledger.status_counts(db=db)
    return summary


#: A stage's lease outlives the TASK by this much, never more: a lease that outlives a killed task is
#: the time the lane stays blocked before the next claim may mark the stage interrupted.
LEASE_SLACK_SECONDS = 600


async def drain_loop(*, lease_seconds: int, budget_seconds: int, db: Any = None,
                     clock: Callable[[], float] = time.monotonic, report: Callable[[Dict[str, Any]], None] = None,
                     task_timeout_seconds: int = 3600, max_leases: int = 1) -> List[Dict[str, Any]]:
    """Stages back to back until nothing is due or the budget is spent. A budget of 0 is one stage.
    A throttled crawl also ends the loop: the crawl NAT is being rate-limited, and the next store would
    draw the same 429s and spend its retry budget -- the next tick is the cool-down. An unexpected
    error propagates (run_stage has recorded it on the job) and ends the loop."""
    start = clock()
    longest = 0.0
    summaries: List[Dict[str, Any]] = []
    while True:
        began = clock()
        # The lease covers what is left of THIS task (+ slack), not a fixed 70 minutes from each claim:
        # a stage killed at the task timeout 50 minutes in must not block the lane for another hour.
        # The time left in THIS task: the lease and the stage's catalog-write deadline both come from it.
        time_left = max(0.0, task_timeout_seconds - (began - start))
        lease = min(lease_seconds, int(time_left) + LEASE_SLACK_SECONDS)
        summary = await drain_once(lease_seconds=lease, max_leases=max_leases, db=db, time_left_s=time_left)
        ended = clock()
        # Logged per stage so the budget/timeout margin can be tuned from data, not guessed.
        summary["duration_s"] = round(ended - began, 1)
        longest = max(longest, ended - began)
        summaries.append(summary)
        if report:
            report(summary)
        # A stage starts only if one as long as the longest seen so far would still end inside the
        # budget: full-catalog crawls (#2296) and 30k-product scans make stages longer than 12 min,
        # and a stage the task timeout kills is failed as "may be partial".
        if (summary.get("outcome") in ("idle", "crawl_throttled") or budget_seconds <= 0
                or (ended - start) + longest >= budget_seconds):
            return summaries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Longer than the slowest stage (a 20k-product crawl at 4s/page + apply): an expired lease makes
    # the job claimable again, so it must not expire under a stage that is still running.
    parser.add_argument("--lease", type=int, default=int(os.getenv("RETAILER_INGEST_LEASE_SECONDS", "4200")))
    # No new stage starts after this many seconds; keep it well inside the task timeout (3600s) so the
    # last stage finishes. 0 = one stage per execution.
    parser.add_argument("--budget", type=int, default=int(os.getenv("RETAILER_INGEST_DRAIN_BUDGET_SECONDS", "0")))
    # The Cloud Run task timeout (setup_scheduler.sh: 3600s); Cloud Run does not expose it to the task.
    parser.add_argument("--task-timeout", type=int,
                        default=int(os.getenv("RETAILER_INGEST_TASK_TIMEOUT_SECONDS", "3600")))
    # Stages in flight across overlapping executions; never two at one host. 1 = one at a time.
    parser.add_argument("--max-leases", type=int, default=ledger.max_leases_from_env())
    args = parser.parse_args()

    if str(os.getenv("RETAILER_INGEST_DRAIN_ENABLED", "")).strip() != "1":
        print(SUMMARY_MARKER + json.dumps({"outcome": "disabled"}))
        return 0

    def report(summary: Dict[str, Any]) -> None:
        # One greppable line per STAGE (the held/failed log metrics count lines), printed as it ends.
        print(SUMMARY_MARKER + json.dumps(summary, default=str, sort_keys=True), flush=True)

    async def _run() -> None:
        await database.connect()
        try:
            await drain_loop(lease_seconds=args.lease, budget_seconds=args.budget, report=report,
                             task_timeout_seconds=args.task_timeout, max_leases=args.max_leases)
        finally:
            await database.disconnect()

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 -- run_stage already recorded it on the job
        print(SUMMARY_MARKER + json.dumps({"outcome": "error", "error": f"{type(exc).__name__}: {exc}"[:1000]}))
        logger.exception("retailer ingest drain failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
