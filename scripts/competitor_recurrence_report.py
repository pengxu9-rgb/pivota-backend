"""Catalog-coverage priority queue: rank competitor brands by cross-audit
recurrence (the demand proxy for what to onboard into the commerce index FIRST).

Read-only. Mirrors the niche_recurrence demand-proxy idea over the competitor
landscape the audits surface. Feed the top of this list to the curated-brand
onboarder (scripts/onboard_curated_brands.py) or use it to order the audit
candidate feed.

Usage:
  python -m scripts.competitor_recurrence_report [--limit 50] [--min-merchants 1] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.competitor_recurrence import top_recurring_competitors  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        top = await top_recurring_competitors(
            limit=args.limit,
            min_merchants=args.min_merchants,
            exclude_non_brands=not args.include_non_brands,
        )
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()

    if args.json:
        print(json.dumps(top, ensure_ascii=False, indent=2))
        return 0
    print(f"top {len(top)} competitor brands by cross-audit recurrence "
          f"(min_merchants={args.min_merchants}):")
    print("  merch  audits  mentions  brand")
    for d in top:
        print("  %5d  %6d  %8d  %s" % (
            d["distinct_merchants"], d["distinct_audits"], d["total_mentions"], d["brand"]
        ))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--min-merchants", type=int, default=1)
    p.add_argument("--include-non-brands", action="store_true",
                   help="keep marketplaces/retailers (default drops them)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
