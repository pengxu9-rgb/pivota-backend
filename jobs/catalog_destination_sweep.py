"""Re-read the storefront pages of catalog rows that have no seed, and (when armed) withdraw dead ones.

The catalog-enrichment twin of jobs.external_seed_destination_sweep; see
services/catalog_destination_liveness for what it reads, what it decides and why. Runs as a Cloud Run
Job on the crawl subnet (`infra/gcp/setup_scheduler.sh`, CATALOG_DESTINATION_SWEEP) — it fetches
from third-party storefronts and must leave from the crawl-egress NAT.

OBSERVE BY DEFAULT. Without `--suppress` it records verdicts in catalog_destination_liveness and
withdraws nothing (a live answer still lifts a row this lane hid earlier). With `--suppress`, a first
corroborated dead observation hides the row under `catalog_destination_dead_pending` and a second,
24h+ later, withdraws it under `catalog_destination_dead`.

SIZING. ~13k served rows; the default limit covers a full pass in about four nights, and stage 1
means it costs one `/products.json` read per host plus a probe per handle the store no longer lists.

THE NUMBER TO WATCH is `hosts_unverifiable`, as for the seed sweep: `coverage_alarm` logs it.

Usage:

    python3 -m jobs.catalog_destination_sweep --limit 4000              # observe
    python3 -m jobs.catalog_destination_sweep --limit 4000 --suppress   # observe + act
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict

from db.database import database
from services import shopify_edge_pacer
from services.catalog_destination_liveness import ensure_table, run_catalog_destination_sweep
from services.external_seed_destination_liveness import coverage_alarm

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 4000


async def run_nightly_catalog_destination_sweep(*, limit: int = DEFAULT_LIMIT, suppress: bool = False) -> Dict[str, Any]:
    await ensure_table()
    summary = await run_catalog_destination_sweep(limit=limit, suppress=suppress)
    if shopify_edge_pacer.enabled():
        summary["shopify_edge_pacer"] = shopify_edge_pacer.stats()
    alarm = coverage_alarm(summary)
    if alarm:
        summary["coverage_alarm"] = alarm
        logger.warning(alarm, extra={"summary": summary})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--suppress",
        action="store_true",
        help="Hide a row on its first corroborated dead observation and withdraw it on the second.",
    )
    args = parser.parse_args()

    async def _run() -> Dict[str, Any]:
        await database.connect()
        try:
            return await run_nightly_catalog_destination_sweep(limit=args.limit, suppress=args.suppress)
        finally:
            await database.disconnect()

    summary = asyncio.run(_run())
    # A text prefix keeps the line in textPayload; a bare JSON object would land in jsonPayload.
    print("CATALOG_DESTINATION_SWEEP " + json.dumps(summary, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
