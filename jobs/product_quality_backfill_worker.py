"""
Product Quality Backfill Worker

Durable score-only worker for merchant catalog quality coverage.

Typical usage:

    cd pivota-backend
    python -m jobs.product_quality_backfill_worker --once
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from db.database import database
from services.product_quality_backfill_service import process_next_quality_backfill_job


logger = logging.getLogger(__name__)


async def _run_once() -> bool:
    job = await process_next_quality_backfill_job()
    if job is None:
        logger.info("No queued quality backfill jobs found")
        return False
    logger.info(
        "Processed quality backfill job %s for merchant=%s platform=%s status=%s",
        job.get("job_id"),
        job.get("merchant_id"),
        job.get("platform"),
        job.get("status"),
    )
    return True


async def _main_async(args: argparse.Namespace) -> None:
    await database.connect()
    try:
        if args.once:
            await _run_once()
            return

        while True:
            processed = await _run_once()
            await asyncio.sleep(args.sleep_seconds if not processed else 1)
    finally:
        await database.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Product Quality Backfill Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one queued job and exit",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=int,
        default=10,
        help="Polling interval when running continuously",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
