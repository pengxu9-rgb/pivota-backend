#!/usr/bin/env python3
"""Run the isolated Store Audit UCP re-probe selector once.

This is a Cloud Run Job entrypoint. It deliberately does not start the
in-process APScheduler: selection is domain/TTL-based and runs in its own
scheduled lane, separate from merchant Agent Presence Monitoring.
"""

from __future__ import annotations

import asyncio
import json

from jobs.scheduled_ucp_reprobe_job import run_scheduled_ucp_reprobes


async def _main() -> None:
    summary = await run_scheduled_ucp_reprobes()
    # Aggregate-only output: route/domain identifiers and probe bodies must
    # never become Cloud Run Job logs.
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
