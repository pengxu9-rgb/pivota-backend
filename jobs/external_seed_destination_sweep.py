"""Re-read the destinations we publish, and withdraw the ones that are gone.

Runs as a Cloud Run Job on a Cloud Scheduler trigger (`infra/gcp/setup_scheduler.sh`), NOT as
a 33rd APScheduler entry: it crawls third-party storefronts and must leave from the reserved
crawl-egress NAT (`infra/gcp/setup_crawl_egress.sh`), not from a web dyno whose IP is shared
with everything else we do.

SIZING. A full pass has to fit inside the readiness gate's staleness window
(`EXTERNAL_REFERRAL_STALE_DAYS = 7`), or every seed spends part of its life blocked no matter
how well the sweep works. ~11.4k active seeds / 7 days ≈ 1,700 a day, which the default limit
covers with room. Stage 1 means that costs a few hundred requests, not 1,700: one
`/products.json` read per host answers every seed on it.

THE NUMBER TO WATCH IS NOT `dead_links_found`. It is `hosts_unverifiable`. A run that cannot
read its hosts reports zero dead links and looks identical to a healthy one — which is exactly
what a client outside the crawl egress sees today (213 of 286 hosts refuse it). `coverage_alarm`
turns that into a log line at WARNING so it can be alerted on.

Usage:

    python3 -m jobs.external_seed_destination_sweep --limit 1700
    python3 -m jobs.external_seed_destination_sweep --limit 200 --no-retire   # observe only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict

from db.database import database
from services.external_seed_destination_liveness import coverage_alarm, run_destination_sweep

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 1700


async def run_daily_destination_sweep(*, limit: int = DEFAULT_LIMIT, retire: bool = True) -> Dict[str, Any]:
    summary = await run_destination_sweep(limit=limit, retire=retire)
    alarm = coverage_alarm(summary)
    if alarm:
        summary["coverage_alarm"] = alarm
        logger.warning(alarm, extra={"summary": summary})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--no-retire",
        dest="retire",
        action="store_false",
        help="Record observations but never withdraw a seed. Use for the first production run.",
    )
    parser.set_defaults(retire=True)
    args = parser.parse_args()

    async def _run() -> Dict[str, Any]:
        await database.connect()
        try:
            return await run_daily_destination_sweep(limit=args.limit, retire=args.retire)
        finally:
            await database.disconnect()

    summary = asyncio.run(_run())
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
