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
import os
from typing import Optional

from db.database import database
from db.product_quality_backfill_jobs import requeue_stale_quality_backfill_jobs
from services.product_quality_backfill_service import process_next_quality_backfill_job


logger = logging.getLogger(__name__)
_loop_task: Optional[asyncio.Task] = None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


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
            requeued = await requeue_stale_quality_backfill_jobs(
                stale_after_seconds=args.stale_after_seconds,
            )
            if requeued:
                logger.warning("Requeued %s stale quality backfill job(s)", requeued)
            processed = await _run_once()
            await asyncio.sleep(args.sleep_seconds if not processed else 1)
    finally:
        await database.disconnect()


async def _loop_forever(*, sleep_seconds: int, stale_after_seconds: int) -> None:
    logger.info(
        "Starting product quality backfill loop sleep_seconds=%s stale_after_seconds=%s",
        sleep_seconds,
        stale_after_seconds,
    )
    try:
        while True:
            requeued = await requeue_stale_quality_backfill_jobs(
                stale_after_seconds=stale_after_seconds,
            )
            if requeued:
                logger.warning("Requeued %s stale quality backfill job(s)", requeued)
            processed = await _run_once()
            await asyncio.sleep(sleep_seconds if not processed else 1)
    except asyncio.CancelledError:
        logger.info("Stopped product quality backfill loop")
        raise


def start_product_quality_backfill_loop() -> None:
    global _loop_task
    enabled = os.getenv("QUALITY_BACKFILL_WORKER_ENABLED", "true").strip().lower()
    if enabled in {"0", "false", "no"}:
        logger.info("Product quality backfill loop disabled by env")
        return
    if _loop_task is not None and not _loop_task.done():
        return
    _loop_task = asyncio.create_task(
        _loop_forever(
            sleep_seconds=max(1, _env_int("QUALITY_BACKFILL_WORKER_SLEEP_SECONDS", 5)),
            stale_after_seconds=max(60, _env_int("QUALITY_BACKFILL_STALE_AFTER_SECONDS", 180)),
        ),
        name="product_quality_backfill_loop",
    )


async def stop_product_quality_backfill_loop() -> None:
    global _loop_task
    if _loop_task is None:
        return
    if not _loop_task.done():
        _loop_task.cancel()
        try:
            await _loop_task
        except asyncio.CancelledError:
            pass
    _loop_task = None


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
    parser.add_argument(
        "--stale-after-seconds",
        type=int,
        default=180,
        help="Requeue running jobs older than this threshold",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
