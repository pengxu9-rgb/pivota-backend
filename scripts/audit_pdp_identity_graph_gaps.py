#!/usr/bin/env python3
"""Read-only PDP identity graph coverage audit.

Outputs JSON for:
  - internal catalog products with offers but no product_group_members row
  - active external seeds attached with legacy merchant|platform|product keys
  - high-confidence recovery proposals the writer would apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402
from services.pdp_identity_recovery import build_recovery_proposals  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        report = await build_recovery_proposals(limit=args.limit, offset=args.offset)
        report["dry_run"] = True
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
