"""Entrypoint for the default-off Store Audit commerce re-probe job."""

from __future__ import annotations

import asyncio
import json
import logging

import sys
from pathlib import Path

# Run as `python scripts/<this>.py` (the Cloud Run Job's command), Python puts scripts/ on
# sys.path, not the repo root, so `jobs` was not importable: the job failed with
# ModuleNotFoundError on every run from its creation (2026-08) to 2026-11-02.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from jobs.scheduled_commerce_checkout_reprobe_job import run_scheduled_commerce_checkout_reprobes  # noqa: E402


async def main() -> None:
    # Like every other job entrypoint: the selector queries through the shared `database`, which
    # nothing else connects in a job process (an unconnected backend asserts "not running").
    await database.connect()
    try:
        result = await run_scheduled_commerce_checkout_reprobes()
    finally:
        await database.disconnect()
    logging.getLogger(__name__).info("scheduled_commerce_checkout_reprobes %s", json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
