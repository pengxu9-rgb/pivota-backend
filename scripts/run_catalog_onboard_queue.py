"""Manual entry for the catalog-onboard queue — enqueue from a feed and/or drain.

The scheduler (jobs.catalog_onboard_job, OFF by default) drains automatically once
CATALOG_ONBOARD_ENABLED is set; this script is for manual/cron operation + seeding.

Usage:
  # enqueue a curated brand list (recurrence-prioritized), then drain + ingest:
  python -m scripts.run_catalog_onboard_queue --enqueue-curated brands.jsonl --drain --apply

  # just drain (dry-run — enumerate/validate, no catalog writes):
  python -m scripts.run_catalog_onboard_queue --drain --limit 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.catalog_onboard_worker import enqueue_curated_brands, process_queue  # noqa: E402
from services.competitor_recurrence import recurrence_rank  # noqa: E402


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        if args.enqueue_curated:
            brands = _read_jsonl(args.enqueue_curated)
            rank = await recurrence_rank()  # prioritize by cross-audit demand
            n = await enqueue_curated_brands(brands, priority_rank=rank, source="curated_list")
            print(f"enqueued {n} curated brand(s) (of {len(brands)}; rest already queued)")

        if args.drain:
            summary = await process_queue(limit=args.limit, apply=args.apply)
            print(f"drain: {summary}{'' if args.apply else '  (DRY-RUN — no catalog writes)'}")
        elif not args.enqueue_curated:
            print("nothing to do — pass --enqueue-curated and/or --drain", file=sys.stderr)
            return 2
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--enqueue-curated", metavar="JSONL", help="enqueue a curated brand list ({domain,category_path,brand?})")
    p.add_argument("--drain", action="store_true", help="claim + process queued items")
    p.add_argument("--limit", type=int, default=20, help="max items to drain")
    p.add_argument("--apply", action="store_true", help="ingest (else dry-run enumerate/validate)")
    args = p.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
