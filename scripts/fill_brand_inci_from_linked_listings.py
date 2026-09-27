#!/usr/bin/env python3
"""Fill brand products' missing INCI from their linked retailer listings (services.linked_listing_inci_fill).

  Dry run (default; writes nothing):
    python -m scripts.fill_brand_inci_from_linked_listings
  Write the fills (canonical INCI intake, reseller_listing rank; recomputes serving eligibility):
    python -m scripts.fill_brand_inci_from_linked_listings --apply

Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.linked_listing_inci_fill import fill  # noqa: E402


async def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    await database.connect()
    try:
        result = await fill(database, apply=args.apply)
    finally:
        await database.disconnect()
    print("RESULT " + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
