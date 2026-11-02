#!/usr/bin/env python3
"""Run the isolated Store Audit UCP re-probe selector once.

This is a Cloud Run Job entrypoint. It deliberately does not start the
in-process APScheduler: selection is domain/TTL-based and runs in its own
scheduled lane, separate from merchant Agent Presence Monitoring.
"""

from __future__ import annotations

import asyncio
import json

import sys
from pathlib import Path

# Run as `python scripts/<this>.py` (the Cloud Run Job's command), Python puts scripts/ on
# sys.path, not the repo root, so `jobs` was not importable: the job failed with
# ModuleNotFoundError on every run from its creation (2026-08) to 2026-11-02.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.scheduled_ucp_reprobe_job import run_scheduled_ucp_reprobes  # noqa: E402


async def _main() -> None:
    summary = await run_scheduled_ucp_reprobes()
    # Aggregate-only output: route/domain identifiers and probe bodies must
    # never become Cloud Run Job logs.
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
