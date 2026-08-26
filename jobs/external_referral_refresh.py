"""Re-read the price and availability of the external seeds we serve.

CLI-ONLY TODAY, AND THAT IS NOT A DESIGN CHOICE. Nothing schedules this: `services/audit_scheduler`
does not register it and `infra/gcp/setup_scheduler.sh` has no entry for it
(`docs/card-rail-readiness-audit.md` row A3, `docs/external-seed-dead-pdp-link-audit.md` §4.3).
When it is armed it must be a Cloud Run Job on a Cloud Scheduler trigger, NOT a 33rd APScheduler
entry, and it must run on the `pivota-crawl` subnet like its sibling
`jobs/external_seed_destination_sweep.py`: it fetches third-party storefronts, so it has to leave
from the reserved crawl-egress NAT (`infra/gcp/setup_crawl_egress.sh`) or most brand hosts answer
it with a bot challenge. A GitHub Actions cron is not an option at all — Cloud SQL `pivota-pg` is
private-IP only, so a runner has no route to the database (`tests/test_run_oneoff_job.py`).

Usage:

    python3 -m jobs.external_referral_refresh --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict

from db.database import database
from routes.employee_products import _refresh_external_seed_by_id
from services.external_referral_readiness import run_external_referral_refresh_batch


logger = logging.getLogger(__name__)


async def _refresh_unbounded(seed_id: str) -> Dict[str, Any]:
    """The batch's patience is UNBOUNDED, and it has to say so.

    `crawl_politeness` refuses a slot further out than the caller allows, and the default
    ceiling (CRAWL_MAX_WAIT_SECONDS, 10s) exists for the interactive route where a human is
    waiting. In a batch that ceiling is actively harmful: most of the backoff curve sits
    beyond it, so a host that has 429'd a couple of times can never be waited for. The
    refusal surfaces as a generic failure, every remaining row on that host resolves in
    milliseconds, and the run reports them as unreadable when in fact we declined to wait.

    `max_wait=0` disables the ceiling (`crawl_politeness.before_request`: the refusal is
    guarded on `ceiling > 0`). The pacing itself is unchanged — we still wait our turn; we
    simply stop giving up on the turn. The sibling destination sweep already does this.
    """
    return await _refresh_external_seed_by_id(seed_id, max_wait=0)


async def run_daily_external_referral_refresh(*, limit: int = 500) -> Dict[str, Any]:
    return await run_external_referral_refresh_batch(
        refresh_seed_by_id=_refresh_unbounded,
        limit=limit,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh active external referral seeds for runtime gating.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum number of referral seeds to refresh")
    args = parser.parse_args()

    # THE POOL DOES NOT EXIST UNTIL SOMEONE OPENS IT. Inside the API process the lifespan hook
    # connects `database` before any route runs; a CLI entrypoint has no lifespan, so it has to
    # do it itself. Without this the first query raises AssertionError("DatabaseBackend is not
    # running") out of `db.database`'s patched `PostgresConnection.acquire` — i.e. this job could
    # never have completed a single run against Postgres. It looked fine locally only because the
    # sqlite backend has no such guard, which is why the regression test around this asserts on
    # the CONNECT CALL rather than on a run succeeding.
    async def _run() -> Dict[str, Any]:
        await database.connect()
        try:
            return await run_daily_external_referral_refresh(limit=args.limit)
        finally:
            await database.disconnect()

    summary = asyncio.run(_run())
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    logger.info("external referral refresh completed", extra={"summary": summary})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
