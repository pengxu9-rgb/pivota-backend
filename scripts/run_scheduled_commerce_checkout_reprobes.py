"""Entrypoint for the default-off Store Audit commerce re-probe job."""

from __future__ import annotations

import asyncio
import json
import logging

from jobs.scheduled_commerce_checkout_reprobe_job import run_scheduled_commerce_checkout_reprobes


async def main() -> None:
    result = await run_scheduled_commerce_checkout_reprobes()
    logging.getLogger(__name__).info("scheduled_commerce_checkout_reprobes %s", json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
